import contextlib
import json
import socketserver
import tempfile
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

from sessionscribe.unix_gateway import UnixGateway, configured_gateway


@contextlib.contextmanager
def endpoint(body=b'{"done":true}', status=200):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            length = int(self.headers["Content-Length"])
            calls.append((self.path, self.headers["Authorization"], self.rfile.read(length)))
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            if status == 302:
                self.send_header("Location", "https://example.invalid/denied")
            self.end_headers()
            with contextlib.suppress(BrokenPipeError):
                self.wfile.write(body)

    # Short, resolved path: Unix sockaddr paths are bounded and /tmp can be a symlink.
    with tempfile.TemporaryDirectory(prefix="scribe-", dir=Path("/tmp").resolve()) as folder:
        path = Path(folder) / "inference.sock"
        with socketserver.UnixStreamServer(str(path), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
            thread.start()
            try:
                yield path, calls
            finally:
                server.shutdown()
                thread.join(timeout=2)


def test_local_inference_socket_sends_exact_route_and_job_credential():
    with endpoint() as (path, calls):
        response = UnixGateway(path, "synthetic-job-credential").chat({"stream": False})
        assert response == {"done": True}
        route, auth, raw = calls[0]
        assert route == "/api/chat" and auth == "Bearer synthetic-job-credential"
        assert json.loads(raw) == {"stream": False}


@pytest.mark.parametrize("status", [302, 403, 429, 502])
def test_gateway_denial_never_redirects_or_echoes_credentials(status):
    with endpoint(status=status) as (path, calls):
        with pytest.raises(OSError) as error:
            UnixGateway(path, "synthetic-private-token").chat({})
        assert len(calls) == 1
        assert "synthetic-private-token" not in str(error.value)


@pytest.mark.parametrize("body", [b"invalid-json", b"[]", b"x" * 2_000_001])
def test_invalid_or_oversized_response_is_not_a_valid_result(body):
    with endpoint(body=body) as (path, _calls), pytest.raises(ValueError):
        UnixGateway(path, "synthetic-job-credential").chat({})


def test_unsafe_socket_and_unapproved_unix_origins_are_rejected(tmp_path):
    path = tmp_path / "ordinary-file"
    path.write_text("not a socket")
    with pytest.raises(ValueError):
        UnixGateway(path, "synthetic").chat({})
    alias = tmp_path / "alias"
    alias.symlink_to(path)
    with pytest.raises(ValueError):
        UnixGateway(alias, "synthetic")
    for origin in ("unix:///tmp/arbitrary.sock", "http://example.invalid", "unix://host/path"):
        with pytest.raises(ValueError):
            configured_gateway(origin, "synthetic")
    with pytest.raises(ValueError):
        UnixGateway(Path("relative.sock"), "synthetic")


def test_oversized_request_is_rejected_before_delivery():
    with endpoint() as (path, calls):
        with pytest.raises(ValueError):
            UnixGateway(path, "synthetic").chat({"content": "x" * 262_145})
        assert calls == []
