import asyncio
import os
import signal
from collections.abc import AsyncIterator
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path

from loguru import logger

from server.core.config import AppConfig

START_TIMEOUT_SECONDS = 15.0
STOP_TIMEOUT_SECONDS = 5.0
_POLL_SECONDS = 0.1


class DisplayStartError(RuntimeError):
    pass


class DisplayManager:
    """On-demand virtual display stack (Xvfb + window manager + x11vnc) for browser logins.

    Logins are rare, so the graphical stack only runs while something needs it — an active login
    flow or a connected VNC viewer — and the idle reaper stops it again to keep idle memory down.
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._processes: list[asyncio.subprocess.Process] = []
        self._lock = asyncio.Lock()
        self._active_uses = 0
        self._last_use = 0.0

    @property
    def running(self) -> bool:
        return bool(self._processes) and all(p.returncode is None for p in self._processes)

    @asynccontextmanager
    async def use(self) -> AsyncIterator[None]:
        """Hold the display stack up for the duration of the block, starting it if needed."""
        self._active_uses += 1
        try:
            async with self._lock:
                if not self.running:
                    await self._stop_locked()
                    await self._start_locked()
            yield
        finally:
            self._active_uses -= 1
            self._last_use = asyncio.get_running_loop().time()

    async def stop_if_idle(self) -> None:
        idle_seconds = self._config.display_idle_seconds
        if idle_seconds <= 0 or self._active_uses > 0:
            return
        if not self.running and not self._processes:
            return
        if asyncio.get_running_loop().time() - self._last_use < idle_seconds:
            return
        async with self._lock:
            if self._active_uses > 0:
                return
            logger.info("stopping idle display stack")
            await self._stop_locked()

    async def stop(self) -> None:
        async with self._lock:
            await self._stop_locked()

    def _display_number(self) -> str:
        return self._config.display.removeprefix(":")

    async def _start_locked(self) -> None:
        display_num = self._display_number()
        geometry = self._config.screen_geometry
        socket_path = Path(f"/tmp/.X11-unix/X{display_num}")
        socket_path.unlink(missing_ok=True)
        env = dict(os.environ)
        env["DISPLAY"] = f":{display_num}"
        try:
            await self._spawn("Xvfb", f":{display_num}", "-screen", "0", geometry, "-nolisten", "tcp", env=env)
            await self._wait_for(socket_path.exists, f"X socket {socket_path}")
            # Kiosk-style window manager: fullscreens every window, so the login
            # browser fills the screen (and thus the whole VNC viewer) exactly.
            await self._spawn("matchbox-window-manager", "-use_titlebar", "no", env=env)
            await self._spawn(
                "x11vnc",
                "-display",
                f":{display_num}",
                "-localhost",
                "-rfbport",
                str(self._config.vnc_port),
                "-forever",
                "-shared",
                "-nopw",
                "-quiet",
                env=env,
            )
            await self._wait_for_vnc_port()
        except Exception:
            await self._stop_locked()
            raise
        logger.info("display stack started on :{} ({})", display_num, geometry)

    async def _spawn(self, *args: str, env: dict[str, str]) -> None:
        process = await asyncio.create_subprocess_exec(*args, env=env, start_new_session=True)
        self._processes.append(process)

    async def _wait_for(self, condition: Callable[[], bool], what: str) -> None:
        deadline = asyncio.get_running_loop().time() + START_TIMEOUT_SECONDS
        while not condition():
            if asyncio.get_running_loop().time() > deadline:
                raise DisplayStartError(f"timed out waiting for {what}")
            await asyncio.sleep(_POLL_SECONDS)

    async def _wait_for_vnc_port(self) -> None:
        deadline = asyncio.get_running_loop().time() + START_TIMEOUT_SECONDS
        while True:
            try:
                _, writer = await asyncio.open_connection(self._config.vnc_host, self._config.vnc_port)
            except OSError:
                if asyncio.get_running_loop().time() > deadline:
                    raise DisplayStartError("timed out waiting for x11vnc to listen") from None
                await asyncio.sleep(_POLL_SECONDS)
            else:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass
                return

    async def _stop_locked(self) -> None:
        processes = self._processes
        self._processes = []
        for process in reversed(processes):
            if process.returncode is not None:
                continue
            _terminate_group(process, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), timeout=STOP_TIMEOUT_SECONDS)
            except TimeoutError:
                _terminate_group(process, signal.SIGKILL)
                await process.wait()


def _terminate_group(process: asyncio.subprocess.Process, sig: signal.Signals) -> None:
    # The process was started in its own session, so the group id is its pid.
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        pass
