"""The server-side pieces of the on-prem compose: `csa.py migrate`, the one-shot service api, mcp
and etl wait for; the front door gate, where the public landing and its anonymous request form exist
only on a server that announces requests somewhere; and the image version plus schema heads that
`/api/health` and `csa.py status` report so an upgrade can be verified.
"""
import json
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

import apiserver
import csa
from analyzer import migrations, orgs, tenant
from tests import legacy_ddl


def _pre_alembic_box(root):
    """A box the last pre-Alembic image left behind: control.db with an org, a default corpus with a
    session, a telemetry sibling, no version table anywhere."""
    data = root / "data"
    data.mkdir()
    con = sqlite3.connect(data / "control.db")
    con.executescript(legacy_ddl.JUL_2026_CONTROL)
    con.execute("INSERT INTO orgs(id,name,created) VALUES('acme','Acme','2026-07-01T00:00:00Z')")
    con.commit()
    con.close()
    con = sqlite3.connect(data / "analyzer.db")
    con.executescript(legacy_ddl.JUL_2026_CORPUS)
    con.execute("INSERT INTO sessions(session_id,project,user) VALUES('old-1','csa','mara@example.com')")
    con.commit()
    con.close()
    con = sqlite3.connect(data / "telemetry.db")
    con.executescript(legacy_ddl.SEP05_2026_TELEMETRY.replace("tel.", ""))
    con.commit()
    con.close()


def _version(path):
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    except sqlite3.OperationalError:
        return None
    finally:
        con.close()


def _migrate(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["csa", "migrate"])
    csa.main()


