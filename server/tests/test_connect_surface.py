"""What #/connect reads: your own machines, and the two facts the install line needs.

Over real HTTP, so the gate is what answers. The page exists to get a machine shipping, so the
questions here are the ones a person on it actually asks: is my laptop connected, and is the command
this server printed one that this server will honour.
"""
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

import apiserver
from analyzer import auth, orgs


class _Headers:
    """Just enough of a request for _base_url, which reads only the Host header."""
    def __init__(self, d):
        self.headers = auth._CIHeaders(d)

    _base_url = apiserver.Handler._base_url


@pytest.fixture()
def http(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CSA_AUTH", "org")
    monkeypatch.setenv("CSA_PUBLIC_URL", "https://csa.example.com")
    monkeypatch.delenv("CSA_AUTH_TOKEN", raising=False)
    cc = orgs.control_db()
    orgs._create_org("Acme", org_id="acme", con=cc)
    orgs._create_org("Rival", org_id="rival", con=cc)
    cc.commit()
    tokens = {
        "alice": orgs.mint_token("alice@acme.com", "acme", hostname="alice-mbp", con=cc),
        "alice2": orgs.mint_token("alice@acme.com", "acme", hostname="alice-desktop", con=cc),
        "aliceweb": orgs.mint_token("alice@acme.com", "acme", hostname="web-sso", con=cc),
        "bob": orgs.mint_token("bob@acme.com", "acme", hostname="bob-x1", con=cc),
        "zed": orgs.mint_token("zed@rival.com", "rival", hostname="zed-box", con=cc),
    }
    srv = ThreadingHTTPServer(("127.0.0.1", 0), apiserver.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % srv.server_address[1]

    def call(path, who=None):
        req = urllib.request.Request(base + path)
        if who:
            req.add_header("Authorization", "Bearer " + tokens.get(who, who))
        try:
            with urllib.request.urlopen(req) as resp:
                body = resp.read()
                return resp.status, json.loads(body) if resp.headers.get_content_type() == "application/json" else body
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    yield call, tokens
    srv.shutdown()
    srv.server_close()


def test_machines_are_scoped_to_the_caller_and_need_no_admin_role(http):
    """Team cannot answer "did it work" for the person who just ran setup: /api/team is a rollup of
    people who have SHIPPED, and the machines column is admin-only. This route is why the page can."""
    call, _ = http
    code, r = call("/api/machines", "alice")
    assert code == 200 and r["ok"], r
    hosts = sorted(m["hostname"] for m in r["machines"])
    assert hosts == ["alice-desktop", "alice-mbp"], hosts
    assert all("token_hash" not in m for m in r["machines"]), "a revoke handle is the admin plane's"

    assert [m["hostname"] for m in call("/api/machines", "bob")[1]["machines"]] == ["bob-x1"], \
        "a member sees their own machine without being an admin"
    assert [m["hostname"] for m in call("/api/machines", "zed")[1]["machines"]] == ["zed-box"]


def test_a_browser_sign_in_is_not_a_machine(http):
    """web / web-sso are how a dashboard sign-in is recorded. Listing them would tell someone their
    machine is connected when only their browser is."""
    call, _ = http
    hosts = [m["hostname"] for m in call("/api/machines", "aliceweb")[1]["machines"]]
    assert "web-sso" not in hosts and sorted(hosts) == ["alice-desktop", "alice-mbp"], hosts


def test_a_revoked_machine_leaves_the_list(http):
    call, tokens = http
    orgs.revoke(tokens["alice2"])
    assert [m["hostname"] for m in call("/api/machines", "alice")[1]["machines"]] == ["alice-mbp"]


def test_machines_needs_a_credential(http):
    call, _ = http
    assert call("/api/machines")[0] == 401


def test_auth_config_hands_out_a_base_the_installer_will_honour(http):
    """The page must never print `curl <base>/install | sh` for a base /install then refuses with
    421. Both answers come from the same _base_url(), so they cannot disagree."""
    call, _ = http
    code, cfg = call("/api/auth-config")
    assert code == 200 and cfg["base_url"] == "https://csa.example.com", cfg


def test_a_non_loopback_host_with_no_public_url_gets_no_installer_line(http, monkeypatch):
    """/install answers 421 rather than baking a Host-derived (attacker-controllable) download URL,
    so base_url is null there too and #/connect renders the refusal, not a one-liner that hands `sh`
    an error body. Over loopback, the dev path, Host is trusted and both still agree."""
    call, _ = http
    monkeypatch.delenv("CSA_PUBLIC_URL", raising=False)
    assert call("/api/auth-config")[1]["base_url"].startswith("http://127.0.0.1"), "loopback dev"

    forged = _Headers({"Host": "evil.example.com"})
    assert forged._base_url() is None, "a non-loopback Host with no CSA_PUBLIC_URL names no base"


def test_client_published_is_the_date_the_binaries_landed_here(http, tmp_path, monkeypatch):
    call, _ = http
    dist = tmp_path / "client-dist"
    monkeypatch.setattr(apiserver, "CLIENT_DIST", str(dist))   # a box that publishes nothing
    assert call("/api/auth-config")[1]["client_published"] is None, "no release dir: say nothing"

    dist.mkdir()
    (dist / "checksums.txt").write_text("%s  csa-client-linux-amd64\n" % ("a" * 64))
    monkeypatch.setattr(apiserver, "CLIENT_DIST", str(dist))
    published = call("/api/auth-config")[1]["client_published"]
    assert published and len(published) == 10 and published.count("-") == 2, published

    # Not cached: an operator drops new files into a bind-mount, and a stale answer would outlive them.
    (dist / "checksums.txt").write_text("%s  csa-client-linux-amd64\n" % ("b" * 64))
    import os
    import time
    old = time.time() - 86400 * 3
    os.utime(dist / "checksums.txt", (old, old))
    assert call("/api/auth-config")[1]["client_published"] != published, "re-read, never memoised"


def test_every_download_row_the_page_renders_is_a_link_that_serves(http, tmp_path, monkeypatch):
    """#/connect renders one row per asset in connect.ts's ASSETS, each linking /latest/<name>. A row
    whose href 404s is a dead link on the one page that exists to hand out a binary."""
    call, _ = http
    dist = tmp_path / "client-dist"
    dist.mkdir()
    names = ["csa-client-darwin-arm64", "csa-client-darwin-amd64", "csa-client-linux-amd64",
             "csa-client-linux-arm64", "csa-client-windows-amd64.exe"]
    for n in names:
        (dist / n).write_bytes(b"binary")
    (dist / "checksums.txt").write_text("".join("%s  %s\n" % ("a" * 64, n) for n in names))
    monkeypatch.setattr(apiserver, "CLIENT_DIST", str(dist))
    for n in names + ["checksums.txt"]:
        assert apiserver._ASSET_RE.match(n), n
        assert call("/latest/" + n)[0] == 200, n


def test_org_id_never_comes_from_the_request(http):
    """The isolation boundary: alice's token names acme, so no header can make it list rival's."""
    call, _ = http
    code, r = call("/api/machines", "alice")
    assert code == 200
    assert not any(m["hostname"] == "zed-box" for m in r["machines"]), r
