import asyncio
from typing import Any

from litestar import WebSocket
from litestar import websocket
from litestar.exceptions import WebSocketDisconnect
from loguru import logger

from server.core.display import DisplayStartError
from server.core.display import ViewerBusyError
from server.web.state import AppServices

_READ_CHUNK = 65536

VIEWER_BUSY_REASON = "the login screen is already open elsewhere — close other tabs or wait for that session to end"


@websocket("/owner/vnc")
async def vnc_bridge(socket: WebSocket[Any, Any, Any], services: AppServices) -> None:
    """Bridge the owner's noVNC websocket to the local x11vnc server (RFB over binary frames).

    The display stack is on-demand; the single connected viewer starts it and holds it up.
    """
    await socket.accept()
    try:
        async with services.display.viewer():
            await _bridge(socket, services)
    except ViewerBusyError:
        await socket.close(code=1013, reason=VIEWER_BUSY_REASON)
    except DisplayStartError as e:
        logger.warning("could not start display stack: {}", e)
        await socket.close(code=1011, reason="VNC server unavailable")


async def _bridge(socket: WebSocket[Any, Any, Any], services: AppServices) -> None:
    try:
        reader, writer = await asyncio.open_connection(services.config.vnc_host, services.config.vnc_port)
    except OSError as e:
        logger.warning("could not connect to VNC server: {}", e)
        await socket.close(code=1011, reason="VNC server unavailable")
        return

    async def ws_to_tcp() -> None:
        while True:
            data = await socket.receive_data(mode="binary")
            writer.write(data)
            await writer.drain()

    async def tcp_to_ws() -> None:
        while True:
            data = await reader.read(_READ_CHUNK)
            if data == b"":
                return
            await socket.send_data(data, mode="binary")

    max_session = services.config.vnc_max_session_seconds
    tasks = [asyncio.ensure_future(ws_to_tcp()), asyncio.ensure_future(tcp_to_ws())]
    try:
        done, pending = await asyncio.wait(
            tasks, return_when=asyncio.FIRST_COMPLETED, timeout=max_session if max_session > 0 else None
        )
        for task in pending:
            task.cancel()
        if not done:
            logger.info("closing VNC viewer: session time limit ({:.0f}s) reached", max_session)
            await socket.close(code=1000, reason="VNC session time limit reached; reconnect to continue")
        for task in done:
            exc = task.exception()
            if exc is not None and not isinstance(exc, WebSocketDisconnect):
                logger.warning("VNC bridge ended with error: {}", exc)
    finally:
        for task in tasks:
            task.cancel()
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
