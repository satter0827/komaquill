"""Export a standard Caddy configuration without exposing backend listeners."""

import json
from pathlib import Path

from komaquill.adapters.atomic import atomic_bytes
from komaquill.adapters.paths import WorkspacePaths
from komaquill.adapters.web_configuration import WebSettings
from komaquill.domain.errors import KomaquillError


def write_config(
    project: WorkspacePaths, settings: WebSettings, web_port: int, comfy_port: int
) -> Path:
    if settings.origin is None or not all(1 <= port <= 65535 for port in (web_port, comfy_port)):
        raise KomaquillError(code="invalid_configuration")
    storage = json.dumps(str((project.runtime / "caddy").resolve()))
    origin = json.dumps(settings.origin)
    value = f"""{{
    admin off
    skip_install_trust
    auto_https disable_redirects
    storage file_system {{
        root {storage}
    }}
}}
{origin} {{
    tls internal
    header Strict-Transport-Security "max-age=31536000"
    handle_path /comfy/* {{
        forward_auth 127.0.0.1:{web_port} {{
            uri /api/v1/access
            header_up -Connection
            header_up -Upgrade
        }}
        reverse_proxy 127.0.0.1:{comfy_port} {{
            stream_timeout {settings.websocket_seconds}s
        }}
    }}
    handle {{
        reverse_proxy 127.0.0.1:{web_port}
    }}
}}
"""
    path = project.state / "web/Caddyfile"
    atomic_bytes(path, value.encode())
    return path
