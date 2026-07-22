import asyncio
from typing import Any

from litestar import WebSocket
from litestar import websocket
from litestar.exceptions import WebSocketDisconnect
from loguru import logger

from server.web.state import AppServices

_READ_CHUNK = 65536


@websocket("/owner/vnc")
async def vnc_bridge(socket: WebSocket[Any, Any, Any], services: AppServices) -> None:
    """Bridge the owner's noVNC websocket to the local x11vnc server (RFB over binary frames)."""
    await socket.accept()
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

    tasks = [asyncio.ensure_future(ws_to_tcp()), asyncio.ensure_future(tcp_to_ws())]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
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
