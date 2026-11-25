"""The one-line installer against a server that publishes no client binaries."""
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

INSTALL_SH = Path(__file__).resolve().parent.parent / "install.sh"


class _NotFound(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


def test_a_missing_binary_names_the_image_that_carries_it(tmp_path):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _NotFound)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % srv.server_address[1]
    script = INSTALL_SH.read_text().replace("@@BASE_URL@@", base)
    try:
        out = subprocess.run(["sh"], input=script, capture_output=True, text=True, timeout=30,
                             env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"})
    finally:
        srv.shutdown()
    assert out.returncode != 0
    assert "server/Dockerfile" in out.stderr, out.stderr
    assert not (tmp_path / ".local").exists(), "a failed download installs nothing"
