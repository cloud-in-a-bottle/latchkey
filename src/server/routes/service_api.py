import json
import re
from typing import Any
from urllib.parse import urlencode

import attr
from litestar import Request
from litestar import Response
from litestar import get
from litestar import post
from litestar import route
from loguru import logger

from server.api_models import ErrorBody
from server.api_models import GrantUrlBody
from server.api_models import PermissionRequiredBody
from server.api_models import RequestGrantRequest
from server.api_models import RequiredGrant
from server.api_models import ServicesListBody
from server.gateway_client import GatewayRpcError
from server.gateway_client import is_permission_denial
from server.grants import META_SCOPE
from server.grants import PERMISSION_SERVICES_READ
from server.grants import ScopeGrant
from server.grants import detent_grants
from server.grants import has_meta_permission
from server.grants import parse_permissions_header
from server.state import AppServices
from server.state import services_from

_META_GRANT = ScopeGrant(scope=META_SCOPE, permissions=(PERMISSION_SERVICES_READ,))

# Request headers never forwarded upstream. The router's service proxy authenticates callers
# for us; Authorization and Cookie from the original request must not leak to third parties
# (browser-originated service calls carry the owner's zone session cookie).
_STRIPPED_REQUEST_HEADERS = {
    "host",
    "authorization",
    "cookie",
    "content-length",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "expect",
}

_STRIPPED_RESPONSE_HEADERS = {
    "content-length",
    "content-encoding",
    "connection",
    "keep-alive",
    "transfer-encoding",
    "upgrade",
    "trailers",
}

PROXY_PATH_PREFIX = "/api/proxy/"


@attr.s(auto_attribs=True, frozen=True)
class ConsumerContext:
    app_id: str
    app_name: str
    grants: tuple[ScopeGrant, ...]


class NotAServiceCallError(Exception):
    pass


def consumer_context(request: Request[Any, Any, Any]) -> ConsumerContext:
    app_id = request.headers.get("X-OpenHost-Consumer-Id")
    app_name = request.headers.get("X-OpenHost-Consumer-Name")
    if app_id is None or app_name is None:
        raise NotAServiceCallError()
    return ConsumerContext(
        app_id=app_id,
        app_name=app_name,
        grants=parse_permissions_header(request.headers.get("X-OpenHost-Permissions")),
    )


def _grant_url(
    services: AppServices,
    consumer: ConsumerContext,
    grant: ScopeGrant | None = None,
    return_to: str | None = None,
) -> str:
    params: dict[str, str] = {"consumer_id": consumer.app_id, "consumer_name": consumer.app_name}
    if grant is not None:
        params["grant"] = json.dumps(grant.as_payload())
    if return_to is not None:
        params["return_to"] = return_to
    return f"{services.config.own_url}/grant?{urlencode(params)}"


def _permission_required_response(
    services: AppServices,
    consumer: ConsumerContext,
    message: str,
    grant: ScopeGrant | None = None,
) -> Response[PermissionRequiredBody]:
    return Response(
        PermissionRequiredBody(
            message=message,
            grant_url=_grant_url(services, consumer, grant=grant),
            required_grant=RequiredGrant(grant=grant.to_payload_model()) if grant is not None else None,
        ),
        status_code=403,
    )


def _not_a_service_call() -> Response[ErrorBody]:
    return Response(
        ErrorBody(error="bad_request", message="not a service call: consumer headers missing"), status_code=400
    )


@get("/api/services")
async def list_services(request: Request[Any, Any, Any]) -> Response[Any]:
    services = services_from(request.app.state)
    try:
        consumer = consumer_context(request)
    except NotAServiceCallError:
        return _not_a_service_call()
    if not has_meta_permission(consumer.grants, PERMISSION_SERVICES_READ):
        return _permission_required_response(
            services, consumer, "listing services requires the metadata grant", grant=_META_GRANT
        )
    await services.runtime.ensure_gateway_running()
    result = await services.gateway.rpc("services list")
    return Response(ServicesListBody(services=result))


