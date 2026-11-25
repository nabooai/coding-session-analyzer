"""An AI Models endpoint is typed by an admin, so it must not become a way to reach the network the
server sits in: refused at save, and again at connect, against the address actually dialled."""
import socket
import urllib.request

import pytest

from analyzer import netguard, providers

PUBLIC = "93.184.216.34"
ANTHROPIC = {"kind": "anthropic", "name": "A", "credential": "k", "models": ["m"]}


def _resolves(monkeypatch, *answers):
    seen = []

    def resolve(host, port):
        seen.append(host)
        return list(answers[min(len(seen), len(answers)) - 1])
    monkeypatch.setattr(netguard, "resolve", resolve)
    return seen


@pytest.mark.parametrize("address", [
    "127.0.0.1", "::1", "10.1.2.3", "172.16.0.9", "192.168.1.5", "169.254.169.254", "fd00:ec2::254",
    "fe80::1", "100.64.0.1", "0.0.0.0", "::ffff:127.0.0.1", "::ffff:169.254.169.254", "64:ff9b::7f00:1",
    "2002:7f00:1::1"])
def test_internal_addresses_are_refused_at_save(monkeypatch, address):
    _resolves(monkeypatch, [address])
    with pytest.raises(ValueError, match="not a public address"):
        providers.checked(ANTHROPIC | {"endpoint": "https://gateway.example.com"})


def test_a_name_that_resolves_to_the_metadata_service_is_refused(monkeypatch):
    _resolves(monkeypatch, ["169.254.169.254"])
    with pytest.raises(ValueError, match="169.254.169.254"):
        providers.checked(ANTHROPIC | {"endpoint": "https://metadata.google.internal/computeMetadata"})


def test_one_internal_answer_among_public_ones_is_enough_to_refuse(monkeypatch):
    _resolves(monkeypatch, [PUBLIC, "10.0.0.8"])
    with pytest.raises(ValueError, match="10.0.0.8"):
        providers.checked(ANTHROPIC | {"endpoint": "https://gateway.example.com"})


@pytest.mark.parametrize("url", ["http://gateway.example.com", "https://u:p@gateway.example.com", "https://",
                                 "ftp://gateway.example.com"])
def test_only_plain_https_without_credentials(monkeypatch, url):
    _resolves(monkeypatch, [PUBLIC])
    with pytest.raises(ValueError):
        netguard.check_url(url)


def test_an_unresolvable_name_says_so(monkeypatch):
    def gone(host, port):
        raise socket.gaierror
    monkeypatch.setattr(netguard, "resolve", gone)
    with pytest.raises(ValueError, match="did not resolve"):
        providers.checked(ANTHROPIC | {"endpoint": "https://nope.example.com"})


def test_a_public_endpoint_is_accepted(monkeypatch):
    _resolves(monkeypatch, [PUBLIC])
    assert providers.checked(ANTHROPIC | {"endpoint": "https://gateway.example.com/v1/"}).endpoint == \
        "https://gateway.example.com/v1"


def test_a_name_that_turns_internal_after_the_save_is_refused_at_call_time(monkeypatch):
    _resolves(monkeypatch, [PUBLIC], ["169.254.169.254"])
    providers.checked(ANTHROPIC | {"endpoint": "https://rebind.example.com"})
    dialled = []
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: dialled.append(a) or pytest.fail("dialled"))
    with pytest.raises(ValueError, match="not a public address"):
        netguard.opener().open(urllib.request.Request("https://rebind.example.com/v1/messages"), timeout=1)
    assert dialled == []


def test_the_connection_goes_to_the_address_that_was_checked(monkeypatch):
    seen = _resolves(monkeypatch, [PUBLIC], ["10.0.0.1"])
    dialled = []

    def create(address, *a, **k):
        dialled.append(address)
        raise ConnectionRefusedError
    monkeypatch.setattr(socket, "create_connection", create)
    with pytest.raises(OSError):
        netguard.opener().open(urllib.request.Request("https://gateway.example.com:8443/x"), timeout=1)
    assert dialled == [(PUBLIC, 8443)] and len(seen) == 1, "resolved once, then dialled by IP"


def test_the_opener_has_no_plain_http_no_proxy_and_no_redirects():
    kinds = {type(h).__name__ for h in netguard.opener().handlers}
    assert not kinds & {"HTTPHandler", "ProxyHandler", "HTTPRedirectHandler"}


def test_an_error_status_raises_like_urlopen_does(monkeypatch):
    import http.server
    import threading
    import urllib.error

    class Reply(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(self.server.status)
            self.send_header("Location", "https://elsewhere.example.com/")
            self.end_headers()

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), Reply)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(netguard._Connection, "connect", lambda self: setattr(
        self, "sock", socket.create_connection(("127.0.0.1", srv.server_port))))
    monkeypatch.setattr(netguard._Connection, "_tunnel_host", None, raising=False)
    for status in (401, 307):
        srv.status = status
        with pytest.raises(urllib.error.HTTPError) as e:
            netguard.opener().open(urllib.request.Request("https://gateway.example.com/", data=b"{}"), timeout=2)
        assert e.value.code == status
    srv.shutdown()


def test_the_refusal_names_an_address_once_and_points_at_the_gateway_secret(monkeypatch):
    _resolves(monkeypatch, ["10.1.2.3"])
    with pytest.raises(ValueError) as literal:
        netguard.public_address("10.1.2.3")
    assert str(literal.value).count("10.1.2.3") == 1
    with pytest.raises(ValueError) as named:
        netguard.public_address("gateway.corp")
    assert "gateway.corp points at 10.1.2.3" in str(named.value)
    for e in (literal.value, named.value):
        assert "ANTHROPIC_BASE_URL in the csa-llm Secret" in str(e)
