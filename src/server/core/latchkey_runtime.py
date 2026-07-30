import asyncio
import base64
import os
import secrets
import signal
from asyncio.subprocess import Process
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import attr
import httpx
from loguru import logger

from server.core.config import AppConfig

CLI_TIMEOUT_SECONDS = 60.0
GATEWAY_START_TIMEOUT_SECONDS = 20.0


def account_option(account: str | None) -> tuple[str, ...]:
    """CLI arguments selecting an account: a global latchkey option, so it must precede the subcommand.

    `None` means "don't pass one" (latchkey then resolves the single stored account, or fails if a
    service has several); the empty string is latchkey's unnamed default account.
    """
    return () if account is None else ("--account", account)


class LatchkeyCliError(Exception):
    def __init__(self, args_: tuple[str, ...], returncode: int, stdout: str, stderr: str) -> None:
        super().__init__(
            f"latchkey {' '.join(args_)} failed with code {returncode}: {stderr.strip() or stdout.strip()}"
        )
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@attr.s(auto_attribs=True, frozen=True)
class CliResult:
    returncode: int
    stdout: str
    stderr: str


class LatchkeyRuntime:
    """Owns the latchkey installation: encryption key, CLI invocations, and the gateway process."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._encryption_key: str | None = None
        self._gateway_process: Process | None = None
        self._gateway_lock = asyncio.Lock()
        self._active_uses = 0
        self._last_use = 0.0

    def prepare(self) -> None:
        self._config.latchkey_home.mkdir(parents=True, exist_ok=True)
        self._config.consumer_permissions_dir.mkdir(parents=True, exist_ok=True)
        self._encryption_key = self._load_or_create_encryption_key()
        # Deny-all default policy: the gateway skips permission checks entirely when no
        # permissions.json exists, so a request that somehow lacked our per-consumer JWT
        # override would otherwise be allowed through.
        default_permissions = self._config.latchkey_home / "permissions.json"
        if not default_permissions.exists():
            default_permissions.write_text('{"rules": []}\n')

    def _load_or_create_encryption_key(self) -> str:
        path = self._config.encryption_key_path
        if path.exists():
            key = path.read_text().strip()
            if key == "":
                raise RuntimeError(f"encryption key file {path} exists but is empty")
            return key
        key = base64.b64encode(secrets.token_bytes(32)).decode()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(mode=0o600)
        path.write_text(key)
        return key

    def _env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        if self._encryption_key is None:
            raise RuntimeError("LatchkeyRuntime.prepare() must be called first")
        env = dict(os.environ)
        env.update(
            {
                "LATCHKEY_DIRECTORY": str(self._config.latchkey_home),
                "LATCHKEY_ENCRYPTION_KEY": self._encryption_key,
                "LATCHKEY_DISABLE_COUNTING": "1",
                "LATCHKEY_EPHEMERAL_BROWSER": "1" if self._config.ephemeral_browser else "",
            }
        )
        # Never inherit client-gateway mode: all our CLI calls are local operations.
        env.pop("LATCHKEY_GATEWAY", None)
        if extra:
            env.update(extra)
        return env

    async def run_cli(self, *args: str, timeout: float = CLI_TIMEOUT_SECONDS) -> CliResult:
        process = await asyncio.create_subprocess_exec(
            "latchkey",
            *args,
            env=self._env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError:
            process.kill()
            raise
        result = CliResult(
            returncode=process.returncode if process.returncode is not None else -1,
            stdout=stdout_bytes.decode(errors="replace"),
            stderr=stderr_bytes.decode(errors="replace"),
        )
        if result.returncode != 0:
            raise LatchkeyCliError(args, result.returncode, result.stdout, result.stderr)
        return result

    async def ensure_browser(self) -> None:
        """Discover/persist a browser for `auth browser` flows (finds the system Chromium)."""
        try:
            await self.run_cli("ensure-browser", timeout=300.0)
            logger.info("latchkey browser configured")
        except (LatchkeyCliError, TimeoutError) as e:
            logger.warning("latchkey ensure-browser failed; browser logins will not work: {}", e)

    async def create_permissions_jwt(self, permissions_config_path: str) -> str:
        result = await self.run_cli("gateway", "create-jwt", permissions_config_path)
        token = result.stdout.strip().splitlines()[-1]
        if token.count(".") != 2:
            raise RuntimeError(f"unexpected create-jwt output: {result.stdout!r}")
        return token

    async def start_gateway(self, wait_healthy: bool = True) -> None:
        async with self._gateway_lock:
            await self._start_gateway_locked(wait_healthy)

    async def _start_gateway_locked(self, wait_healthy: bool) -> None:
        if self._gateway_process is not None and self._gateway_process.returncode is None:
            return
        if self._gateway_process is not None:
            logger.warning("latchkey gateway exited with code {}; restarting", self._gateway_process.returncode)
        self._gateway_process = await asyncio.create_subprocess_exec(
            "latchkey",
            "gateway",
            "--host",
            self._config.gateway_host,
            "--port",
            str(self._config.gateway_port),
            env=self._env(extra={"LATCHKEY_GATEWAY_LISTEN_PASSWORD": self._config.gateway_password}),
            # Own session/group, so stopping the gateway also kills any browser
            # it spawned for a login flow (nothing lingers holding memory).
            start_new_session=True,
        )
        logger.info("started latchkey gateway on {}:{}", self._config.gateway_host, self._config.gateway_port)
        self._last_use = asyncio.get_running_loop().time()
        if wait_healthy:
            await self._wait_gateway_healthy()

    async def _wait_gateway_healthy(self) -> None:
        deadline = asyncio.get_running_loop().time() + GATEWAY_START_TIMEOUT_SECONDS
        async with httpx.AsyncClient() as client:
            while True:
                try:
                    response = await client.get(
                        f"{self._config.gateway_url}/",
                        headers={"X-Latchkey-Gateway-Password": self._config.gateway_password},
                        timeout=2.0,
                    )
                    if response.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                if asyncio.get_running_loop().time() > deadline:
                    raise RuntimeError("latchkey gateway did not become healthy in time")
                await asyncio.sleep(0.2)

    async def restart_gateway(self) -> None:
        """Needed after operations the gateway only reads at startup (e.g. `services register`)."""
        await self.stop_gateway()
        await self.start_gateway()

    @asynccontextmanager
    async def gateway_use(self) -> AsyncIterator[None]:
        """Start the gateway if needed and hold off the idle reaper for the duration."""
        self._active_uses += 1
        try:
            await self.start_gateway()
            yield
        finally:
            self._active_uses -= 1
            self._last_use = asyncio.get_running_loop().time()

    async def stop_gateway_if_idle(self) -> None:
        idle_seconds = self._config.gateway_idle_seconds
        if idle_seconds <= 0 or self._active_uses > 0 or not self.gateway_running:
            return
        if asyncio.get_running_loop().time() - self._last_use < idle_seconds:
            return
        async with self._gateway_lock:
            if self._active_uses > 0:
                return
            logger.info("stopping idle latchkey gateway")
            await self._stop_gateway_locked()

    async def stop_gateway(self) -> None:
        async with self._gateway_lock:
            await self._stop_gateway_locked()

    async def _stop_gateway_locked(self) -> None:
        process = self._gateway_process
        self._gateway_process = None
        if process is None or process.returncode is not None:
            return
        _terminate_group(process, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=15.0)
        except TimeoutError:
            _terminate_group(process, signal.SIGKILL)
            await process.wait()

    @property
    def gateway_running(self) -> bool:
        return self._gateway_process is not None and self._gateway_process.returncode is None


def _terminate_group(process: Process, sig: signal.Signals) -> None:
    # The process was started in its own session, so the group id is its pid.
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        pass
