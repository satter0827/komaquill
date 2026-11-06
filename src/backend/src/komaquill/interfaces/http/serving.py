"""Share the HTTP event loop used by the CLI and live verification."""

import asyncio
import socket
import sys

from uvicorn import Server


def serve(server: Server, sockets: list[socket.socket] | None = None) -> None:
    if sys.platform == "win32":
        # Socket serving needs no asynchronous subprocess support. Selector cleanup
        # also completes when a peer resets a connection on supported Python 3.12.
        asyncio.run(server.serve(sockets=sockets), loop_factory=asyncio.SelectorEventLoop)
    else:
        server.run(sockets=sockets)
