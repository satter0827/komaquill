"""Verify reset connections do not stop socket serving or prevent shutdown."""

import asyncio
import http.client
import json
import socket
import struct
import sys
import threading
import time
from contextlib import closing

import uvicorn
from komaquill.interfaces.http.serving import serve
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route


def test_socket_server_survives_resets_and_finishes_shutdown(caplog):
    async def ready(request):
        return JSONResponse(
            {"selector": isinstance(asyncio.get_running_loop(), asyncio.SelectorEventLoop)}
        )

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    address = listener.getsockname()
    server = uvicorn.Server(
        uvicorn.Config(Starlette(routes=[Route("/", ready)]), access_log=False, loop="asyncio")
    )
    observed = []
    errors = []

    def exercise():
        try:
            deadline = time.monotonic() + 10
            while not server.started:
                if time.monotonic() >= deadline:
                    raise TimeoutError
                time.sleep(0.01)
            for _ in range(20):
                with socket.create_connection(address, timeout=2) as peer:
                    peer.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
                    peer.recv(4096)
                    peer.setsockopt(
                        socket.SOL_SOCKET,
                        socket.SO_LINGER,
                        struct.pack("hh" if sys.platform == "win32" else "ii", 1, 0),
                    )
            with closing(http.client.HTTPConnection(*address, timeout=2)) as connection:
                connection.request("GET", "/", headers={"Connection": "close"})
                response = connection.getresponse()
                observed.append((response.status, json.loads(response.read())))
        except BaseException as error:
            errors.append(error)
        finally:
            server.should_exit = True

    client = threading.Thread(target=exercise, daemon=True)
    started = time.monotonic()
    try:
        client.start()
        serve(server, [listener])
        client.join(timeout=2)
        assert not client.is_alive() and not errors
        assert observed == [(200, {"selector": True})]
        assert time.monotonic() - started < 15
        assert not [r for r in caplog.records if r.exc_info]
    finally:
        server.should_exit = True
        listener.close()
