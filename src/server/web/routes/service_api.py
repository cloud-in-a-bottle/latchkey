from typing import Any

from litestar import Request
from litestar import Response
from litestar import get
from litestar import post
from litestar import route
from loguru import logger

from server.core.gateway_client import GatewayRpcError
from server.core.gateway_client import is_permission_denial
from server.core.grants import META_SCOPE
from server.core.grants import PERMISSION_SERVICES_READ
from server.core.grants import ConsumerContext
from server.core.grants import ScopeGrant
from server.core.grants import build_grant_url
from server.core.grants import detent_grants
from server.core.grants import has_meta_permission
from server.core.proxying import PROXY_PATH_PREFIX
from server.core.proxying import extract_proxy_target
from server.core.proxying import forwardable_request_headers
from server.core.proxying import forwardable_response_headers
from server.web.api_models import ErrorBody
from server.web.api_models import GrantUrlBody
from server.web.api_models import PermissionRequiredBody
from server.web.api_models import RequestGrantRequest
from server.web.api_models import RequiredGrant
from server.web.api_models import ServicesListBody
from server.web.state import AppServices

_META_GRANT = ScopeGrant(scope=META_SCOPE, permissions=(PERMISSION_SERVICES_READ,))


def _permission_required_response(
    services: AppServices,
    consumer: ConsumerContext,
    message: str,
    grant: ScopeGrant | None = None,
) -> Response[PermissionRequiredBody]:
    return Response(
        PermissionRequiredBody(
            message=message,
            grant_url=build_grant_url(services.config.own_url, consumer, grant=grant),
            required_grant=RequiredGrant(grant=grant.to_payload_model()) if grant is not None else None,
        ),
        status_code=403,
    )


def _not_a_service_call() -> Response[ErrorBody]:
    return Response(
        ErrorBody(error="bad_request", message="not a service call: consumer headers missing"), status_code=400
    )


@get("/api/services")
async def list_services(services: AppServices, consumer: ConsumerContext | None) -> Response[Any]:
    if consumer is None:
        return _not_a_service_call()
    if not has_meta_permission(consumer.grants, PERMISSION_SERVICES_READ):
        return _permission_required_response(
            services, consumer, "listing services requires the metadata grant", grant=_META_GRANT
        )
    await services.runtime.ensure_gateway_running()
    result = await services.gateway.rpc("services list")
    return Response(ServicesListBody(services=result))


@get("/api/services/{service_name:str}")
async def service_info(services: AppServices, consumer: ConsumerContext | None, service_name: str) -> Response[Any]:
    if consumer is None:
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
    services: AppServices, consumer: ConsumerContext | None, data: RequestGrantRequest
) -> Response[GrantUrlBody] | Response[ErrorBody]:
    if consumer is None:
        return _not_a_service_call()
    grant_url = build_grant_url(
        services.config.own_url, consumer, grant=data.grant.to_scope_grant(), return_to=data.return_to
    )
    return Response(GrantUrlBody(grant_url=grant_url, required_grant=RequiredGrant(grant=data.grant)))


@route(
    f"{PROXY_PATH_PREFIX}{{target_url:path}}",
    http_method=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
)
async def proxy(
    request: Request[Any, Any, Any],
    services: AppServices,
    consumer: ConsumerContext | None,
    target_url: str,
) -> Response[Any]:
    if consumer is None:
        return _not_a_service_call()

    # Use the raw ASGI path so percent-encoding survives litestar's path-parameter decoding.
    raw_path: bytes = request.scope["raw_path"]
    target = extract_proxy_target(raw_path.decode("latin-1"), request.scope["query_string"].decode("latin-1"))
    if target is None:
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
    headers = forwardable_request_headers(list(request.headers.items()))
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

    return Response(
        upstream.content,
        status_code=upstream.status_code,
        headers=forwardable_response_headers(list(upstream.headers.items())),
        media_type=upstream.headers.get("content-type", "application/octet-stream"),
    )
