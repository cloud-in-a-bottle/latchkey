import shlex
from typing import Any

import httpx
from litestar import Request
from litestar import Response
from litestar import get
from litestar import post

from server.api_models import ApproveGrantRequest
from server.api_models import AuthClearRequest
from server.api_models import AuthSetRequest
from server.api_models import BrowserLoginStartRequest
from server.api_models import BrowserLoginStatusBody
from server.api_models import ErrorBody
from server.api_models import OkBody
from server.api_models import ServicesRegisterRequest
from server.api_models import StatusBody
from server.browser_login import LoginAlreadyRunningError
from server.browser_login import LoginJob
from server.gateway_client import GatewayRpcError
from server.latchkey_runtime import LatchkeyCliError
from server.state import services_from

SERVICE_URL = "github.com/imbue-openhost/openhost-latchkey/services/latchkey"


def _cli_error(e: LatchkeyCliError) -> Response[ErrorBody]:
    return Response(ErrorBody(error="bad_request", message=str(e)), status_code=400)


def _login_status(login: LoginJob | None) -> BrowserLoginStatusBody:
    if login is None:
        return BrowserLoginStatusBody(state="idle")
    return BrowserLoginStatusBody(state=login.state.value, service=login.service_name, error=login.error)


@get("/owner/api/status")
async def status(request: Request[Any, Any, Any]) -> StatusBody:
    services = services_from(request.app.state)
    gateway_healthy = await services.gateway.is_healthy()
    auth: dict[str, Any] | None = None
    service_names: list[str] | None = None
    if gateway_healthy:
        auth = await services.gateway.rpc("auth list")
        service_names = await services.gateway.rpc("services list")
    login = services.browser_logins.current
    return StatusBody(
        gateway_healthy=gateway_healthy,
        auth=auth,
        services=service_names,
        browser_login=_login_status(login) if login is not None else None,
    )


@post("/owner/api/auth/set", status_code=200)
async def auth_set(request: Request[Any, Any, Any], data: AuthSetRequest) -> Response[OkBody] | Response[ErrorBody]:
    services = services_from(request.app.state)
    try:
        await services.runtime.run_cli("auth", "set", data.service_name, *shlex.split(data.curl_args))
    except LatchkeyCliError as e:
        return _cli_error(e)
    return Response(OkBody())


@post("/owner/api/auth/clear", status_code=200)
async def auth_clear(
    request: Request[Any, Any, Any], data: AuthClearRequest
) -> Response[OkBody] | Response[ErrorBody]:
    services = services_from(request.app.state)
    args = ["auth", "clear"]
    if data.service_name is not None:
        args.append(data.service_name)
    try:
        await services.runtime.run_cli(*args)
    except LatchkeyCliError as e:
        return _cli_error(e)
    return Response(OkBody())


@post("/owner/api/services/register", status_code=200)
async def services_register(
    request: Request[Any, Any, Any], data: ServicesRegisterRequest
) -> Response[OkBody] | Response[ErrorBody]:
    """Register a runtime service (e.g. a self-hosted GitLab) so credentials can be stored for it."""
    services = services_from(request.app.state)
    args = ["services", "register", data.service_name, f"--base-api-url={data.base_api_url}"]
    if data.service_family is not None:
        args.append(f"--service-family={data.service_family}")
    try:
        await services.runtime.run_cli(*args)
    except LatchkeyCliError as e:
        return _cli_error(e)
    # The gateway only loads the service registry at startup.
    await services.runtime.restart_gateway()
    return Response(OkBody())


@post("/owner/api/browser-login/start", status_code=200)
async def browser_login_start(
    request: Request[Any, Any, Any], data: BrowserLoginStartRequest
) -> Response[BrowserLoginStatusBody] | Response[ErrorBody]:
    services = services_from(request.app.state)
    await services.runtime.ensure_gateway_running()
    try:
        job = services.browser_logins.start(data.service_name)
    except LoginAlreadyRunningError as e:
        return Response(ErrorBody(error="login_already_running", message=str(e)), status_code=409)
    return Response(_login_status(job))


@get("/owner/api/browser-login/status")
async def browser_login_status(request: Request[Any, Any, Any]) -> BrowserLoginStatusBody:
    services = services_from(request.app.state)
    return _login_status(services.browser_logins.current)


@get("/owner/api/service-info/{service_name:str}")
async def owner_service_info(request: Request[Any, Any, Any], service_name: str) -> Response[Any]:
    services = services_from(request.app.state)
    await services.runtime.ensure_gateway_running()
    try:
        # Latchkey RPC passthrough; the shape is latchkey's (see services/latchkey/openapi.yaml).
        result = await services.gateway.rpc("services info", {"serviceName": service_name})
    except GatewayRpcError as e:
        if e.status_code == 400:
            return Response(ErrorBody(error="unknown_service", message=e.message), status_code=404)
        raise
    return Response(result)


@post("/owner/api/grants/approve", status_code=200)
async def approve_grant(
    request: Request[Any, Any, Any], data: ApproveGrantRequest
) -> Response[OkBody] | Response[ErrorBody]:
    """Create the app-scoped grant in the router after the owner confirmed on the consent page."""
    services = services_from(request.app.state)
    async with httpx.AsyncClient() as client:
        router_response = await client.post(
            f"{services.config.router_url}/api/permissions/v2/grant_app_scoped",
            headers={"Authorization": f"Bearer {services.config.app_token}"},
            json={
                "consumer_app_id": data.consumer_app_id,
                "service_url": SERVICE_URL,
                "grant": data.grant.to_scope_grant().as_payload(),
            },
            timeout=10.0,
        )
    if router_response.status_code != 200:
        return Response(
            ErrorBody(
                error="grant_failed",
                message=f"router returned {router_response.status_code}: {router_response.text[:300]}",
            ),
            status_code=502,
        )
    return Response(OkBody())
