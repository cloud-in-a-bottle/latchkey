import asyncio
import time
from enum import StrEnum
from typing import Any

import attr
from loguru import logger

from server.core.display import DisplayManager
from server.core.gateway_client import BROWSER_LOGIN_TIMEOUT_SECONDS
from server.core.gateway_client import GatewayClient
from server.core.gateway_client import GatewayRpcError

# Worst-case wall time for a login job: the login and (when needed) the preparation each get the
# RPC timeout, and a preparation is followed by a second login attempt.
LOGIN_MAX_SECONDS = 3 * BROWSER_LOGIN_TIMEOUT_SECONDS

# Latchkey refuses `auth browser` for services whose login needs a one-time setup (e.g. Google,
# where it creates an OAuth client) until that setup has run. Matched by message for want of
# structured error codes (upstream wishlist).
_PREPARATION_REQUIRED_MARKER = "requires preparation first"


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
    # The account whose stored setup (e.g. its OAuth client) this login reuses, when the owner
    # picked one; the account actually logged in as is only known once the flow succeeds.
    reuse_account: str | None = None
    logged_in_account: str | None = None
    error: str | None = None


class BrowserLoginManager:
    """Runs `auth browser` flows through the gateway RPC, one at a time (there is one display).

    Latchkey stores one preparation per service and re-runs the whole setup whenever asked, so a job
    tries the login first and only prepares when latchkey says the service has no setup yet.
    """

    def __init__(self, gateway: GatewayClient, display: DisplayManager) -> None:
        self._gateway = gateway
        self._display = display
        self._job: LoginJob | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def current(self) -> LoginJob | None:
        return self._job

    def start(self, service_name: str, reuse_account: str | None = None) -> LoginJob:
        if self._job is not None and self._job.state.is_active:
            raise LoginAlreadyRunningError(self._job.service_name)
        job = LoginJob(
            service_name=service_name,
            state=LoginState.RUNNING,
            started_at=time.time(),
            reuse_account=reuse_account,
        )
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
                try:
                    account = await self._login(job)
                except GatewayRpcError as e:
                    if _PREPARATION_REQUIRED_MARKER not in e.message:
                        raise
                    self._job = attr.evolve(job, state=LoginState.PREPARING)
                    await self._gateway.rpc(
                        "auth browser-prepare",
                        {"serviceName": job.service_name},
                        timeout=BROWSER_LOGIN_TIMEOUT_SECONDS,
                    )
                    self._job = attr.evolve(job, state=LoginState.RUNNING)
                    account = await self._login(job)
        except GatewayRpcError as e:
            logger.warning("browser login for {} failed: {}", job.service_name, e.message)
            self._job = attr.evolve(job, state=LoginState.FAILED, error=e.message)
        except Exception as e:
            logger.exception("browser login for {} errored", job.service_name)
            self._job = attr.evolve(job, state=LoginState.FAILED, error=str(e))
        else:
            logger.info("browser login for {} succeeded as account {!r}", job.service_name, account)
            self._job = attr.evolve(job, state=LoginState.SUCCEEDED, logged_in_account=account)

    async def _login(self, job: LoginJob) -> str | None:
        """Run `auth browser`, returning the account latchkey stored the credentials under."""
        params: dict[str, Any] = {"serviceName": job.service_name}
        if job.reuse_account is not None:
            params["account"] = job.reuse_account
        result = await self._gateway.rpc("auth browser", params, timeout=BROWSER_LOGIN_TIMEOUT_SECONDS)
        account = result.get("account") if isinstance(result, dict) else None
        if not isinstance(account, str):
            # The credentials are stored either way; only the "logged in as" label is lost.
            logger.warning("no account in `auth browser` result for {}: {!r}", job.service_name, result)
            return None
        return account


class LoginAlreadyRunningError(Exception):
    def __init__(self, service_name: str) -> None:
        super().__init__(f"a browser login for '{service_name}' is already running")
        self.service_name = service_name
