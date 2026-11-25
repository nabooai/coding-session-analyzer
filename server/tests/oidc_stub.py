"""An in-process OpenID provider for the tests: discovery, a JWKS, an authorize endpoint that sends
the browser straight back with a code, and a token endpoint that checks PKCE and returns a signed
id_token. What the id_token says is set per test on the instance (see reset).

Run it on its own for a browser walk:  uv run python -m tests.oidc_stub 9411
"""
import base64
import hashlib
import json
import secrets
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

_KEY = rsa.generate_private_key(65537, 2048)              # one per process: a keypair per test is slow


def _b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


class OidcStub:
    def __init__(self, port=0, host="127.0.0.1"):
        self.reset()
        self.codes = {}
        self.token_requests = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, obj, status=200):
                body = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                u = urllib.parse.urlsplit(self.path)
                qs = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
                if u.path == "/.well-known/openid-configuration":
                    return self._json({"issuer": stub.discovery_issuer or stub.issuer,
                                       "authorization_endpoint": stub.issuer + "/authorize",
                                       "token_endpoint": stub.issuer + "/token",
                                       "jwks_uri": stub.issuer + "/jwks",
                                       "token_endpoint_auth_methods_supported": ["none", "client_secret_post"]})
                if u.path == "/jwks":
                    pub = _KEY.public_key().public_numbers()
                    return self._json({"keys": [{"kty": "RSA", "kid": "test", "alg": "RS256", "use": "sig",
                                                 "n": _b64(pub.n.to_bytes(256, "big")),
                                                 "e": _b64(pub.e.to_bytes(3, "big"))}]})
                if u.path == "/authorize":
                    code = "code-" + secrets.token_hex(8)
                    stub.codes[code] = qs
                    self.send_response(302)
                    self.send_header("Location", qs["redirect_uri"] + "?" + urllib.parse.urlencode(
                        {"code": code, "state": qs["state"]}))
                    self.end_headers()
                    return
                self.send_response(404)
                self.end_headers()

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                form = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(n).decode()).items()}
                stub.token_requests.append(form)
                if self.path != "/token":
                    return self._json({"error": "not found"}, 404)
                if stub.token_status != 200:
                    return self._json({"error": "server_error"}, stub.token_status)
                req = stub.codes.pop(form.get("code"), None)
                challenge = _b64(hashlib.sha256(form.get("code_verifier", "").encode()).digest())
                if req is None or req.get("code_challenge") != challenge:
                    return self._json({"error": "invalid_grant"}, 400)
                return self._json({"id_token": stub.id_token(req["client_id"], req["nonce"]),
                                   "access_token": "at", "token_type": "Bearer"})

        self.server = ThreadingHTTPServer((host, port), Handler)
        self.issuer = "http://%s:%d" % (host, self.server.server_address[1])
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def reset(self):
        """The claims every test starts from: a verified alice@acme.test, no hd, sane aud/iss/nonce."""
        self.email = "alice@acme.test"
        self.email_verified = True                       # None omits the claim (Entra sends none)
        self.hd = None                                   # None omits the claim (a consumer Google account)
        self.groups = None                               # None omits the claim (no Okta group rule)
        self.aud = None                                  # None = the client_id the browser sent
        self.iss = None                                  # None = this issuer
        self.nonce = None                                # None = the nonce the browser sent
        self.discovery_issuer = None                     # None = this issuer
        self.token_status = 200
        self.exp_offset = 3600

    def id_token(self, client_id, nonce):
        now = int(time.time())
        claims = {"iss": self.iss or self.issuer, "aud": self.aud or client_id, "sub": "u1",
                  "nonce": self.nonce or nonce, "iat": now, "exp": now + self.exp_offset}
        if self.email is not None:
            claims["email"] = self.email
        if self.email_verified is not None:
            claims["email_verified"] = self.email_verified
        if self.hd:
            claims["hd"] = self.hd
        if self.groups is not None:
            claims["groups"] = self.groups
        header = _b64(json.dumps({"alg": "RS256", "kid": "test", "typ": "JWT"}).encode())
        payload = _b64(json.dumps(claims).encode())
        sig = _KEY.sign(f"{header}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256())
        return f"{header}.{payload}.{_b64(sig)}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


if __name__ == "__main__":
    stub = OidcStub(int(sys.argv[1]) if len(sys.argv) > 1 else 0)
    if len(sys.argv) > 2:
        stub.email = sys.argv[2]
    print("issuer:", stub.issuer, "signs in", stub.email, flush=True)
    threading.Event().wait()
