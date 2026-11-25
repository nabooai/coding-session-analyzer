"""The Claude Code plugin's Python (csa-plugin/scripts), as Claude Code runs it: real processes, a temp
HOME and a stub server. Stdlib plus pytest only, so CI can run this file under Python 3.9 too:
    uvx --python 3.9 pytest --noconftest server/tests/test_plugin_client.py
"""
import ast
import filecmp
import hashlib
import importlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "csa-plugin" / "scripts"
CSA = SCRIPTS / "csa.py"
sys.path.append(str(SCRIPTS))
sys.dont_write_bytecode = True


def _load_plugin_modules():
    """server/csa.py already owns the name `csa` in this process: load the plugin's beside it."""
    spec = importlib.util.spec_from_file_location("csa", CSA)
    module = importlib.util.module_from_spec(spec)
    server_csa = sys.modules.get("csa")
    sys.modules["csa"] = module
    try:
        spec.loader.exec_module(module)
        return module, importlib.import_module("csa_commands")
    finally:
        if server_csa is None:
            del sys.modules["csa"]
        else:
            sys.modules["csa"] = server_csa


csa, csa_commands = _load_plugin_modules()
redact = importlib.import_module("redact")

SID = "11111111-2222-3333-4444-555555555555"
FAKE_KEY = "sk-" + "abcdEFGH1234567890ijklmnop"


class Stub:
    """A server that records every request and answers from `routes`: path -> (status, json body)."""

    def __init__(self):
        self.requests = []
        self.routes = {"/api/ingest": (200, {"ok": True}), "/api/client-config": (200, {"ok": True, "archive_days": 0, "directives": []}),
                       "/api/client-config/ack": (200, {"ok": True}), "/api/ask": (200, {"ok": True, "found": False})}
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                stub.requests.append({"method": self.command, "path": self.path, "headers": dict(self.headers), "raw": raw})
                status, body = stub.routes.get(self.path, (404, {"ok": False}))
                data = json.dumps(body).encode()
                self.send_response(status)
                if status in (301, 302, 307):
                    self.send_header("Location", "http://127.0.0.1:1/stolen")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _serve

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def paths(self):
        return [r["path"] for r in self.requests]

    def body(self, path):
        return [json.loads(r["raw"]) for r in self.requests if r["path"] == path]


@pytest.fixture()
def stub():
    s = Stub()
    yield s
    s.server.shutdown()
    s.server.server_close()


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / ".claude" / "projects" / "-work-repo").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    return h


@pytest.fixture(autouse=True)
def no_settle(monkeypatch):
    """In this process only: a hook's real child still waits, and wait_for covers it."""
    monkeypatch.setattr(csa, "SETTLE_SECONDS", 0)


def connect_config(home, server, shipping="plugin", **over):
    d = home / ".csa"
    d.mkdir(mode=0o700, exist_ok=True)
    cfg = {"server": server, "identity": "sam@acme.test", "auth_token": "csa_tok", "shipping": shipping,
           "sweep_from": "2000-01-01T00:00:00Z", "redaction": {"mode": "secrets"},
           "consent": {"policy_version": 2, "accepted_at": "2026-01-01T00:00:00Z", "method": "browser",
                       "identity_at_consent": "sam@acme.test", "archive_days_at_consent": 0}}
    cfg.update(over)
    (d / "config.json").write_text(json.dumps(cfg))
    return cfg


def transcript(home, sid=SID, text="hello from the session\n", project="-work-repo"):
    p = home / ".claude" / "projects" / project / (sid + ".jsonl")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def run_hook(home, cmd, event=None):
    r = subprocess.run([sys.executable, "-I", "-S", "-B", str(CSA), cmd], input=json.dumps(event or {}).encode(),
                       capture_output=True, env=dict(os.environ, HOME=str(home)), timeout=30)
    assert r.returncode == 0, r.stderr
    return r.stdout.decode()


def wait_for(cond, seconds=15):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


def settled(home):
    """No detached child still working: claims released and the log quiet for a moment."""
    time.sleep(0.4)
    assert wait_for(lambda: not list((home / ".csa").glob("shipped/*.inflight")))


# --- what a stranger can check --------------------------------------------------------------------

def test_the_redactor_is_a_byte_copy_of_the_servers():
    assert filecmp.cmp(SCRIPTS / "redact.py", REPO / "server" / "analyzer" / "redact.py", shallow=False)


def test_every_redaction_sample_through_the_copy():
    for c in json.loads((REPO / "docs" / "redaction_samples.json").read_text())["cases"]:
        out, found = redact.redact(c["input"], c.get("mode", "secrets"), c.get("deny") or (), c.get("allow") or ())
        for bad in c.get("must_redact", []):
            assert bad not in out, c["name"]
        for good in c.get("must_keep", []):
            assert good in out, c["name"]
        assert set(found) == set(c.get("labels", [])), c["name"]


def test_the_policy_version_is_one_number_in_three_places():
    go = (REPO / "client" / "config.go").read_text()
    required = int(re.search(r"const requiredPolicyVersion = (\d+)", go).group(1))
    server = (REPO / "server" / "analyzer" / "cliauth.py").read_text()
    assert required == int(re.search(r"^CONSENT_POLICY = (\d+)", server, re.M).group(1))
    assert required == csa.REQUIRED_POLICY


