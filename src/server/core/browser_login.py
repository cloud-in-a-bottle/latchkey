import asyncio
import time
from enum import StrEnum

import attr
from loguru import logger

from server.core.display import DisplayManager
from server.core.gateway_client import BROWSER_LOGIN_TIMEOUT_SECONDS
from server.core.gateway_client import GatewayClient
from server.core.gateway_client import GatewayRpcError


class LoginState(StrEnum):
    # One-time interactive setup some services need before login (e.g. Google flows create an
    # OAuth client in the cloud console; latchkey's browser-prepare).
    PREPARING = "preparing"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"

    @property
    def is_active(self) -> bool:
        return self in (LoginState.PREPARING, LoginState.RUNNING)


@attr.s(auto_attribs=True, frozen=True)
class LoginJob:
    service_name: str
    state: LoginState
    started_at: float
    error: str | None = None


class BrowserLoginManager:
    """Runs `auth browser` flows through the gateway RPC, one at a time (there is one display).

    Each job chains latchkey's two browser steps: `auth browser-prepare` (a no-op returning
    alreadyPrepared for services that don't need it) and then `auth browser`.
    """

    def __init__(self, gateway: GatewayClient, display: DisplayManager) -> None:
        self._gateway = gateway
        self._display = display
        self._job: LoginJob | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def current(self) -> LoginJob | None:
        return self._job

    def start(self, service_name: str) -> LoginJob:
        if self._job is not None and self._job.state.is_active:
            raise LoginAlreadyRunningError(self._job.service_name)
        job = LoginJob(service_name=service_name, state=LoginState.PREPARING, started_at=time.time())
        self._job = job
        self._task = asyncio.create_task(self._run(job))
        return job

    async def wait_until_done(self) -> None:
        if self._task is not None:
            await self._task

    async def _run(self, job: LoginJob) -> None:
        try:
            # The login browser renders onto the on-demand display stack; hold it
            # up for the whole flow.
            async with self._display.use():
                await self._gateway.rpc(
                    "auth browser-prepare",
                    {"serviceName": job.service_name},
                    timeout=BROWSER_LOGIN_TIMEOUT_SECONDS,
                )
                self._job = attr.evolve(job, state=LoginState.RUNNING)
                await self._gateway.rpc(
                    "auth browser",
                    {"serviceName": job.service_name},
                    timeout=BROWSER_LOGIN_TIMEOUT_SECONDS,
                )
        except GatewayRpcError as e:
            logger.warning("browser login for {} failed: {}", job.service_name, e.message)
            self._job = attr.evolve(job, state=LoginState.FAILED, error=e.message)
        except Exception as e:
            logger.exception("browser login for {} errored", job.service_name)
            self._job = attr.evolve(job, state=LoginState.FAILED, error=str(e))
        else:
            logger.info("browser login for {} succeeded", job.service_name)
            self._job = attr.evolve(job, state=LoginState.SUCCEEDED)


class LoginAlreadyRunningError(Exception):
    def __init__(self, service_name: str) -> None:
        super().__init__(f"a browser login for '{service_name}' is already running")
        self.service_name = service_name
