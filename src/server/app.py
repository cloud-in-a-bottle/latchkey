import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import attr
from litestar import Litestar
from litestar import get
from litestar.static_files import create_static_files_router
from loguru import logger

from server.browser_login import BrowserLoginManager
from server.config import load_config
from server.config import novnc_dir
from server.gateway_client import GatewayClient
from server.grants import ConsumerPermissionFiles
from server.latchkey_runtime import LatchkeyRuntime
from server.routes.owner_api import approve_grant
from server.routes.owner_api import auth_clear
from server.routes.owner_api import auth_set
from server.routes.owner_api import browser_login_start
from server.routes.owner_api import browser_login_status
from server.routes.owner_api import owner_service_info
from server.routes.owner_api import services_register
from server.routes.owner_api import status
from server.routes.pages import connect_page
from server.routes.pages import console
from server.routes.pages import grant_page
from server.routes.service_api import list_services
from server.routes.service_api import proxy
from server.routes.service_api import request_grant
from server.routes.service_api import service_info
from server.routes.vnc import vnc_bridge
from server.state import AppServices


@attr.s(auto_attribs=True, frozen=True)
class HealthStatus:
    status: str


@get("/health", sync_to_thread=False)
def health() -> HealthStatus:
    return HealthStatus(status="ok")


@asynccontextmanager
async def lifespan(app: Litestar) -> AsyncIterator[None]:
    config = load_config()
    runtime = LatchkeyRuntime(config)
    runtime.prepare()
    gateway = GatewayClient(config)
    services = AppServices(
        config=config,
        runtime=runtime,
        gateway=gateway,
        consumer_files=ConsumerPermissionFiles(config, runtime),
        browser_logins=BrowserLoginManager(gateway),
    )
    app.state.services = services

    await runtime.start_gateway()
    # Browser discovery can download things on first boot; don't block startup on it.
    browser_task = asyncio.create_task(runtime.ensure_browser())
    logger.info("latchkey app ready")
    try:
        yield
    finally:
        browser_task.cancel()
        await gateway.close()
        await runtime.stop_gateway()


def _route_handlers() -> list[object]:
    handlers: list[object] = [
        health,
        console,
        connect_page,
        grant_page,
        status,
        auth_set,
        auth_clear,
        browser_login_start,
        browser_login_status,
        owner_service_info,
        services_register,
        approve_grant,
        list_services,
        service_info,
        request_grant,
        proxy,
        vnc_bridge,
    ]
    novnc = novnc_dir()
    if novnc.is_dir():
        handlers.append(create_static_files_router(path="/novnc", directories=[novnc]))
    else:
        logger.warning("noVNC directory {} not found; browser-login page will not render a viewer", novnc)
    return handlers


app = Litestar(route_handlers=_route_handlers(), lifespan=[lifespan])  # type: ignore[arg-type]