def test_migrate_brings_every_file_of_a_pre_alembic_box_to_head(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _pre_alembic_box(tmp_path)
    data = tmp_path / "data"
    assert _version(data / "control.db") is None and _version(data / "analyzer.db") is None

    _migrate(monkeypatch)

    heads = {env: migrations.head(env) for env in ("control", "corpus", "telemetry")}
    assert capsys.readouterr().out.splitlines() == [f"{env}: {rev}" for env, rev in heads.items()]
    assert _version(data / "control.db") == heads["control"]
    assert _version(data / "analyzer.db") == heads["corpus"]
    assert _version(data / "telemetry.db") == heads["telemetry"]
    assert _version(data / "orgs" / "acme" / "analyzer.db") == heads["corpus"], "every org row's corpus"
    assert _version(data / "orgs" / "acme" / "telemetry.db") == heads["telemetry"]
    con = sqlite3.connect(data / "analyzer.db")
    assert con.execute("SELECT project, user FROM sessions").fetchall() == [("csa", "mara@example.com")]
    con.close()

    _migrate(monkeypatch)                                # a second run finds every file at head
    assert capsys.readouterr().out.splitlines() == [f"{env}: {rev}" for env, rev in heads.items()]


def test_migrate_exits_non_zero_when_a_file_cannot_be_migrated(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _pre_alembic_box(tmp_path)

    def boom(con, env, path=None):
        raise RuntimeError("upgrade blew up on %s" % env)
    monkeypatch.setattr(migrations, "ensure", boom)
    with pytest.raises(SystemExit) as e:
        _migrate(monkeypatch)
    assert e.value.code == 1
    assert "migrate failed: upgrade blew up on control" in capsys.readouterr().err
    assert _version(tmp_path / "data" / "control.db") is None, "nothing was stamped"

    monkeypatch.undo()
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "control.db").write_bytes(b"not a database at all")
    with pytest.raises(SystemExit) as e:
        _migrate(monkeypatch)
    assert e.value.code == 1
    assert "migrate failed:" in capsys.readouterr().err


# --- the front door -----------------------------------------------------------------------------------

@pytest.fixture()
def box(tmp_path, monkeypatch):
    """org mode, `default` adopted as Acme, no request webhook, over real HTTP."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CSA_AUTH", "org")
    monkeypatch.setenv("CSA_PUBLIC_URL", "https://csa.example.com")
    monkeypatch.delenv("CSA_AUTH_TOKEN", raising=False)
    orgs.adopt_default_org("Acme", domain="acme.test")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), apiserver.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def _call(base, path, body=None):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"} if body is not None else {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_the_front_door_is_the_sign_in_unless_requests_have_somewhere_to_go(box, monkeypatch):
    assert orgs.is_multi_tenant() is True, "an adopted default is a tenant, and that no longer decides"
    st, cfg = _call(box, "/api/auth-config")
    assert st == 200 and cfg["workspace_requests"] is False and "multi_tenant" not in cfg
    st, body = _call(box, "/api/workspace-request", {"company": "Flow Co", "email": "dev@flow.co"})
    assert st == 404 and body == {"ok": False, "error": "not found"}
    assert orgs.list_requests(None) == [], "nothing was written"

    tenant.set_deployment(orgs.control_db(), request_webhook="http://127.0.0.1:9/hook")   # nothing listens: logged, never raised
    assert _call(box, "/api/auth-config")[1]["workspace_requests"] is True, "read per request, no restart"
    st, body = _call(box, "/api/workspace-request", {"company": "Flow Co", "email": "dev@flow.co"})
    assert (st, body) == (200, {"ok": True})
    assert [r["company"] for r in orgs.list_requests(None)] == ["Flow Co"]


# --- version + schema heads: /api/health and `csa.py status` -----------------------------------------

def test_health_reports_version_and_schema_heads_matching_migrations_head(box):
    st, body = _call(box, "/api/health")
    assert st == 200 and body["version"] == "dev", "no VERSION file -> source checkout still answers"
    assert body["schema"] == {env: migrations.head(env) for env in ("control", "corpus", "telemetry")}
    assert body["extraction"] == {"stalled": False, "reason": ""}


def test_health_and_status_report_the_same_VERSION_file_verbatim(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "VERSION").write_text("lab-9d82ae5\n")
    monkeypatch.setattr(migrations, "_ROOT", str(tmp_path))   # decoupled from INI: alembic itself is untouched

    monkeypatch.setattr(sys, "argv", ["csa", "status"])
    with pytest.raises(SystemExit) as exc:
        csa.main()
    assert exc.value.code == 0, "control plane readable -> status exits 0 so a monitor can trust it"
    heads = {env: migrations.head(env) for env in ("control", "corpus", "telemetry")}
    assert capsys.readouterr().out.splitlines() == [
        "version: lab-9d82ae5",
        f"schema.control: {heads['control']}",
        f"schema.corpus: {heads['corpus']}",
        f"schema.telemetry: {heads['telemetry']}",
        "control_plane: ok",
    ]

    srv = ThreadingHTTPServer(("127.0.0.1", 0), apiserver.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        st, body = _call("http://127.0.0.1:%d" % srv.server_address[1], "/api/health")
    finally:
        srv.shutdown()
        srv.server_close()
    assert st == 200 and body["version"] == "lab-9d82ae5" and body["schema"] == heads


def test_status_exits_non_zero_when_the_control_plane_is_unreadable(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(orgs, "control_plane_ok", lambda: False)
    monkeypatch.setattr(sys, "argv", ["csa", "status"])
    with pytest.raises(SystemExit) as exc:
        csa.main()
    assert exc.value.code == 1, "so a monitor script can alert, same contract the old ETL check had"
    assert capsys.readouterr().out.splitlines()[-1] == "control_plane: unreadable"


def test_health_answers_with_a_null_schema_instead_of_dropping_the_connection(box, monkeypatch):
    def boom():
        raise RuntimeError("schema heads blew up")
    monkeypatch.setattr(apiserver, "_schema_heads", boom)
    st, body = _call(box, "/api/health")
    assert st == 200 and body["ok"] is True and body["schema"] is None, \
        "a schema-head failure degrades one key, it must not drop the connection with no response"


_CHART = pathlib.Path(__file__).resolve().parents[1] / "helm" / "csa"
needs_helm = pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")


def _helm(*args):
    return subprocess.run(["helm", *args], capture_output=True, text=True)


@needs_helm
def test_a_packaged_chart_defaults_image_tag_to_its_app_version(tmp_path):
    pkg = _helm("package", str(_CHART), "--version", "9.9.9", "--app-version", "9.9.9", "--destination", str(tmp_path))
    assert pkg.returncode == 0, pkg.stderr
    tgz = tmp_path / "csa-9.9.9.tgz"
    rendered = _helm("template", "csa", str(tgz), "--set", "host=csa.example.com")
    assert rendered.returncode == 0, rendered.stderr
    assert 'image: "ghcr.io/nabooai/csa-server-slim:9.9.9"' in rendered.stdout
    pinned = _helm("template", "csa", str(tgz), "--set", "host=csa.example.com,image.tag=pinned")
    assert 'image: "ghcr.io/nabooai/csa-server-slim:pinned"' in pinned.stdout
    assert "csa-server-slim:9.9.9" not in pinned.stdout


@needs_helm
def test_a_helm_user_on_an_explicit_image_renders_what_they_rendered_before():
    explicit = _helm("template", "csa", str(_CHART), "--set",
                     "host=csa.example.com,image.repository=ghcr.io/nabooai/csa-server,image.tag=lab-abc")
    assert explicit.returncode == 0, explicit.stderr
    assert 'image: "ghcr.io/nabooai/csa-server:lab-abc"' in explicit.stdout
    assert "csa-server-slim" not in explicit.stdout
    assert "imagePullSecrets:\n        - name: ghcr" in explicit.stdout, "their pull secret still applies"


def test_the_compose_defaults_to_the_image_without_claude_code():
    compose = (_CHART.parents[1] / "docker-compose.trial.yml").read_text()
    assert "${CSA_SERVER_IMAGE:-ghcr.io/nabooai/csa-server-slim:latest}" in compose


def test_the_plain_build_is_the_stage_without_claude_code():
    stages = [line.split() for line in (_CHART.parents[1] / "Dockerfile").read_text().splitlines()
              if line.startswith("FROM ")]
    assert stages[-1] == ["FROM", "base", "AS", "slim"], "a plain `docker build` makes the last stage"
    assert ["FROM", "base", "AS", "with-claude-code"] in stages


@needs_helm
def test_an_unpackaged_chart_refuses_to_render_without_an_image_tag():
    bare = _helm("template", "csa", str(_CHART), "--set", "host=csa.example.com")
    assert bare.returncode != 0
    assert "image.tag is required" in bare.stderr
    tagged = _helm("template", "csa", str(_CHART), "--set", "host=csa.example.com,image.tag=abc")
    assert tagged.returncode == 0, tagged.stderr
    assert 'image: "ghcr.io/nabooai/csa-server-slim:abc"' in tagged.stdout
