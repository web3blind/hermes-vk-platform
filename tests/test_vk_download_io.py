"""Exercise the actual urllib/cache path on loopback, never live VK."""
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

# Reuse the standalone plugin bootstrap; its fixture isolates Hermes home.
from test_vk_adapter import clear_vk_env  # noqa: F401
from plugins.platforms.vk.adapter import _download_attachment


@pytest.fixture
def attachment_server():
    body = b"verified attachment content\n"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            # Intentionally no length: exercise streaming size enforcement.
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/sample.pdf", body
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_real_http_download_materializes_complete_file(attachment_server, tmp_path):
    url, body = attachment_server
    path = Path(_download_attachment(url, "application/pdf", timeout=3))
    assert path.is_relative_to(tmp_path)
    assert path.read_bytes() == body


def test_failed_redownload_preserves_completed_file(attachment_server):
    url, body = attachment_server
    path = Path(_download_attachment(url, "application/pdf", timeout=3))
    with pytest.raises(RuntimeError, match="size limit"):
        _download_attachment(url, "application/pdf", timeout=3, max_bytes=3)
    assert path.read_bytes() == body
    assert list(path.parent.iterdir()) == [path]
