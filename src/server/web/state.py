from typing import Any

import attr
from litestar import Request
from litestar.datastructures import State

from server.core.browser_login import BrowserLoginManager
from server.core.config import AppConfig
from server.core.display import DisplayManager
from server.core.gateway_client import GatewayClient
from server.core.grants import ConsumerContext
from server.core.grants import ConsumerPermissionFiles
from server.core.grants import parse_permissions_header
from server.core.latchkey_runtime import LatchkeyRuntime


@attr.s(auto_attribs=True, frozen=True)
class AppServices:
    config: AppConfig
    runtime: LatchkeyRuntime
    gateway: GatewayClient
    consumer_files: ConsumerPermissionFiles
    browser_logins: BrowserLoginManager
    display: DisplayManager


def services_from(state: State) -> AppServices:
    services = state.get("services")
    if not isinstance(services, AppServices):
        raise RuntimeError("app services not initialized (lifespan did not run?)")
    return services


def provide_services(state: State) -> AppServices:
    return services_from(state)


def provide_consumer(request: Request[Any, Any, Any]) -> ConsumerContext | None:
    """The calling app per the router's service-proxy headers; None when this isn't a service call."""
    app_id = request.headers.get("X-OpenHost-Consumer-Id")
    app_name = request.headers.get("X-OpenHost-Consumer-Name")
    if app_id is None or app_name is None:
        return None
    return ConsumerContext(
        app_id=app_id,
        app_name=app_name,
        grants=parse_permissions_header(request.headers.get("X-OpenHost-Permissions")),
    )
