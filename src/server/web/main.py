# Single-process server entrypoint: the hypercorn CLI runs a master process plus a spawned
# worker, while serving programmatically keeps the whole app in one Python process, which
# matters on memory-tight instances.

import asyncio
import signal

from hypercorn.asyncio import serve
from hypercorn.config import Config

from server.web.app import app

BIND = "0.0.0.0:8080"


async def _serve() -> None:
    config = Config()
    config.bind = [BIND]
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown.set)
    # Litestar is a plain ASGI app; hypercorn's callable alias just doesn't match structurally.
    await serve(app, config, shutdown_trigger=shutdown.wait)  # type: ignore[arg-type]


def main() -> None:
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
