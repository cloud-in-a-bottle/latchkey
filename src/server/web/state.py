import attr
from litestar.datastructures import State

from server.core.browser_login import BrowserLoginManager
from server.core.config import AppConfig
from server.core.gateway_client import GatewayClient
from server.core.grants import ConsumerPermissionFiles
from server.core.latchkey_runtime import LatchkeyRuntime


@attr.s(auto_attribs=True, frozen=True)
class AppServices:
    config: AppConfig
    runtime: LatchkeyRuntime
    gateway: GatewayClient
    consumer_files: ConsumerPermissionFiles
    browser_logins: BrowserLoginManager


def services_from(state: State) -> AppServices:
    services = state.get("services")
    if not isinstance(services, AppServices):
        raise RuntimeError("app services not initialized (lifespan did not run?)")
    return services