def test_the_version_in_code_is_the_manifests():
    manifest = json.loads((REPO / "csa-plugin" / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["version"] == csa.VERSION
    assert manifest["repository"] == "https://github.com/nabooai/coding-session-analyzer"


def test_the_marketplace_points_at_the_readable_plugin():
    marketplace = json.loads((REPO / ".claude-plugin" / "marketplace.json").read_text())
    assert marketplace["name"] == "csa"
    assert marketplace["plugins"] == [{
        "name": "csa",
        "source": "./csa-plugin",
        "description": "Team memory for Claude Code in readable Python with no compiled binary. Installs disabled. "
                       "Read it, enable it, then run /csa:connect.",
    }]


def test_the_plugin_installs_disabled_and_contains_no_binary_or_symlink():
    assert json.loads((REPO / "csa-plugin" / ".claude-plugin" / "plugin.json").read_text())["defaultEnabled"] is False
    for path in (REPO / "csa-plugin").rglob("*"):
        if "__pycache__" in path.parts:
            continue
        assert not path.is_symlink(), path
        if path.is_file():
            assert path.suffix.lower() not in {".exe", ".dll", ".dylib", ".so", ".a", ".o", ".pyc"}, path


@pytest.mark.parametrize("name", ["csa.py", "csa_commands.py", "redact.py"])
def test_python_39_syntax(name):
    ast.parse((SCRIPTS / name).read_text(), feature_version=(3, 9))


@pytest.mark.parametrize("name", ["csa.py", "csa_commands.py"])
def test_nothing_fetches_code(name):
    assert not re.search(r"git |pip |curl |urlretrieve", (SCRIPTS / name).read_text()), name


def test_one_network_call_site_and_the_size_limit():
    sources = [(SCRIPTS / n).read_text() for n in ("csa.py", "csa_commands.py")]
    assert sum(s.count("urlopen(") for s in sources) == 1
    assert sources[0].count("\n") <= 800
    assert not any(re.search(r"\bCSA_[A-Z]", s) for s in sources), "no new environment variables"


def test_every_skill_stays_out_of_the_models_reach():
    skills = list((REPO / "csa-plugin" / "skills").glob("*/SKILL.md"))
    assert {p.parent.name for p in skills} == {"connect", "disconnect", "status", "preview", "send-session", "sent",
                                               "scope", "forget", "team"}
    for p in skills:
        text = p.read_text()
        assert "disable-model-invocation: true" in text, p
        for line in text.splitlines():
            if line.startswith("!`"):
                assert "$ARGUMENTS" not in line, "an argument must not reach a shell: " + str(p)


def test_hooks_start_no_python_without_a_config_and_never_block(home, tmp_path):
    hooks = json.loads((REPO / "csa-plugin" / "hooks" / "hooks.json").read_text())["hooks"]
    assert set(hooks) == {"UserPromptSubmit", "SessionStart", "SessionEnd"}
    log = tmp_path / "python.log"
    shim = tmp_path / "bin"
    shim.mkdir()
    (shim / "python3").write_text('#!/bin/sh\necho "$*" >> "%s"\nexit 2\n' % log)
    (shim / "python3").chmod(0o755)
    env = dict(os.environ, HOME=str(home), CLAUDE_PLUGIN_ROOT=str(REPO / "csa-plugin"), PATH="%s:/usr/bin:/bin" % shim)
    for event, entries in hooks.items():
        cmd = entries[0]["hooks"][0]["command"]
        r = subprocess.run(["sh", "-c", cmd], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        assert r.returncode == 0 and r.stdout == b"", event
    assert not log.exists(), "no config means no Python"
    connect_config(home, "http://127.0.0.1:1")
    for entries in hooks.values():
        cmd = entries[0]["hooks"][0]["command"]
        assert subprocess.run(["sh", "-c", cmd], env=env).returncode == 0, "a crash must not block the prompt"
        gone = dict(env, CLAUDE_PLUGIN_ROOT=str(tmp_path / "removed"))
        assert subprocess.run(["sh", "-c", cmd], env=gone).returncode == 0, "nor a removed cache"
    assert log.read_text().count("\n") == 3


# --- the shared gates ------------------------------------------------------------------------------

def test_gates_from_the_shared_fixture():
    s = json.loads((REPO / "docs" / "hook_gate_samples.json").read_text())
    for p in s["looks_secret"]["yes"]:
        assert csa.looks_secret(p), p
    for p in s["looks_secret"]["no"]:
        assert not csa.looks_secret(p), p
    for p in s["should_recall"]["yes"]:
        assert csa.should_recall(p), p
    for p in s["should_recall"]["no"]:
        assert not csa.should_recall(p), p
    for c in s["ambient_query"]:
        assert csa.ambient_query(c["in"]) == c["out"], c
    for c in s["ignored_by"]:
        assert bool(csa.ignored_by(c["path"], [c["pattern"]])) is c["match"], c


def test_a_home_path_holding_the_word_does_not_widen_only(home):
    connect_config(home, "http://x")
    (home / ".csa" / "only").write_text("proj*\n")
    assert csa.excluded_reason("/Users/project/.claude/projects/-other/a.jsonl")
    (home / ".csa" / "only").write_text("# comment\n\n-other\n")
    assert csa.excluded_reason("/Users/project/.claude/projects/-other/a.jsonl") == ""
    (home / ".csa" / "ignore").write_text("-other\n")
    assert "ignore" in csa.excluded_reason("/Users/project/.claude/projects/-other/a.jsonl")


# --- config ------------------------------------------------------------------------------------------

def test_config_round_trip_keeps_unknown_keys_and_stays_private(home):
    connect_config(home, "http://x", history_only=["a"], future_field={"k": 1})
    csa.update_config(server_archive_days=9)
    path = home / ".csa" / "config.json"
    cfg = json.loads(path.read_text())
    assert cfg["history_only"] == ["a"] and cfg["future_field"] == {"k": 1} and cfg["server_archive_days"] == 9
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [p.name for p in (home / ".csa").iterdir()] == ["config.json"], "no temp file left"


def test_a_crash_mid_write_leaves_the_old_config(home, monkeypatch):
    connect_config(home, "http://old")
    monkeypatch.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        csa.update_config(server="http://new")
    monkeypatch.undo()
    monkeypatch.setenv("HOME", str(home))
    assert json.loads((home / ".csa" / "config.json").read_text())["server"] == "http://old"
    assert [p.name for p in (home / ".csa").iterdir()] == ["config.json"]


def test_a_detached_write_does_not_bring_a_deleted_config_back(home):
    connect_config(home, "http://x")
    shutil.rmtree(home / ".csa")
    csa.update_config(server_archive_days=9)
    csa.note_auth(401)
    csa.note_failure("tls: nope")
    assert not (home / ".csa").exists()


@pytest.mark.parametrize("hook", ["recall", "ship", "sweep"])
def test_no_shipping_field_is_inert(home, stub, hook):
    cfg = connect_config(home, stub.url)
    del cfg["shipping"]
    (home / ".csa" / "config.json").write_text(json.dumps(cfg))
    transcript(home)
    out = run_hook(home, hook, {"session_id": SID, "transcript_path": str(transcript(home)),
                                "prompt": "what did we decide about the release rollback plan"})
    time.sleep(0.5)
    assert stub.requests == []
    if hook == "recall":
        assert "/csa:connect" in json.loads(out)["systemMessage"]
    else:
        assert out == ""


def test_a_voided_policy_stops_everything_and_says_why(home, stub):
    connect_config(home, stub.url, consent={"policy_version": 1})
    p = transcript(home)
    out = run_hook(home, "recall", {"session_id": SID, "prompt": "what did we decide about the release rollback plan"})
    assert "older policy" in json.loads(out)["systemMessage"] and "/csa:connect" in json.loads(out)["systemMessage"]
    assert run_hook(home, "recall", {"prompt": "what did we decide about the release rollback plan"}) == "", "hourly"
    run_hook(home, "ship", {"session_id": SID, "transcript_path": str(p)})
    run_hook(home, "sweep", {"session_id": "other"})
    time.sleep(0.5)
    assert stub.requests == []


# --- send: every outcome -----------------------------------------------------------------------------

@pytest.mark.parametrize("status,body,outcome,retried", [
    (200, {"ok": True}, "sent", False),
    (200, {"ok": False, "error": "session not found"}, "rejected", False),
    (401, {"ok": False}, "unauthorized", True),
    (413, {"ok": False}, "too_large", False),
    (429, {"ok": False}, "down", True),
    (502, {"ok": False}, "down", True),
    (200, [], "down", True),
    (302, {}, "down", True),
])
def test_send_outcomes(home, stub, status, body, outcome, retried):
    stub.routes["/api/ingest"] = (status, body)
    cfg = connect_config(home, stub.url)
    p = transcript(home)
    assert csa.ship_one(cfg, SID, str(p)) == outcome
    assert (home / ".csa" / "shipped" / SID).exists() is (not retried), "a marker only when retrying cannot help"
    assert not (home / ".csa" / "shipped" / (SID + ".inflight")).exists()
    assert (home / ".csa" / "auth_broken").exists() is (status == 401)
    assert (home / ".csa" / "oversize").exists() is (status == 413)
    assert stub.paths() == ["/api/ingest"], "a redirect is not followed"
    assert stub.requests[0]["headers"]["User-Agent"] == "csa-plugin/" + csa.VERSION
    assert stub.requests[0]["headers"]["Authorization"] == "Bearer csa_tok"


def test_a_transport_failure_is_down(home):
    cfg = connect_config(home, "http://127.0.0.1:1")
    assert csa.ship_one(cfg, SID, str(transcript(home))) == "down"
    assert not (home / ".csa" / "shipped" / SID).exists()


def test_an_untrusted_certificate_is_marked_with_the_advice(home, tmp_path):
    ssl = pytest.importorskip("ssl")
    if not shutil.which("openssl"):
        pytest.skip("no openssl")
    key, crt = tmp_path / "k.pem", tmp_path / "c.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(crt),
                    "-days", "1", "-subj", "/CN=localhost"], check=True, capture_output=True)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(crt, key)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cfg = connect_config(home, "https://localhost:%d" % srv.server_address[1])
    assert csa.ship_one(cfg, SID, str(transcript(home))) == "down"
    assert "SSL_CERT_FILE" in (home / ".csa" / "tls_broken").read_text()
    out = run_hook(home, "recall", {"prompt": "what did we decide about the release rollback plan"})
    assert "SSL_CERT_FILE" in json.loads(out)["systemMessage"]
    srv.shutdown()
    srv.server_close()


def test_the_encoded_size_is_checked_after_redaction(home, stub, monkeypatch):
    monkeypatch.setattr(csa, "MAX_BODY", 1000)
    cfg = connect_config(home, stub.url)
    p = transcript(home, text='"' * 600)          # under the raw limit, over it once JSON-escaped
    assert csa.ship_one(cfg, SID, str(p)) == "too_large"
    assert stub.requests == []
    assert (home / ".csa" / "shipped" / SID).read_text() == "600"
    assert SID in (home / ".csa" / "oversize").read_text()


def test_the_raw_size_skips_before_reading(home, stub, monkeypatch):
    monkeypatch.setattr(csa, "MAX_BODY", 10)
    monkeypatch.setattr(csa, "redacted_transcript", lambda *a: pytest.fail("read an oversize transcript"))
    assert csa.ship_one(connect_config(home, stub.url), SID, str(transcript(home))) == "too_large"


def test_a_planted_key_never_reaches_the_body_and_the_ledger_matches_it(home, stub):
    cfg = connect_config(home, stub.url)
    p = transcript(home, text="key %s and bytes \xff end\n" % FAKE_KEY)
    assert csa.ship_one(cfg, SID, str(p)) == "sent"
    raw = stub.requests[0]["raw"]
    assert FAKE_KEY.encode() not in raw
    body = json.loads(raw)
    assert set(body) == {"session_id", "content", "redaction_mode"} and "[REDACTED:openai_key]" in body["content"]
    row = json.loads((home / ".csa" / "sent.jsonl").read_text())
    assert row["sha256"] == hashlib.sha256(body["content"].encode()).hexdigest()
    assert row["labels"] == ["openai_key"] and row["sid"] == SID and row["server"] == stub.url and row["ok"] is True
    assert stat.S_IMODE((home / ".csa" / "sent.jsonl").stat().st_mode) == 0o600


def test_deny_and_a_stricter_mode_apply(home, stub):
    cfg = connect_config(home, stub.url, redaction={"mode": "privacy"})
    (home / ".csa" / "redact.deny").write_text("Bluefin\n")
    csa.ship_one(cfg, SID, str(transcript(home, text="Bluefin ships, mail sam@acme.test\n")))
    body = stub.body("/api/ingest")[0]
    assert "Bluefin" not in body["content"] and "sam@acme.test" not in body["content"]
    assert body["redaction_mode"] == "privacy"


def test_it_ships_only_when_grown(home, stub):
    cfg = connect_config(home, stub.url)
    p = transcript(home)
    assert csa.ship_one(cfg, SID, str(p)) == "sent"
    assert csa.ship_one(cfg, SID, str(p)) == "skipped"
    p.write_text(p.read_text() + "more\n")
    assert csa.ship_one(cfg, SID, str(p)) == "sent"


def test_the_marker_and_claim_are_csa_clients(home, stub):
    cfg = connect_config(home, stub.url)
    p = transcript(home)
    (home / ".csa" / "shipped").mkdir(mode=0o700)
    (home / ".csa" / "shipped" / SID).write_text(str(p.stat().st_size))
    assert csa.ship_one(cfg, SID, str(p)) == "skipped", "a size marker csa-client wrote counts"
    (home / ".csa" / "shipped" / SID).unlink()
    inflight = home / ".csa" / "shipped" / (SID + ".inflight")
    inflight.write_text("")
    assert csa.ship_one(cfg, SID, str(p)) == "claimed" and stub.requests == []
    old = time.time() - csa.CLAIM_TTL - 5
    os.utime(inflight, (old, old))
    assert csa.ship_one(cfg, SID, str(p)) == "sent", "a killed child's claim is stolen"
    assert not inflight.exists()


def test_a_claim_race_has_one_winner(home):
    connect_config(home, "http://x")
    (home / ".csa" / "shipped").mkdir(mode=0o700)
    results = []
    threads = [threading.Thread(target=lambda: results.append(csa.claim_ship(SID))) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len([r for r in results if r]) == 1


def test_a_hostile_session_id_touches_nothing(home, stub):
    cfg = connect_config(home, stub.url)
    assert csa.ship_one(cfg, "../../evil", str(transcript(home))) == "skipped"
    assert stub.requests == [] and not (home / "evil").exists()


# --- the hooks, as Claude Code runs them -------------------------------------------------------------

def test_ship_prints_nothing_and_ships_detached(home, stub):
    connect_config(home, stub.url)
    p = transcript(home, text="key %s\n" % FAKE_KEY)
    assert run_hook(home, "ship", {"session_id": SID, "transcript_path": str(p), "reason": "exit"}) == ""
    assert wait_for(lambda: "/api/ingest" in stub.paths())
    settled(home)
    assert "sent %s ok=true" % SID in (home / ".csa" / "ship.log").read_text()


def test_a_session_end_and_the_next_start_ship_it_once(home, stub):
    """/clear: SessionEnd for the old session, SessionStart for the new one, and Claude Code appends to the old
    transcript just after the hook fires. Without the settle the sweep sees growth and POSTs it a second time."""
    connect_config(home, stub.url)
    p = transcript(home, "cleared-session")
    run_hook(home, "ship", {"session_id": "cleared-session", "transcript_path": str(p), "reason": "clear"})
    run_hook(home, "sweep", {"session_id": "the-new-session", "source": "clear"})
    with p.open("a") as f:
        f.write('{"type":"summary"}\n')
    assert wait_for(lambda: "/api/ingest" in stub.paths())
    time.sleep(csa.SETTLE_SECONDS + 3 if csa.SETTLE_SECONDS else 3)
    settled(home)
    assert [b["session_id"] for b in stub.body("/api/ingest")] == ["cleared-session"]
    assert "summary" in stub.body("/api/ingest")[0]["content"], "the settle is what lets the POST carry the last lines"


def test_the_event_is_read_without_waiting_for_eof(home, stub):
    connect_config(home, stub.url)
    p = transcript(home)
    proc = subprocess.Popen([sys.executable, "-I", "-S", "-B", str(CSA), "ship"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, env=dict(os.environ, HOME=str(home)))
    proc.stdin.write(json.dumps({"session_id": SID, "transcript_path": str(p)}).encode())
    proc.stdin.flush()                         # the pipe stays open, as Claude Code may leave it
    assert proc.wait(timeout=5) == 0
    proc.stdin.close()
    proc.stdout.close()
    assert wait_for(lambda: "/api/ingest" in stub.paths())


def test_recall_sends_the_redacted_query_and_returns_the_context(home, stub):
    connect_config(home, stub.url)
    stub.routes["/api/ask"] = (200, {"ok": True, "found": True, "context": "CSA line", "system_message": "CSA recalled"})
    out = json.loads(run_hook(home, "recall", {"session_id": SID, "cwd": "/work/repo", "prompt_id": "x",
                                               "prompt": "what did we decide about the rollback %s plan" % FAKE_KEY}))
    assert out["systemMessage"] == "CSA recalled"
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "CSA line" in out["hookSpecificOutput"]["additionalContext"]
    assert FAKE_KEY.encode() not in stub.requests[0]["raw"]
    body = stub.body("/api/ask")[0]
    assert set(body) == {"query", "session", "asked_by", "cwd", "k"} and body["k"] == 6 and body["session"] == SID


def test_an_excluded_project_keeps_its_prompts_home(home, stub):
    connect_config(home, stub.url)
    (home / ".csa" / "ignore").write_text("-work-repo\n")
    q = {"prompt": "what did we decide about the release rollback plan", "transcript_path": str(transcript(home))}
    assert run_hook(home, "recall", q) == "" and stub.requests == []
    (home / ".csa" / "ignore").unlink()
    run_hook(home, "recall", q)
    assert stub.paths() == ["/api/ask"]


def test_recall_honours_deny_and_mode(home, stub):
    connect_config(home, stub.url, redaction={"mode": "privacy"})
    (home / ".csa" / "redact.deny").write_text("Bluefin\n")
    run_hook(home, "recall", {"prompt": "what did we decide about Bluefin and sam@acme.test rollout"})
    assert "Bluefin" not in stub.body("/api/ask")[0]["query"] and "sam@acme.test" not in stub.body("/api/ask")[0]["query"]


@pytest.mark.parametrize("prompt", ["ok", "/clear the current session please", "<system-reminder>one two three four</system-reminder>",
                                    "my password is hunter2hunter2 for the staging box"])
def test_recall_stays_home_on_the_gates(home, stub, prompt):
    connect_config(home, stub.url)
    assert run_hook(home, "recall", {"prompt": prompt}) == ""
    assert stub.requests == []


def test_recall_has_a_total_budget(home):
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(5)                                # accepts, never answers
    connect_config(home, "http://127.0.0.1:%d" % s.getsockname()[1])
    t0 = time.monotonic()
    r = subprocess.run([sys.executable, "-I", "-S", "-B", str(CSA), "recall"],
                       input=json.dumps({"prompt": "what did we decide about the release rollback plan"}).encode(),
                       stdout=subprocess.PIPE, env=dict(os.environ, HOME=str(home)), timeout=10)
    s.close()
    assert time.monotonic() - t0 < 2.5 and r.stdout == b""


def test_a_401_surfaces_on_the_next_prompt_then_hourly_then_clears(home, stub):
    connect_config(home, stub.url)
    stub.routes["/api/ask"] = (401, {"ok": False})
    q = {"prompt": "what did we decide about the release rollback plan"}
    assert run_hook(home, "recall", q) == ""
    assert (home / ".csa" / "auth_broken").exists()
    assert "/csa:connect" in json.loads(run_hook(home, "recall", q))["systemMessage"]
    assert run_hook(home, "recall", q) == ""
    stub.routes["/api/ask"] = (200, {"ok": True, "found": False})
    run_hook(home, "recall", q)
    assert not (home / ".csa" / "auth_broken").exists()


def test_the_connected_notice_shows_once(home, stub):
    connect_config(home, stub.url, shipping="plugin-manual")
    (home / ".csa" / "connected_notice").write_text("CSA connected as sam")
    assert json.loads(run_hook(home, "recall", {}))["systemMessage"] == "CSA connected as sam"
    assert run_hook(home, "recall", {}) == ""


# --- sweep --------------------------------------------------------------------------------------------

def test_sweep_floor_current_subagents_and_chunk(home, stub):
    floor = "2026-06-01T00:00:00Z"
    connect_config(home, stub.url, sweep_from=floor)
    t0 = 1780272000                            # the floor as epoch seconds
    old = transcript(home, "old-session")
    os.utime(old, (t0 - 3600, t0 - 3600))
    transcript(home, "current-session")
    sub = home / ".claude" / "projects" / "-work-repo" / "parent" / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-a.jsonl").write_text("sub\n")
    for i in range(csa.SWEEP_CHUNK + 2):
        transcript(home, "s%02d" % i)
    assert run_hook(home, "sweep", {"session_id": "current-session", "source": "startup"}) == ""
    assert wait_for(lambda: len(stub.body("/api/ingest")) >= csa.SWEEP_CHUNK)
    settled(home)
    ids = {b["session_id"] for b in stub.body("/api/ingest")}
    assert len(ids) == csa.SWEEP_CHUNK and "old-session" not in ids and "current-session" not in ids
    assert not any(i.startswith("agent") for i in ids)
    run_hook(home, "sweep", {"session_id": "current-session"})
    assert wait_for(lambda: len(stub.body("/api/ingest")) >= csa.SWEEP_CHUNK + 2), "the rest catches up next time"
    settled(home)
    assert "old-session" not in {b["session_id"] for b in stub.body("/api/ingest")}


@pytest.mark.parametrize("status", [502, 401, 429])
def test_sweep_stops_at_the_first_server_refusal(home, stub, status):
    connect_config(home, stub.url)
    stub.routes["/api/ingest"] = (status, {"ok": False})
    for i in range(5):
        transcript(home, "s%d" % i)
    csa.child_sweep("")
    assert len(stub.body("/api/ingest")) == 1


def test_a_413_does_not_stop_the_sweep(home, stub):
    connect_config(home, stub.url)
    stub.routes["/api/ingest"] = (413, {"ok": False})
    for i in range(3):
        transcript(home, "s%d" % i)
    csa.child_sweep("")
    assert len(stub.body("/api/ingest")) == 3


def test_an_unreadable_floor_sweeps_nothing(home, stub):
    connect_config(home, stub.url, sweep_from="not a date")
    transcript(home)
    csa.child_sweep("")
    assert "/api/ingest" not in stub.paths()


def test_no_floor_sweeps_nothing(home, stub):
    connect_config(home, stub.url, sweep_from="")
    transcript(home)
    csa.child_sweep("")
    assert "/api/ingest" not in stub.paths()


def test_retention_pause_and_unpause(home, stub):
    connect_config(home, stub.url)
    p = transcript(home)
    stub.routes["/api/client-config"] = (200, {"ok": True, "archive_days": 30, "directives": []})
    csa.child_sweep("")
    assert "/api/ingest" not in stub.paths()
    assert json.loads((home / ".csa" / "config.json").read_text())["server_archive_days"] == 30
    csa.child_ship(SID, str(p))
    assert "/api/ingest" not in stub.paths(), "SessionEnd honours the cached window"
    assert "30 days" in json.loads(run_hook(home, "recall", {}))["systemMessage"]
    stub.routes["/api/client-config"] = (200, {"ok": True, "archive_days": 0, "directives": []})
    csa.child_sweep("")
    assert "/api/ingest" in stub.paths(), "lowering it needs nobody's permission"


def test_forever_exceeds_any_number_and_only_forever_covers_it():
    cfg = {"consent": {"archive_days_at_consent": 30}, "server_archive_days": -1}
    assert csa.retention_exceeds_consent(cfg)
    cfg["consent"]["archive_days_at_consent"] = -1
    assert not csa.retention_exceeds_consent(cfg)


def test_a_session_begun_before_connecting_stays_here_unless_sent_by_name(home, stub, capsys):
    connect_config(home, stub.url, sweep_from="2026-06-01T00:00:00Z", shipping="plugin")
    line = json.dumps({"type": "user", "timestamp": "2026-05-31T23:59:00.000Z", "message": {"role": "user", "content": "before"}})
    p = transcript(home, text="not json\n" + line + "\n")
    assert csa.started_before_floor(csa.load_config(), str(p))
    csa.child_ship(SID, str(p))
    csa.child_sweep("")
    assert "/api/ingest" not in stub.paths()
    assert "began before you connected" in capsys.readouterr().err
    assert csa_commands.cmd_send_session(["--session", SID]) == 0, "the person can still send it themselves"
    assert stub.paths().count("/api/ingest") == 1
    assert not csa.started_before_floor(csa.load_config(), str(transcript(home, "new", json.dumps(
        {"timestamp": "2026-06-02T00:00:00Z"}) + "\n")))
    assert not csa.started_before_floor(csa.load_config(), str(transcript(home, "stamped-nowhere", "x\n")))


def test_a_send_that_did_not_happen_keeps_the_forget(home, stub, capsys):
    stub.routes["/api/forget"] = (200, {"ok": True})
    cfg = connect_config(home, stub.url)
    p = transcript(home)
    csa.ship_one(cfg, SID, str(p))
    csa_commands.cmd_forget([SID])
    p.write_text(p.read_text() + "more\n")
    (home / ".csa" / "ignore").write_text("-work-repo\n")
    assert csa_commands.cmd_send_session(["--session", SID]) == 1
    (home / ".csa" / "ignore").unlink()
    stub.routes["/api/ingest"] = (502, {"ok": False})
    assert csa_commands.cmd_send_session(["--session", SID]) == 1
    stub.routes["/api/ingest"] = (200, {"ok": True})
    assert csa.ship_one(cfg, SID, str(p)) == "forgotten", "a refused send did not undo the forget"
    assert (home / ".csa" / "shipped" / SID).exists()


def test_the_floor_check_reads_past_lines_with_no_timestamp(home, stub):
    connect_config(home, stub.url, sweep_from="2026-06-01T00:00:00Z")
    lines = '{"type":"meta"}\n' * 40 + json.dumps({"timestamp": "2020-01-01T00:00:00Z"}) + "\n"
    assert csa.started_before_floor(csa.load_config(), str(transcript(home, text=lines)))


def test_a_forgotten_session_does_not_come_back_on_its_own(home, stub, capsys, monkeypatch):
    monkeypatch.setattr(csa_commands, "run_claude", lambda *a: True)
    stub.routes["/api/forget"] = (200, {"ok": True})
    stub.routes["/api/token/revoke"] = (200, {"ok": True, "revoked": True})
    cfg = connect_config(home, stub.url)
    p = transcript(home)
    assert csa.ship_one(cfg, SID, str(p)) == "sent"
    assert csa_commands.cmd_forget([SID]) == 0
    p.write_text(p.read_text() + "more\n")
    assert csa.ship_one(cfg, SID, str(p)) == "forgotten"
    csa_commands.cmd_sent([])
    assert "(forgotten)" in capsys.readouterr().out
    assert csa_commands.cmd_send_session(["--session", SID]) == 0, "sending it by name is the person's decision"
    assert stub.paths().count("/api/ingest") == 2
    assert csa_commands.cmd_forget([SID]) == 0
    capsys.readouterr()
    assert csa_commands.cmd_disconnect([]) == 0
    assert SID not in capsys.readouterr().out, "a forgotten session is not listed as staying on the server"


def test_the_ledger_and_the_preview_count_the_same_bytes(home, stub, capsys):
    cfg = connect_config(home, stub.url)
    p = transcript(home, text='{"a":"quote \\" and caf\u00e9"}\n')
    csa_commands.cmd_preview(["--session", SID, "--out", str(home / "p.txt")])
    printed = re.search(r"bytes\s+(\d+)", capsys.readouterr().out).group(1)
    csa.ship_one(cfg, SID, str(p))
    assert json.loads((home / ".csa" / "sent.jsonl").read_text())["bytes"] == int(printed)


def test_send_session_lists_recent_sessions(home, capsys):
    transcript(home, "older-session")
    assert csa_commands.cmd_send_session(["--list"]) == 0
    out = capsys.readouterr().out
    assert "older-session" in out and "-work-repo" in out


def test_the_recipe_needs_only_csa_py_and_redact_py(home, tmp_path):
    (tmp_path / "s").mkdir()
    for name in ("csa.py", "redact.py"):
        shutil.copy(SCRIPTS / name, tmp_path / "s" / name)
    r = subprocess.run([sys.executable, str(tmp_path / "s" / "csa.py"), "redact"], input=FAKE_KEY.encode(), capture_output=True,
                       env=dict(os.environ, HOME=str(home)))
    assert r.returncode == 0 and r.stdout == b"[REDACTED:openai_key]", r.stderr


def test_a_directive_is_acked_declined_and_ships_nothing_older(home, stub):
    connect_config(home, stub.url, sweep_from="2999-01-01T00:00:00Z")
    transcript(home)
    stub.routes["/api/client-config"] = (200, {"ok": True, "archive_days": 0,
                                               "directives": [{"id": "abc", "since": ""}]})
    csa.child_sweep("")
    assert stub.body("/api/client-config/ack") == [{"id": "abc", "status": "declined", "shipped": 0,
                                                    "detail": "history sharing is off on this machine"}]
    assert "/api/ingest" not in stub.paths()


# --- manual mode --------------------------------------------------------------------------------------

@pytest.mark.parametrize("hook,event", [("recall", {"prompt": "what did we decide about the release rollback plan"}),
                                        ("ship", {"session_id": SID}), ("sweep", {"session_id": SID})])
def test_manual_hooks_send_nothing(home, stub, hook, event):
    connect_config(home, stub.url, shipping="plugin-manual")
    event = dict(event, transcript_path=str(transcript(home)))
    assert run_hook(home, hook, event) == ""
    time.sleep(0.5)
    assert stub.requests == []
    assert not (home / ".csa" / "ship.log").exists(), "no child was started"


def test_send_session_is_the_only_thing_that_leaves(home, stub, capsys):
    connect_config(home, stub.url, shipping="plugin-manual")
    p = transcript(home, text="key %s\n" % FAKE_KEY)
    assert csa_commands.cmd_send_session(["--session", SID]) == 0
    out = capsys.readouterr().out
    assert stub.paths() == ["/api/client-config", "/api/ingest"]
    assert FAKE_KEY not in out and "hello" not in out and "openai_key" in out and stub.url in out
    assert csa_commands.cmd_send_session(["--session", SID]) == 0
    assert "Nothing new" in capsys.readouterr().out
    assert stub.paths().count("/api/ingest") == 1
    p.write_text(p.read_text() + "more\n")
    assert csa_commands.cmd_send_session(["--session", SID]) == 0
    assert stub.paths().count("/api/ingest") == 2


def test_send_session_checks_retention_at_send_time(home, stub, capsys):
    connect_config(home, stub.url, shipping="plugin-manual")
    transcript(home)
    stub.routes["/api/client-config"] = (200, {"ok": True, "archive_days": 30, "directives": [{"id": "abc"}]})
    assert csa_commands.cmd_send_session(["--session", SID]) == 1
    assert "/api/ingest" not in stub.paths() and "30 days" in capsys.readouterr().out
    assert "/api/client-config/ack" not in stub.paths()


def test_send_session_refuses_without_consent_or_on_ambiguity_or_exclusion(home, stub, capsys):
    connect_config(home, stub.url, shipping="plugin-manual", consent={"policy_version": 1})
    transcript(home)
    assert csa_commands.cmd_send_session(["--session", SID]) == 1
    connect_config(home, stub.url, shipping="plugin-manual")
    transcript(home, project="-work-other")
    assert csa_commands.cmd_send_session(["--session", SID]) == 1, "two transcripts share the id"
    (home / ".claude" / "projects" / "-work-other" / (SID + ".jsonl")).unlink()
    (home / ".csa" / "ignore").write_text("-work-repo\n")
    assert csa_commands.cmd_send_session(["--session", SID]) == 1
    assert "/api/ingest" not in stub.paths()


# --- preview -------------------------------------------------------------------------------------------

def test_preview_is_the_body_and_touches_no_network(home, stub, capsys, tmp_path):
    cfg = connect_config(home, stub.url)
    p = transcript(home, text="key %s\n" % FAKE_KEY)
    out = tmp_path / "preview.txt"
    assert csa_commands.cmd_preview(["--session", SID, "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert stub.requests == [] and FAKE_KEY not in printed and "openai_key: 1" in printed
    assert csa.ship_one(cfg, SID, str(p)) == "sent"
    body = stub.body("/api/ingest")[0]
    assert out.read_text() == body["content"]
    sha = hashlib.sha256(body["content"].encode()).hexdigest()
    assert sha in printed and json.loads((home / ".csa" / "sent.jsonl").read_text())["sha256"] == sha


def test_preview_works_before_connecting(home, capsys):
    transcript(home, text="key %s\n" % FAKE_KEY)
    assert csa_commands.cmd_preview(["--session", SID]) == 0
    assert not (home / ".csa" / "config.json").exists()
    written = home / ".csa" / "preview" / (SID + ".txt")
    assert FAKE_KEY not in written.read_text() and stat.S_IMODE(written.stat().st_mode) == 0o600


# --- both clients --------------------------------------------------------------------------------------

@pytest.mark.parametrize("command,args", [("/x/.csa/bin/csa-client", ["hook"]), ('"/my home/csa-client" ship', None), ("csa-client ship", None)])
def test_csa_client_hooks_are_a_conflict_in_every_form(home, command, args):
    hook = {"type": "command", "command": command}
    if args:
        hook["args"] = args
    (home / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"SessionEnd": [{"hooks": [hook]}]}}))
    assert "csa-client hooks" in csa.conflict()


@pytest.mark.parametrize("command", ["/opt/acme-csa-client", "/bin/true", "python3 csa-client.py"])
def test_another_tool_is_not_a_conflict(home, command):
    (home / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"SessionEnd": [{"hooks": [{"command": command}]}]}}))
    assert csa.conflict() == ""


