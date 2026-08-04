import asyncio
from pathlib import Path
from typing import Any

import pytest

from server.core.browser_login import BrowserLoginManager
from server.core.browser_login import LoginAlreadyRunningError
from server.core.browser_login import LoginState
from server.core.config import AppConfig
from server.core.display import DisplayManager
from server.core.display import ViewerBusyError
from server.core.gateway_client import GatewayClient
from server.core.gateway_client import GatewayRpcError
from server.core.latchkey_runtime import LatchkeyRuntime

# What latchkey answers `auth browser` with for a service whose login needs a one-time setup first.
PREPARATION_REQUIRED = "Service google-gmail requires preparation first. Run 'latchkey auth browser-prepare ...'"


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
        vnc_max_session_seconds=1800,
        ephemeral_browser=True,
    )


class FakeGateway(GatewayClient):
    """Gateway RPC stub: records calls and fails the commands in `failures` with the given message."""

    def __init__(self, failures: dict[str, str] | None = None, fail_first_only: bool = False) -> None:
        super().__init__(_config(), LatchkeyRuntime(_config()))
        self.failures = failures or {}
        self.fail_first_only = fail_first_only
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    @property
    def commands(self) -> list[str]:
        return [command for command, _ in self.calls]

    async def rpc(self, command: str, params: dict[str, Any] | None = None, timeout: float = 0) -> Any:
        self.calls.append((command, params))
        message = self.failures.get(command)
        if message is not None and not (self.fail_first_only and self.commands.count(command) > 1):
            raise GatewayRpcError(400, message)
        if command == "auth browser":
            return {"account": "bob@example.com"}
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


def test_login_skips_prepare_when_not_needed() -> None:
    async def scenario() -> None:
        gateway = FakeGateway()
        display = FakeDisplay()
        manager = BrowserLoginManager(gateway, display)
        job = manager.start("slack")
        assert job.state == LoginState.RUNNING
        await manager.wait_until_done()
        assert gateway.calls == [("auth browser", {"serviceName": "slack"})]
        current = manager.current
        assert current is not None and current.state == LoginState.SUCCEEDED
        assert current.logged_in_account == "bob@example.com"
        # The login started the display stack and released it when done.
        assert display.starts == 1
        assert display._active_uses == 0

    asyncio.run(scenario())


def test_login_prepares_only_when_latchkey_asks() -> None:
    async def scenario() -> None:
        gateway = FakeGateway({"auth browser": PREPARATION_REQUIRED}, fail_first_only=True)
        manager = BrowserLoginManager(gateway, FakeDisplay())
        manager.start("google-gmail")
        await manager.wait_until_done()
        assert gateway.commands == ["auth browser", "auth browser-prepare", "auth browser"]
        current = manager.current
        assert current is not None and current.state == LoginState.SUCCEEDED

    asyncio.run(scenario())


def test_reuse_account_is_passed_to_latchkey() -> None:
    async def scenario() -> None:
        gateway = FakeGateway()
        manager = BrowserLoginManager(gateway, FakeDisplay())
        manager.start("google-gmail", reuse_account="alice@example.com")
        await manager.wait_until_done()
        assert gateway.calls == [("auth browser", {"serviceName": "google-gmail", "account": "alice@example.com"})]

    asyncio.run(scenario())


def test_single_vnc_viewer_slot() -> None:
    async def scenario() -> None:
        display = FakeDisplay()
        async with display.viewer():
            assert display.viewer_connected
            with pytest.raises(ViewerBusyError):
                async with display.viewer():
                    pass
        # Released: a new viewer may connect.
        assert not display.viewer_connected
        async with display.viewer():
            assert display.viewer_connected

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


def test_prepare_failure_skips_the_retry() -> None:
    async def scenario() -> None:
        gateway = FakeGateway({"auth browser": PREPARATION_REQUIRED, "auth browser-prepare": "setup exploded"})
        manager = BrowserLoginManager(gateway, FakeDisplay())
        manager.start("google-gmail")
        await manager.wait_until_done()
        assert gateway.commands == ["auth browser", "auth browser-prepare"]
        current = manager.current
        assert current is not None and current.state == LoginState.FAILED
        assert current.error == "setup exploded"

    asyncio.run(scenario())


def test_login_failure_reported_without_preparing() -> None:
    async def scenario() -> None:
        gateway = FakeGateway({"auth browser": "Error: login cancelled"})
        manager = BrowserLoginManager(gateway, FakeDisplay())
        manager.start("slack")
        await manager.wait_until_done()
        assert gateway.commands == ["auth browser"]
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
        assert gateway.commands.count("auth browser") == 2

    asyncio.run(scenario())
