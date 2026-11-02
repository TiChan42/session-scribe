"""Authenticated inference over an explicitly mounted local Unix socket.

No IP network, DNS, proxy, redirect or alternative endpoint is used. The separate
host gateway authorizes the service UID and short-lived, job-scoped credential.
"""

from __future__ import annotations

import http.client
import json
import socket
import stat
from pathlib import Path


class UnixGateway:
    def __init__(self, path: Path, token: str, *, timeout: int = 180):
        if (
            not path.is_absolute()
            or ".." in path.parts
            or any(p.is_symlink() for p in (path, *path.parents))
            or len(str(path).encode()) > 100
        ):
            raise ValueError("explicit local inference socket required")
        if not token or any(c in token for c in "\r\n"):
            raise ValueError("gateway credential required")
        if not 1 <= timeout <= 180:
            raise ValueError("invalid gateway timeout")
        self.path, self.token, self.timeout = path, token, timeout

    def chat(self, request: dict) -> dict:
        if not stat.S_ISSOCK(self.path.stat(follow_symlinks=False).st_mode):
            raise ValueError("inference endpoint is not a Unix socket")
        body = json.dumps(request, ensure_ascii=False, allow_nan=False).encode()
        if len(body) > 262_144:
            raise ValueError("inference request exceeds gateway limit")
        path = str(self.path)

        class Connection(http.client.HTTPConnection):
            def connect(connection):
                connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                connection.sock.settimeout(connection.timeout)
                try:
                    connection.sock.connect(path)
                except BaseException:
                    connection.sock.close()
                    raise

        connection = Connection("localhost", timeout=self.timeout)
        try:
            connection.request(
                "POST",
                "/api/chat",
                body,
                {"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"},
            )
            response = connection.getresponse()
            if response.status != 200:
                raise OSError("inference gateway rejected request")
            data = response.read(2_000_001)
            if len(data) > 2_000_000:
                raise ValueError("inference response too large")
            result = json.loads(data)
            if not isinstance(result, dict):
                raise ValueError("invalid inference response")
            return result
        finally:
            connection.close()


def configured_gateway(origin: str, token: str):
    # These origins come only from the operator's readonly worker configuration.
    if origin == "unix:///gateway/inference.sock":
        return UnixGateway(Path("/gateway/inference.sock"), token)
    from .analysis import Gateway

    return Gateway(origin, token)