@pytest.mark.parametrize("path", ["Library/LaunchAgents/com.example.csa-client.plist", ".config/systemd/user/csa-client.service"])
def test_a_watcher_is_a_conflict(home, path):
    (home / path).parent.mkdir(parents=True)
    (home / path).write_text("")
    assert "watcher" in csa.conflict()


def test_a_broken_settings_file_is_not_a_crash(home):
    (home / ".claude" / "settings.json").write_text("{not json")
    assert csa.conflict() == ""


def test_conflict_stands_down_and_warns_hourly(home, stub, capsys):
    connect_config(home, stub.url)
    p = transcript(home)
    (home / ".claude" / "settings.json").write_text(json.dumps(
        {"hooks": {"SessionEnd": [{"hooks": [{"command": "/x/csa-client", "args": ["ship"]}]}]}}))
    q = {"session_id": SID, "transcript_path": str(p), "prompt": "what did we decide about the release rollback plan"}
    message = json.loads(run_hook(home, "recall", q))["systemMessage"]
    assert "csa-client uninstall" in message and "claude plugin disable csa@csa" in message
    assert run_hook(home, "recall", q) == ""
    run_hook(home, "ship", q)
    run_hook(home, "sweep", q)
    time.sleep(0.5)
    assert stub.requests == []
    assert csa_commands.cmd_connect([stub.url]) == 1
    assert csa_commands.cmd_send_session(["--session", SID]) == 1
    assert stub.requests == []


