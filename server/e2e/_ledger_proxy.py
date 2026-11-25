"""A reverse proxy that writes every request it forwards to a JSONL ledger, so a harness can read what
actually crossed the wire.   _ledger_proxy.py <port> <upstream http://host:port> <ledger path>"""
import http.client
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

PORT, UPSTREAM, LEDGER = int(sys.argv[1]), urlsplit(sys.argv[2]), sys.argv[3]


class Handler(BaseHTTPRequestHandler):
    def _forward(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        with open(LEDGER, "a", encoding="utf-8") as f:
            f.write(json.dumps({"method": self.command, "path": self.path, "ua": self.headers.get("User-Agent"),
                                "body": body.decode("utf-8", "replace")}) + "\n")
        con = http.client.HTTPConnection(UPSTREAM.hostname, UPSTREAM.port, timeout=60)
        headers = {k: v for k, v in self.headers.items() if k.lower() != "host"}
        con.request(self.command, self.path, body=body or None, headers=headers)
        resp = con.getresponse()
        data = resp.read()
        self.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() not in ("transfer-encoding", "connection", "content-length", "date", "server"):
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        con.close()

    do_GET = do_POST = _forward

    def log_message(self, *args):
        pass


ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
