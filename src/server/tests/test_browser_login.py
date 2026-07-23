import asyncio
from pathlib import Path
from typing import Any

import pytest

from server.core.browser_login import BrowserLoginManager
from server.core.browser_login import LoginAlreadyRunningError
from server.core.browser_login import LoginState
from server.core.config import AppConfig
from server.core.display import DisplayManager
from server.core.gateway_client import GatewayClient
from server.core.gateway_client import GatewayRpcError
from server.core.latchkey_runtime import LatchkeyRuntime


def _config() -> AppConfig:
    return AppConfig(
        app_name="latchkey",
        app_data_dir=Path("/tmp"),
        router_url="http://router",
        app_token="token",
        zone_domain="zone.test",
        gateway_host="127.0.0.1",
        gateway_port=1,
        gateway_password="pw",
        vnc_host="127.0.0.1",
        vnc_port=1,
        display=":99",
        screen_geometry="1600x1000x24",
        gateway_idle_seconds=300,
        display_idle_seconds=60,
    )


class FakeGateway(GatewayClient):
    def __init__(self, fail_on: str | None = None) -> None:
        super().__init__(_config(), LatchkeyRuntime(_config()))
        self.fail_on = fail_on
        self.calls: list[str] = []

    async def rpc(self, command: str, params: dict[str, Any] | None = None, timeout: float = 0) -> Any:
        self.calls.append(command)
        if command == self.fail_on:
            raise GatewayRpcError(400, f"{command} failed")
        return None


class FakeDisplay(DisplayManager):
    """DisplayManager with process spawning stubbed out; tracks start/stop calls."""

    def __init__(self) -> None:
        super().__init__(_config())
        self.starts = 0
        self.stops = 0
        self._up = False

    @property
    def running(self) -> bool:
        return self._up

    async def _start_locked(self) -> None:
        self.starts += 1
        self._up = True

    async def _stop_locked(self) -> None:
        if self._up:
            self.stops += 1
        self._up = False


def test_login_runs_prepare_then_browser() -> None:
    async def scenario() -> None:
        gateway = FakeGateway()
        display = FakeDisplay()
        manager = BrowserLoginManager(gateway, display)
        job = manager.start("google-gmail")
        assert job.state == LoginState.PREPARING
        await manager.wait_until_done()
        assert gateway.calls == ["auth browser-prepare", "auth browser"]
        assert manager.current is not None and manager.current.state == LoginState.SUCCEEDED
        # The login started the display stack and released it when done.
        assert display.starts == 1
        assert display._active_uses == 0

    asyncio.run(scenario())


def test_display_stops_when_idle_but_not_during_login() -> None:
    async def scenario() -> None:
        display = FakeDisplay()
        async with display.use():
            assert display.running
            await display.stop_if_idle()
            assert display.running, "must not stop while in use"
        # Recently used: the idle window (60s) has not elapsed.
        await display.stop_if_idle()
        assert display.running
        # Pretend the last use was long ago.
        display._last_use = asyncio.get_running_loop().time() - 3600
        await display.stop_if_idle()
        assert not display.running
        assert display.stops == 1

    asyncio.run(scenario())


def test_prepare_failure_skips_login() -> None:
    async def scenario() -> None:
        gateway = FakeGateway(fail_on="auth browser-prepare")
        manager = BrowserLoginManager(gateway, FakeDisplay())
        manager.start("google-gmail")
        await manager.wait_until_done()
        assert gateway.calls == ["auth browser-prepare"]
        current = manager.current
        assert current is not None and current.state == LoginState.FAILED
        assert current.error == "auth browser-prepare failed"

    asyncio.run(scenario())


def test_login_failure_reported() -> None:
    async def scenario() -> None:
        gateway = FakeGateway(fail_on="auth browser")
        manager = BrowserLoginManager(gateway, FakeDisplay())
        manager.start("slack")
        await manager.wait_until_done()
        current = manager.current
        assert current is not None and current.state == LoginState.FAILED

    asyncio.run(scenario())


def test_second_start_rejected_while_active() -> None:
    async def scenario() -> None:
        gateway = FakeGateway()
        manager = BrowserLoginManager(gateway, FakeDisplay())
        manager.start("slack")
        with pytest.raises(LoginAlreadyRunningError):
            manager.start("github")
        await manager.wait_until_done()
        # After completion a new login may start.
        manager.start("github")
        await manager.wait_until_done()
        assert gateway.calls.count("auth browser") == 2

    asyncio.run(scenario())