# --- connect and disconnect -----------------------------------------------------------------------------

def connect_routes(stub, **new):
    stub.routes["/api/cli-auth/new"] = (200, dict({"ok": True, "nonce": "secret-nonce", "code": "ORBIT-EMBER",
                                                   "consent_policy": 2}, **new))


@pytest.mark.parametrize("new,says", [({"consent_policy": None}, "too old"), ({"consent_policy": 1}, "too old"),
                                      ({"consent_policy": 3}, "Update the plugin")])
def test_a_server_on_another_policy_is_refused_before_any_browser(home, stub, capsys, monkeypatch, new, says):
    connect_routes(stub, **new)
    monkeypatch.setattr(csa_commands, "open_browser", lambda url: pytest.fail("opened a browser"))
    assert csa_commands.cmd_connect([stub.url]) == 1
    assert says in capsys.readouterr().out and not (home / ".csa").exists()


def test_a_server_with_no_such_route_is_too_old_and_leaves_nothing(home, stub, capsys, monkeypatch):
    monkeypatch.setattr(csa_commands, "open_browser", lambda url: pytest.fail("opened a browser"))
    assert csa_commands.cmd_connect([stub.url]) == 1
    assert "too old" in capsys.readouterr().out and not (home / ".csa").exists()


def test_an_unreachable_server_leaves_nothing(home, capsys, monkeypatch):
    monkeypatch.setattr(csa_commands, "open_browser", lambda url: pytest.fail("opened a browser"))
    assert csa_commands.cmd_connect(["http://127.0.0.1:1"]) == 1
    assert "Nothing was connected" in capsys.readouterr().out and not (home / ".csa").exists()