@get("/api/services/{service_name:str}")
async def service_info(request: Request[Any, Any, Any], service_name: str) -> Response[Any]:
    services = services_from(request.app.state)
    try:
        consumer = consumer_context(request)
    except NotAServiceCallError:
        return _not_a_service_call()
    if not has_meta_permission(consumer.grants, PERMISSION_SERVICES_READ):
        return _permission_required_response(
            services, consumer, "service info requires the metadata grant", grant=_META_GRANT
        )
    await services.runtime.ensure_gateway_running()
    try:
        # Latchkey RPC passthrough; the shape is latchkey's (see services/latchkey/openapi.yaml).
        result = await services.gateway.rpc("services info", {"serviceName": service_name})
    except GatewayRpcError as e:
        if e.status_code == 400:
            return Response(ErrorBody(error="unknown_service", message=e.message), status_code=404)
        raise
    return Response(result)


@post("/api/grants/request", status_code=200)
async def request_grant(
    request: Request[Any, Any, Any], data: RequestGrantRequest
) -> Response[GrantUrlBody] | Response[ErrorBody]:
    services = services_from(request.app.state)
    try:
        consumer = consumer_context(request)
    except NotAServiceCallError:
        return _not_a_service_call()
    grant = data.grant.to_scope_grant()
    return Response(
        GrantUrlBody(
            grant_url=_grant_url(services, consumer, grant=grant, return_to=data.return_to),
            required_grant=RequiredGrant(grant=data.grant),
        )
    )


@route(
    f"{PROXY_PATH_PREFIX}{{target_url:path}}",
    http_method=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
)
async def proxy(request: Request[Any, Any, Any], target_url: str) -> Response[Any]:
    services = services_from(request.app.state)
    try:
        consumer = consumer_context(request)
    except NotAServiceCallError:
        return _not_a_service_call()

    # Rebuild the target from the raw ASGI path so percent-encoding survives litestar's
    # path-parameter decoding.
    raw_path: bytes = request.scope["raw_path"]
    raw = raw_path.decode("latin-1")
    if not raw.startswith(PROXY_PATH_PREFIX):
        return Response(
            ErrorBody(error="bad_request", message=f"proxy path must start with {PROXY_PATH_PREFIX}"),
            status_code=400,
        )
    target = raw[len(PROXY_PATH_PREFIX) :]
    # Proxies (including the router) may collapse "//" in paths; restore the scheme separator.
    if re.match(r"^https?:/[^/]", target):
        target = target.replace(":/", "://", 1)
    query = request.scope["query_string"].decode("latin-1")
    if query:
        target = f"{target}?{query}"
    if not target.startswith(("http://", "https://")):
        return Response(
            ErrorBody(
                error="bad_request",
                message="target must be an absolute http(s) URL, e.g. /api/proxy/https://slack.com/api/...",
            ),
            status_code=400,
        )

    if not detent_grants(consumer.grants):
        return _permission_required_response(
            services, consumer, "no API access grants; request one for the scope you need"
        )

    jwt = await services.consumer_files.jwt_for(consumer.app_id, consumer.grants)
    headers = [
        (name, value)
        for name, value in request.headers.items()
        if name.lower() not in _STRIPPED_REQUEST_HEADERS and not name.lower().startswith("x-openhost-")
    ]
    body = await request.body()

    await services.runtime.ensure_gateway_running()
    upstream = await services.gateway.proxy(request.method, target, headers, body, jwt)

    if is_permission_denial(upstream):
        logger.info("denied {} {} for consumer {}", request.method, target, consumer.app_name)
        return _permission_required_response(
            services,
            consumer,
            "request not allowed by this consumer's grants; request a grant for the scope covering it",
        )

    response_headers = {
        name: value for name, value in upstream.headers.items() if name.lower() not in _STRIPPED_RESPONSE_HEADERS
    }
    return Response(
        upstream.content,
        status_code=upstream.status_code,
        headers=response_headers,
        media_type=upstream.headers.get("content-type", "application/octet-stream"),
    )
