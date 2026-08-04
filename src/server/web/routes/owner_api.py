import math
import shlex
import time
from typing import Any

from litestar import Response
from litestar import get
from litestar import post
from loguru import logger

from server.core.accounts import ServiceAccounts
from server.core.accounts import parse_auth_list
from server.core.browser_login import LOGIN_MAX_SECONDS
from server.core.browser_login import LoginAlreadyRunningError
from server.core.browser_login import LoginJob
from server.core.gateway_client import GatewayRpcError
from server.core.latchkey_runtime import LatchkeyCliError
from server.core.latchkey_runtime import account_option
from server.core.router_client import GrantCreationError
from server.core.router_client import grant_app_scoped
from server.web.api_models import ApproveGrantRequest
from server.web.api_models import AuthClearRequest
from server.web.api_models import AuthSetRequest
from server.web.api_models import BrowserLoginStartRequest
from server.web.api_models import BrowserLoginStatusBody
from server.web.api_models import ErrorBody
from server.web.api_models import OkBody
from server.web.api_models import ServicesRegisterRequest
from server.web.api_models import StatusBody
from server.web.state import AppServices


def _cli_error(e: LatchkeyCliError) -> Response[ErrorBody]:
    return Response(ErrorBody(error="bad_request", message=str(e)), status_code=400)


def _login_status(login: LoginJob | None, viewer_connected: bool = False) -> BrowserLoginStatusBody:
    if login is None:
        return BrowserLoginStatusBody(state="idle", viewer_connected=viewer_connected)
    return BrowserLoginStatusBody(
        state=login.state.value,
        service=login.service_name,
        error=login.error,
        viewer_connected=viewer_connected,
        logged_in_account=login.logged_in_account,
    )


def _login_busy_message(service_name: str, login: LoginJob | None) -> str:
    message = f"a browser login for '{service_name}' is already running — only one can run at a time."
    remaining = LOGIN_MAX_SECONDS - (time.time() - login.started_at) if login is not None else LOGIN_MAX_SECONDS
    minutes = max(1, math.ceil(remaining / 60))
    return f"{message} Close the tab running it, or wait for it to finish (at most {minutes} min)."


@get("/owner/api/status")
async def status(services: AppServices) -> StatusBody:
    connected: list[ServiceAccounts] | None = None
    service_names: list[str] | None = None
    try:
        # Starts the idle-stopped gateway on demand.
        connected = list(parse_auth_list(await services.gateway.rpc("auth list")))
        service_names = await services.gateway.rpc("services list")
        gateway_healthy = True
    except Exception as e:
        logger.warning("gateway unavailable for status: {}", e)
        gateway_healthy = False
    login = services.browser_logins.current
    return StatusBody(
        gateway_healthy=gateway_healthy,
        connected=connected,
        services=service_names,
        browser_login=_login_status(login, viewer_connected=services.display.viewer_connected)
        if login is not None
        else None,
    )


@post("/owner/api/auth/set", status_code=200)
async def auth_set(services: AppServices, data: AuthSetRequest) -> Response[OkBody] | Response[ErrorBody]:
    args = (*account_option(data.account), "auth", "set", data.service_name, *shlex.split(data.curl_args))
    try:
        await services.runtime.run_cli(*args)
    except LatchkeyCliError as e:
        return _cli_error(e)
    return Response(OkBody())


@post("/owner/api/auth/clear", status_code=200)
async def auth_clear(services: AppServices, data: AuthClearRequest) -> Response[OkBody] | Response[ErrorBody]:
    args: tuple[str, ...]
    if data.service_name is None:
        # Clearing everything: `-y` because there is no terminal to confirm at.
        args = ("auth", "clear", "-y")
    elif data.all_accounts:
        args = ("auth", "clear", data.service_name, "--all")
    else:
        args = (*account_option(data.account), "auth", "clear", data.service_name)
    try:
        await services.runtime.run_cli(*args)
    except LatchkeyCliError as e:
        return _cli_error(e)
    return Response(OkBody())


@post("/owner/api/services/register", status_code=200)
async def services_register(
    services: AppServices, data: ServicesRegisterRequest
) -> Response[OkBody] | Response[ErrorBody]:
    """Register a runtime service (e.g. a self-hosted GitLab) so credentials can be stored for it."""
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
    services: AppServices, data: BrowserLoginStartRequest
) -> Response[BrowserLoginStatusBody] | Response[ErrorBody]:
    try:
        job = services.browser_logins.start(data.service_name, reuse_account=data.reuse_account)
    except LoginAlreadyRunningError as e:
        return Response(
            ErrorBody(
                error="login_already_running",
                message=_login_busy_message(e.service_name, services.browser_logins.current),
            ),
            status_code=409,
        )
    return Response(_login_status(job, viewer_connected=services.display.viewer_connected))


@get("/owner/api/browser-login/status")
async def browser_login_status(services: AppServices) -> BrowserLoginStatusBody:
    return _login_status(services.browser_logins.current, viewer_connected=services.display.viewer_connected)


@get("/owner/api/service-info/{service_name:str}")
async def owner_service_info(services: AppServices, service_name: str) -> Response[Any]:
    try:
        # Latchkey RPC passthrough; the shape is latchkey's (see services/latchkey/openapi.yaml).
        result = await services.gateway.rpc("services info", {"serviceName": service_name})
    except GatewayRpcError as e:
        if e.status_code == 400:
            return Response(ErrorBody(error="unknown_service", message=e.message), status_code=404)
        raise
    return Response(result)


@post("/owner/api/grants/approve", status_code=200)
async def approve_grant(services: AppServices, data: ApproveGrantRequest) -> Response[OkBody] | Response[ErrorBody]:
    """Create the app-scoped grant in the router after the owner confirmed on the consent page."""
    try:
        await grant_app_scoped(services.config, data.consumer_app_id, data.grant.to_scope_grant())
    except GrantCreationError as e:
        return Response(ErrorBody(error="grant_failed", message=str(e)), status_code=502)
    return Response(OkBody())