def test_a_timed_out_connect_leaves_no_trace(home, stub, monkeypatch, capsys):
    monkeypatch.setattr(csa_commands, "POLL_SECONDS", 0)
    (home / ".csa").mkdir(mode=0o700)
    (home / ".csa" / "ship.log").write_text("")
    (home / ".csa" / "connect.pending").write_text(json.dumps({"nonce": "n", "server": stub.url, "shipping": "plugin"}))
    assert csa_commands.child_poll() == 1
    assert not (home / ".csa").exists()


def test_an_mcp_endpoint_on_another_host_is_not_registered(home, stub, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(csa_commands, "run_claude", lambda *a: calls.append(a) or True)
    stub.routes["/api/health"] = (200, {"ok": True, "mcp_url": "https://elsewhere.example/mcp"})
    csa_commands.register_mcp(connect_config(home, stub.url))
    assert calls == [] and "elsewhere.example" in capsys.readouterr().out
    stub.routes["/api/health"] = (200, {"ok": True, "mcp_port": 58768, "mcp_path": "/mcp"})
    csa_commands.register_mcp(connect_config(home, stub.url))
    assert any(a[:2] == ("mcp", "add") and "http://127.0.0.1:58768/mcp" in a for a in calls)


def test_the_nonce_is_never_on_stdout_off_a_terminal(home, stub, capsys, monkeypatch):
    connect_routes(stub)
    opened = []
    monkeypatch.setattr(csa_commands, "open_browser", lambda url: opened.append(url) or True)
    monkeypatch.setattr(csa_commands, "detach", lambda *a: opened.append(a))
    monkeypatch.setattr(csa_commands, "on_terminal", lambda: False)
    assert csa_commands.cmd_connect([stub.url, "--manual"]) == 0
    out = capsys.readouterr().out
    assert "secret-nonce" not in out and "ORBIT-EMBER" in out
    assert opened[0].startswith(stub.url + "/cli-auth?nonce=secret-nonce") and opened[1] == ("_poll",)
    assert stub.body("/api/cli-auth/new")[0]["shipping"] == "plugin-manual"
    pending = home / ".csa" / "connect.pending"
    assert stat.S_IMODE(pending.stat().st_mode) == 0o600 and not (home / ".csa" / "config.json").exists()


def test_no_browser_off_a_terminal_prints_the_terminal_form_and_no_secret(home, stub, capsys, monkeypatch):
    connect_routes(stub)
    monkeypatch.setattr(csa_commands, "open_browser", lambda url: False)
    monkeypatch.setattr(csa_commands, "on_terminal", lambda: False)
    assert csa_commands.cmd_connect([stub.url]) == 1
    out = capsys.readouterr().out
    assert "secret-nonce" not in out and "connect " + stub.url in out
    assert not (home / ".csa" / "connect.pending").exists()


@pytest.mark.parametrize("url", ["", "file:///etc/passwd", "ftp://x", "http://user:pw@x", "not a url"])
def test_a_bad_server_url_is_refused(home, url, capsys):
    assert csa_commands.cmd_connect([url] if url else []) == 2
    assert not (home / ".csa").exists()


def test_cloudflare_credentials_keep_csa_client(home, stub, capsys):
    connect_routes(stub)
    connect_config(home, stub.url, cf_access_client_id="x")
    assert csa_commands.cmd_connect([stub.url]) == 1
    assert stub.requests == []


def test_a_claim_records_consent_and_keeps_what_this_machine_chose(home, stub, monkeypatch, capsys):
    monkeypatch.setattr(csa_commands, "run_claude", lambda *a: True)
    connect_config(home, stub.url, redaction={"mode": "privacy"}, history_only=["keep"], auth_token="csa_old", shipping="plugin")
    (home / ".csa" / "only").write_text("-work-repo\n")
    pending = {"nonce": "n", "server": stub.url, "shipping": "plugin"}
    stub.routes["/api/token/revoke"] = (200, {"ok": True, "revoked": True})
    stub.routes["/api/health"] = (200, {"ok": True, "mcp_url": stub.url + "/mcp"})
    (home / ".csa" / "connect.pending").write_text(json.dumps(pending))
    assert csa_commands.finish_connect(pending, {"token": "csa_new", "email": "sam@acme.test", "shipping": "plugin", "consent_policy": 2,
                                                 "policy": {"redaction_min": "secrets", "archive_days": 30}}) == 0
    cfg = json.loads((home / ".csa" / "config.json").read_text())
    assert cfg["auth_token"] == "csa_new" and cfg["identity"] == "sam@acme.test" and cfg["shipping"] == "plugin"
    assert cfg["redaction"]["mode"] == "privacy", "never downgraded"
    assert cfg["history_only"] == ["keep"] and (home / ".csa" / "only").exists()
    consent = cfg["consent"]
    assert consent["method"] == "browser" and consent["policy_version"] == 2 and consent["archive_days_at_consent"] == 30
    assert cfg["sweep_from"] == "2000-01-01T00:00:00Z", "a re-sign-in keeps the queue"
    assert [r["headers"]["Authorization"] for r in stub.requests if r["path"] == "/api/token/revoke"] == ["Bearer csa_old"]
    assert not (home / ".csa" / "connect.pending").exists()
    assert "sam@acme.test" in (home / ".csa" / "connected_notice").read_text()


def test_a_first_connect_floors_at_now(home, stub, monkeypatch, capsys):
    monkeypatch.setattr(csa_commands, "run_claude", lambda *a: True)
    (home / ".csa").mkdir(mode=0o700)
    pending = {"nonce": "n", "server": stub.url, "shipping": "plugin-manual"}
    csa_commands.finish_connect(pending, {"token": "t", "email": "e@x", "shipping": "plugin-manual", "consent_policy": 2, "policy": {}})
    cfg = json.loads((home / ".csa" / "config.json").read_text())
    assert time.time() - csa.parse_floor(cfg["sweep_from"]) < 60
    assert stat.S_IMODE((home / ".csa" / "config.json").stat().st_mode) == 0o600


def test_manual_to_automatic_gets_a_fresh_floor(home, stub, monkeypatch, capsys):
    """Sessions manual mode kept home were never consented to ship."""
    monkeypatch.setattr(csa_commands, "run_claude", lambda *a: True)
    stub.routes["/api/token/revoke"] = (200, {"ok": True, "revoked": True})
    connect_config(home, stub.url, shipping="plugin-manual")
    pending = {"nonce": "n", "server": stub.url, "shipping": "plugin"}
    csa_commands.finish_connect(pending, {"token": "t", "email": "e@x", "shipping": "plugin", "consent_policy": 2, "policy": {}})
    assert time.time() - csa.parse_floor(json.loads((home / ".csa" / "config.json").read_text())["sweep_from"]) < 60


def test_connecting_elsewhere_revokes_the_old_credential_and_refloors(home, stub, monkeypatch, capsys):
    monkeypatch.setattr(csa_commands, "run_claude", lambda *a: True)
    other = Stub()
    other.routes["/api/token/revoke"] = (200, {"ok": True, "revoked": True})
    connect_config(home, other.url, auth_token="csa_on_the_other")
    pending = {"nonce": "n", "server": stub.url, "shipping": "plugin"}
    csa_commands.finish_connect(pending, {"token": "t", "email": "e@x", "shipping": "plugin", "consent_policy": 2, "policy": {}})
    other.server.shutdown()
    other.server.server_close()
    assert [r["headers"]["Authorization"] for r in other.requests] == ["Bearer csa_on_the_other"]
    assert time.time() - csa.parse_floor(json.loads((home / ".csa" / "config.json").read_text())["sweep_from"]) < 60


def test_csa_client_consent_gets_a_fresh_floor_even_with_its_token_kept(home, stub, monkeypatch, capsys):
    monkeypatch.setattr(csa_commands, "run_claude", lambda *a: True)
    stub.routes["/api/token/revoke"] = (200, {"ok": True, "revoked": True})
    cfg = connect_config(home, stub.url)
    del cfg["shipping"]
    (home / ".csa" / "config.json").write_text(json.dumps(cfg))
    pending = {"nonce": "n", "server": stub.url, "shipping": "plugin"}
    csa_commands.finish_connect(pending, {"token": "t", "email": "e@x", "shipping": "plugin", "consent_policy": 2, "policy": {}})
    assert time.time() - csa.parse_floor(json.loads((home / ".csa" / "config.json").read_text())["sweep_from"]) < 60


def test_a_claim_for_another_mode_is_revoked(home, stub, capsys):
    stub.routes["/api/token/revoke"] = (200, {"ok": True, "revoked": True})
    (home / ".csa").mkdir(mode=0o700)
    pending = {"nonce": "n", "server": stub.url, "shipping": "plugin-manual"}
    assert csa_commands.finish_connect(pending, {"token": "t", "email": "e@x", "shipping": "plugin", "consent_policy": 2, "policy": {}}) == 1
    assert "/api/token/revoke" in stub.paths() and not (home / ".csa" / "config.json").exists()


def test_a_superseded_poll_gives_up(home, stub, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    (home / ".csa").mkdir(mode=0o700)
    (home / ".csa" / "connect.pending").write_text(json.dumps({"nonce": "a", "server": stub.url, "shipping": "plugin"}))
    real = csa_commands.read_text
    seen = []

    def changes(path):
        seen.append(path)
        return json.dumps({"nonce": "b"}) if len(seen) > 1 else real(path)

    monkeypatch.setattr(csa_commands, "read_text", changes)
    assert csa_commands.child_poll() == 1
    assert stub.requests == []


@pytest.mark.parametrize("status,reply,says", [(200, {"ok": True, "revoked": True}, "Credential revoked"), (401, {"ok": False}, "already dead"),
                                               (200, {"ok": True, "revoked": False}, "no per-user credential"),
                                               (404, {"ok": False}, "NOT revoke")])
def test_disconnect_revokes_and_removes_everything(home, stub, capsys, monkeypatch, status, reply, says):
    calls = []
    monkeypatch.setattr(csa_commands, "run_claude", lambda *a: calls.append(a) or True)
    stub.routes["/api/token/revoke"] = (status, reply)
    connect_config(home, stub.url)
    assert csa_commands.cmd_disconnect([]) == 0
    out = capsys.readouterr().out
    assert says in out and not (home / ".csa").exists() and "claude plugin uninstall csa@csa" in out
    assert ("mcp", "remove", "csa", "-s", "user") in calls


def test_a_refused_revoke_removes_nothing(home, stub, capsys, monkeypatch):
    monkeypatch.setattr(csa_commands, "run_claude", lambda *a: pytest.fail("ran claude"))
    stub.routes["/api/token/revoke"] = (409, {"ok": False, "error": "this is the workspace's only admin credential."})
    connect_config(home, stub.url)
    assert csa_commands.cmd_disconnect([]) == 1
    assert "only admin credential" in capsys.readouterr().out and (home / ".csa" / "config.json").exists()


def test_disconnect_survives_a_half_written_ledger(home, stub, capsys, monkeypatch):
    monkeypatch.setattr(csa_commands, "run_claude", lambda *a: True)
    stub.routes["/api/token/revoke"] = (200, {"ok": True, "revoked": True})
    connect_config(home, stub.url)
    (home / ".csa" / "sent.jsonl").write_text('{"sid":"a-session"}\n{"sid":"cut of')
    assert csa_commands.cmd_disconnect([]) == 0
    assert "a-session" in capsys.readouterr().out and not (home / ".csa").exists()


def test_disconnect_with_the_server_gone_still_removes_and_says_so(home, capsys, monkeypatch):
    monkeypatch.setattr(csa_commands, "run_claude", lambda *a: True)
    connect_config(home, "http://127.0.0.1:1")
    assert csa_commands.cmd_disconnect([]) == 0
    assert "NOT revoke" in capsys.readouterr().out and not (home / ".csa").exists()


def test_disconnect_will_not_touch_a_csa_client_config(home, stub, capsys, monkeypatch):
    monkeypatch.setattr(csa_commands, "run_claude", lambda *a: pytest.fail("ran claude"))
    cfg = connect_config(home, stub.url)
    del cfg["shipping"]
    (home / ".csa" / "config.json").write_text(json.dumps(cfg))
    assert csa_commands.cmd_disconnect([]) == 1
    assert (home / ".csa" / "config.json").exists() and stub.requests == []


def test_disconnect_names_what_stays_on_the_server(home, stub, capsys, monkeypatch):
    monkeypatch.setattr(csa_commands, "run_claude", lambda *a: True)
    stub.routes["/api/token/revoke"] = (200, {"ok": True, "revoked": True})
    cfg = connect_config(home, stub.url)
    csa.ship_one(cfg, SID, str(transcript(home)))
    assert csa_commands.cmd_disconnect([]) == 0
    assert SID in capsys.readouterr().out


def test_disconnect_with_nothing_connected_prints_the_steps(home, capsys):
    assert csa_commands.cmd_disconnect([]) == 0
    assert "claude plugin marketplace remove csa" in capsys.readouterr().out


# --- the rest of what a person runs -----------------------------------------------------------------------

def test_scope_writes_atomically_and_refuses_a_bad_glob(home, capsys):
    assert csa_commands.cmd_scope(["only", "*repo*"]) == 0
    assert csa_commands.cmd_scope(["only", "*other*"]) == 0
    assert (home / ".csa" / "only").read_text() == "*repo*\n*other*\n"
    assert csa_commands.cmd_scope(["ignore", "["]) == 2
    assert not (home / ".csa" / "ignore").exists()
    assert csa_commands.cmd_scope([]) == 0
    assert "-work-repo" in capsys.readouterr().out
    assert csa_commands.cmd_scope(["clear", "only"]) == 0
    assert not (home / ".csa" / "only").exists()


def test_forget_needs_clean_ids_and_reports_each(home, stub, capsys):
    connect_config(home, stub.url)
    stub.routes["/api/forget"] = (200, {"ok": True})
    assert csa_commands.cmd_forget(["../evil"]) == 2
    assert stub.requests == []
    assert csa_commands.cmd_forget([SID]) == 0
    assert stub.body("/api/forget") == [{"session_id": SID, "owner": "sam@acme.test"}]
    stub.routes["/api/forget"] = (200, {"ok": False, "error": "not yours"})
    assert csa_commands.cmd_forget([SID]) == 1
    assert "not yours" in capsys.readouterr().out


def test_status_names_the_cause_and_the_fix(home, stub, capsys):
    assert csa_commands.cmd_status([]) == 0
    assert "Not connected" in capsys.readouterr().out
    connect_config(home, stub.url)
    stub.routes["/api/me"] = (401, {"ok": False})
    csa_commands.cmd_status([])
    out = capsys.readouterr().out
    assert csa.VERSION in out and csa.RELEASES in out and "/csa:connect" in out
    stub.routes["/api/me"] = (200, {"ok": True})
    csa_commands.cmd_status([])
    assert "credential accepted" in capsys.readouterr().out
    connect_config(home, "http://127.0.0.1:1")
    csa_commands.cmd_status([])
    assert "unreachable" in capsys.readouterr().out


def test_the_redact_filter_honours_the_deny_list(home):
    connect_config(home, "http://x")
    (home / ".csa" / "redact.deny").write_text("Bluefin\n")
    r = subprocess.run([sys.executable, "-I", "-S", "-B", str(CSA), "redact"], input=("Bluefin %s\n" % FAKE_KEY).encode(),
                       capture_output=True, env=dict(os.environ, HOME=str(home)))
    assert r.stdout == b"[REDACTED:denylisted] [REDACTED:openai_key]\n"
    assert b"openai_key" in r.stderr


def test_a_planted_module_beside_the_cwd_loses(home, tmp_path):
    connect_config(home, "http://127.0.0.1:1")
    (tmp_path / "json.py").write_text("raise SystemExit('planted json ran')\n")
    r = subprocess.run([sys.executable, "-I", "-S", "-B", str(CSA), "status"], cwd=str(tmp_path), capture_output=True,
                       env=dict(os.environ, HOME=str(home)))
    assert r.returncode == 0, r.stderr


def test_no_pyc_lands_beside_the_scripts(home, tmp_path):
    copy = tmp_path / "scripts"
    shutil.copytree(SCRIPTS, copy, ignore=shutil.ignore_patterns("__pycache__"))
    connect_config(home, "http://127.0.0.1:1")
    transcript(home)
    for cmd in ("status", "recall", "sweep", "preview"):
        subprocess.run([sys.executable, "-I", "-S", "-B", str(copy / "csa.py"), cmd], input=b"{}", stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, env=dict(os.environ, HOME=str(home)))
    assert not (copy / "__pycache__").exists(), "the cache must stay byte-identical to the tag"
