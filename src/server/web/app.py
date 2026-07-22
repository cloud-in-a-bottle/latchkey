import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import attr
from litestar import Litestar
from litestar import Request
from litestar import Response
from litestar import get
from litestar.contrib.jinja import JinjaTemplateEngine
from litestar.exceptions import ValidationException
from litestar.static_files import create_static_files_router
from litestar.template import TemplateConfig
from loguru import logger

from server.core.browser_login import BrowserLoginManager
from server.core.config import load_config
from server.core.config import novnc_dir
from server.core.gateway_client import GatewayClient
from server.core.grants import ConsumerPermissionFiles
from server.core.latchkey_runtime import LatchkeyRuntime
from server.web.api_models import ErrorBody
from server.web.routes.owner_api import approve_grant
from server.web.routes.owner_api import auth_clear
from server.web.routes.owner_api import auth_set
from server.web.routes.owner_api import browser_login_start
from server.web.routes.owner_api import browser_login_status
from server.web.routes.owner_api import owner_service_info
from server.web.routes.owner_api import services_register
from server.web.routes.owner_api import status
from server.web.routes.pages import connect_page
from server.web.routes.pages import console
from server.web.routes.pages import grant_page
from server.web.routes.service_api import list_services
from server.web.routes.service_api import proxy
from server.web.routes.service_api import request_grant
from server.web.routes.service_api import service_info
from server.web.routes.vnc import vnc_bridge
from server.web.state import AppServices


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


def _validation_exception_handler(request: Request[Any, Any, Any], exc: ValidationException) -> Response[ErrorBody]:
    """Keep the documented {"error": "bad_request", "message": ...} shape for binding failures."""
    messages = [str(e["message"]) for e in (exc.extra or []) if isinstance(e, dict) and "message" in e]
    return Response(ErrorBody(error="bad_request", message="; ".join(messages) or exc.detail), status_code=400)


app = Litestar(
    route_handlers=_route_handlers(),  # type: ignore[arg-type]
    lifespan=[lifespan],
    template_config=TemplateConfig(directory=Path(__file__).parent / "templates", engine=JinjaTemplateEngine),
    exception_handlers={ValidationException: _validation_exception_handler},
)
