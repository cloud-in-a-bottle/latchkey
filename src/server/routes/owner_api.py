import shlex
from typing import Any

import httpx
from litestar import Request
from litestar import Response
from litestar import get
from litestar import post

from server.browser_login import LoginAlreadyRunningError
from server.gateway_client import GatewayRpcError
from server.grants import parse_grant_payload
from server.latchkey_runtime import LatchkeyCliError
from server.state import services_from

SERVICE_URL = "github.com/imbue-openhost/openhost-latchkey"


def _bad_request(message: str) -> Response[dict[str, Any]]:
    return Response({"error": "bad_request", "message": message}, status_code=400)


@get("/owner/api/status")
async def status(request: Request[Any, Any, Any]) -> Response[Any]:
    services = services_from(request.app.state)
    gateway_healthy = await services.gateway.is_healthy()
    result: dict[str, Any] = {"gateway_healthy": gateway_healthy}
    if gateway_healthy:
        result["auth"] = await services.gateway.rpc("auth list")
        result["services"] = await services.gateway.rpc("services list")
    login = services.browser_logins.current
    if login is not None:
        result["browser_login"] = {"service": login.service_name, "state": login.state.value, "error": login.error}
    return Response(result)


@post("/owner/api/auth/set")
async def auth_set(request: Request[Any, Any, Any]) -> Response[Any]:
    services = services_from(request.app.state)
    body = await request.json()
    service_name = body.get("service_name")
    curl_args = body.get("curl_args")
    if not isinstance(service_name, str) or service_name == "":
        return _bad_request("'service_name' must be a non-empty string")
    if not isinstance(curl_args, str) or curl_args.strip() == "":
        return _bad_request("'curl_args' must be a non-empty string, e.g. -H \"Authorization: Bearer ...\"")
    try:
        args = shlex.split(curl_args)
    except ValueError as e:
        return _bad_request(f"could not parse curl arguments: {e}")
    try:
        await services.runtime.run_cli("auth", "set", service_name, *args)
    except LatchkeyCliError as e:
        return _bad_request(str(e))
    return Response({"ok": True})


@post("/owner/api/auth/clear")
async def auth_clear(request: Request[Any, Any, Any]) -> Response[Any]:
    services = services_from(request.app.state)
    body = await request.json()
    service_name = body.get("service_name")
    args = ["auth", "clear"]
    if service_name is not None:
        if not isinstance(service_name, str) or service_name == "":
            return _bad_request("'service_name' must be a non-empty string when present")
        args.append(service_name)
    try:
        await services.runtime.run_cli(*args)
    except LatchkeyCliError as e:
        return _bad_request(str(e))
    return Response({"ok": True})


@post("/owner/api/services/register")
async def services_register(request: Request[Any, Any, Any]) -> Response[Any]:
    """Register a runtime service (e.g. a self-hosted GitLab) so credentials can be stored for it."""
    services = services_from(request.app.state)
    body = await request.json()
    service_name = body.get("service_name")
    base_api_url = body.get("base_api_url")
    service_family = body.get("service_family")
    if not isinstance(service_name, str) or service_name == "":
        return _bad_request("'service_name' must be a non-empty string")
    if not isinstance(base_api_url, str) or base_api_url == "":
        return _bad_request("'base_api_url' must be a non-empty string")
    args = ["services", "register", service_name, f"--base-api-url={base_api_url}"]
    if service_family is not None:
        if not isinstance(service_family, str) or service_family == "":
            return _bad_request("'service_family' must be a non-empty string when present")
        args.append(f"--service-family={service_family}")
    try:
        await services.runtime.run_cli(*args)
    except LatchkeyCliError as e:
        return _bad_request(str(e))
    # The gateway only loads the service registry at startup.
    await services.runtime.restart_gateway()
    return Response({"ok": True})


@post("/owner/api/browser-login/start")
async def browser_login_start(request: Request[Any, Any, Any]) -> Response[Any]:
    services = services_from(request.app.state)
    body = await request.json()
    service_name = body.get("service_name")
    if not isinstance(service_name, str) or service_name == "":
        return _bad_request("'service_name' must be a non-empty string")
    await services.runtime.ensure_gateway_running()
    try:
        job = services.browser_logins.start(service_name)
    except LoginAlreadyRunningError as e:
        return Response({"error": "login_already_running", "message": str(e)}, status_code=409)
    return Response({"service": job.service_name, "state": job.state.value})


@get("/owner/api/browser-login/status")
async def browser_login_status(request: Request[Any, Any, Any]) -> Response[Any]:
    services = services_from(request.app.state)
    login = services.browser_logins.current
    if login is None:
        return Response({"state": "idle"})
    return Response({"service": login.service_name, "state": login.state.value, "error": login.error})


@get("/owner/api/service-info/{service_name:str}")
async def owner_service_info(request: Request[Any, Any, Any], service_name: str) -> Response[Any]:
    services = services_from(request.app.state)
    await services.runtime.ensure_gateway_running()
    try:
        result = await services.gateway.rpc("services info", {"serviceName": service_name})
    except GatewayRpcError as e:
        if e.status_code == 400:
            return Response({"error": "unknown_service", "message": e.message}, status_code=404)
        raise
    return Response(result)


@post("/owner/api/grants/approve")
async def approve_grant(request: Request[Any, Any, Any]) -> Response[Any]:
    """Create the app-scoped grant in the router after the owner confirmed on the consent page."""
    services = services_from(request.app.state)
    body = await request.json()
    consumer_app_id = body.get("consumer_app_id")
    if not isinstance(consumer_app_id, str) or consumer_app_id == "":
        return _bad_request("'consumer_app_id' must be a non-empty string")
    grant = parse_grant_payload(body.get("grant"))
    if grant is None:
        return _bad_request('\'grant\' must be {"scope": str, "permissions": [str, ...], "schemas"?: {name: schema}}')

    async with httpx.AsyncClient() as client:
        router_response = await client.post(
            f"{services.config.router_url}/api/permissions/v2/grant_app_scoped",
            headers={"Authorization": f"Bearer {services.config.app_token}"},
            json={
                "consumer_app_id": consumer_app_id,
                "service_url": SERVICE_URL,
                "grant": grant.as_payload(),
            },
            timeout=10.0,
        )
    if router_response.status_code != 200:
        return Response(
            {
                "error": "grant_failed",
                "message": f"router returned {router_response.status_code}: {router_response.text[:300]}",
            },
            status_code=502,
        )
    return Response({"ok": True})
