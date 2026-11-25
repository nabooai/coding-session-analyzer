"""The schema contract: the models ARE the DDL, and a pre-Alembic file catches up on open.

Three environments, one per SQLite file. What is pinned here:

  * `alembic check` finds no difference between the models and a freshly migrated file, per
    environment, WITH a negative control (an extra column must make it fail) so the check cannot
    pass vacuously. This replaces the hand-written per-table parity lists.
  * a database created by any pre-Alembic DDL (July and September 2026) opens, keeps its rows,
    gains every model column, and ends stamped at head.
  * a fresh file is schema-identical to one the old DDL built, so a new workspace does not fork.
  * an up-to-date file costs exactly one extra statement to open.
  * racing threads and processes migrate a file once.

Run from server/: uv run pytest tests/test_migrations.py
"""
import configparser
import os
import sqlite3
import subprocess
import sys
import threading
import time

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext

from analyzer import fuzzy_axes, migrations, models, orgs, store, tenant
from tests import legacy_ddl

ENVS = [("corpus", lambda: tenant.Base.metadata), ("telemetry", lambda: models.telemetry),
        ("control", lambda: tenant.ControlBase.metadata)]


def _built(tmp_path, env):
    """A file at head for `env`, opened the way the server opens it, and its connection."""
    if env == "control":
        return orgs.control_db(str(tmp_path / "control.db"))
    return store.sqlite_db(str(tmp_path / "analyzer.db"))       # attaches + migrates `tel` too


def _diffs(con, env, metadata):
    """What `alembic check` compares: the models against this file, through the environment's own
    comparison options (test_the_documented_check_command_runs proves the CLI takes the same path)."""
    engine = sa.create_engine("sqlite://", poolclass=sa.pool.StaticPool, creator=lambda: con)
    with engine.connect() as bound:
        ctx = MigrationContext.configure(bound, opts=migrations.compare_opts(env, metadata))
        return compare_metadata(ctx, metadata)


@pytest.mark.parametrize("env,metadata", ENVS)
def test_models_match_a_freshly_migrated_file(tmp_path, env, metadata):
    con = _built(tmp_path, env)
    try:
        assert _diffs(con, env, metadata()) == []
    finally:
        con.close()


@pytest.mark.parametrize("env,metadata", ENVS)
def test_check_fails_on_a_model_the_migrations_do_not_have(tmp_path, env, metadata):
    """The negative control. Without it, a check wired to an empty MetaData would pass forever."""
    con = _built(tmp_path, env)
    extra = sa.MetaData()
    for table in metadata().tables.values():
        table.to_metadata(extra)
    one = next(iter(extra.tables.values()))
    one.append_column(sa.Column("never_migrated", sa.Text()))
    try:
        found = _diffs(con, env, extra)
        assert found and "never_migrated" in str(found), found
    finally:
        con.close()


def test_check_catches_a_lost_server_default(tmp_path):
    """`alembic check` ignores server defaults unless asked to compare them, and the parity tests
    that used to cover column-level shape are gone. sessions.source DEFAULT 'session' is read by
    every ingest path."""
    con = _built(tmp_path, "corpus")
    without = sa.MetaData()
    for table in tenant.Base.metadata.tables.values():
        table.to_metadata(without)
    without.tables["sessions"].c.source.server_default = None
    try:
        found = _diffs(con, "corpus", without)
        assert found and "source" in str(found), found
    finally:
        con.close()


def test_the_tracer_writes_the_modelled_spans_shape(tmp_path):
    """analyzer/tracer.py owns a hand-written CREATE TABLE and a positional INSERT because it must
    stay stdlib-only. Nothing else pins it to models.spans, so a revision that adds a column there
    would break every trace write silently."""
    from analyzer import tracer
    path = str(tmp_path / "analyzer.db")
    tracer._write_db(path, [{"trace_id": "t", "span_id": "s", "parent_id": None, "name": "ask",
                             "kind": "CHAIN", "at": "2026-09-01T00:00:00Z", "dur_ms": 1,
                             "status": "OK", "attrs": {}}])
    con = sqlite3.connect(store.telemetry_path(path))
    written = [r[1] for r in con.execute("PRAGMA table_info(spans)")]
    con.close()
    assert written == [c.name for c in models.spans.columns], \
        "tracer._DDL and models.spans have drifted; the positional INSERT writes the wrong columns"


SERVER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _scratch_ini(tmp_path, env, db_path):
    """The repo's own alembic.ini with one value changed: this environment's file.

    Read through ConfigParser with `here` bound to server/, the way alembic.config.Config does it,
    so %(here)s still resolves to the repo and every other option (prepend_sys_path above all)
    comes from the real file rather than a stripped-down copy that could drift from it."""
    src = configparser.ConfigParser(defaults={"here": SERVER})
    src.read(os.path.join(SERVER, "alembic.ini"))
    out = configparser.ConfigParser()
    out[env] = {k: src.get(env, k) for k in src.options(env) if k != "here"}
    out[env]["sqlalchemy.url"] = f"sqlite:///{db_path}"
    ini = tmp_path / f"{env}.ini"
    with open(ini, "w") as f:
        out.write(f)
    return ini


def _alembic(ini, env, *args, cwd):
    """Run the CLI the way a shell does. cwd is deliberately NOT server/: `python -m` puts the
    working directory on sys.path, so running from server/ would import `analyzer` by accident and
    hide whether alembic.ini actually puts it there."""
    done = subprocess.run([sys.executable, "-m", "alembic", "-c", str(ini), "-n", env, *args],
                          cwd=cwd, capture_output=True, text=True)
    assert done.returncode == 0, f"alembic -n {env} {' '.join(args)}\n{done.stdout}{done.stderr}"
    return done.stdout + done.stderr


def _cli_legacy(env, path):
    """A file built by a pre-Alembic DDL, with a row in it, for the CLI to catch up."""
    con = sqlite3.connect(path)
    if env == "corpus":
        con.executescript(legacy_ddl.JUL_2026_CORPUS)
        con.execute("INSERT INTO sessions(session_id,project,user) VALUES('old-1','csa','mara@example.com')")
        row = ("SELECT project, user FROM sessions", [("csa", "mara@example.com")])
    elif env == "telemetry":
        con.executescript(legacy_ddl.SEP05_2026_TELEMETRY.replace("tel.", ""))
        con.execute("INSERT INTO ui_events(at,user,event,page) "
                    "VALUES('2026-09-05T10:00:00Z','mara@example.com','page_view','#/memory')")
        row = ("SELECT user, page FROM ui_events", [("mara@example.com", "#/memory")])
    else:
        con.executescript(legacy_ddl.JUL_2026_CONTROL)
        con.execute("INSERT INTO orgs(id,name,created) VALUES('acme','Acme','2026-07-01T00:00:00Z')")
        row = ("SELECT id, name FROM orgs", [("acme", "Acme")])
    con.commit()
    con.close()
    return row


@pytest.mark.parametrize("env", ["corpus", "telemetry", "control"])
@pytest.mark.parametrize("shape", ["fresh", "legacy"])
def test_the_documented_cli_migrates_a_real_file(tmp_path, env, shape):
    """`uv run alembic -c alembic.ini -n <env> upgrade head` against a file on disk, for every
    environment, empty and pre-Alembic. This is the developer's and the operator's path: it goes
    through alembic.ini and migrations._cli_engine, neither of which the server ever touches, so
    nothing else in this file would notice it breaking.

    Asserted on the FILE, not on an exit code: the version row reaches head, `check` finds no
    remaining diff, every model column is there, integrity_check is ok, and a pre-Alembic row is
    still readable afterwards."""
    db = tmp_path / f"{env}.db"
    expect = _cli_legacy(env, str(db)) if shape == "legacy" else None
    ini = _scratch_ini(tmp_path, env, db)

    _alembic(ini, env, "upgrade", "head", cwd=tmp_path)
    _alembic(ini, env, "check", cwd=tmp_path)

    metadata = dict(ENVS)[env]()
    con = sqlite3.connect(db)
    try:
        assert con.execute("SELECT version_num FROM alembic_version").fetchone()[0] \
            == migrations.head(env)
        for table in metadata.sorted_tables:
            have = {r[1] for r in con.execute(f"PRAGMA table_info({table.name})")}
            assert have >= {c.name for c in table.columns}, table.name
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        if expect:
            assert con.execute(expect[0]).fetchall() == expect[1]
    finally:
        con.close()


@pytest.mark.parametrize("env", ["corpus", "telemetry", "control"])
def test_the_cli_reads_revisions_without_opening_a_database(tmp_path, env):
    """`heads`, `history` and `show` import the revision modules but never run env.py, so they get
    no help from it. The revisions import analyzer.migrations, which means alembic.ini has to put
    server/ on sys.path itself (prepend_sys_path) or all three die with ModuleNotFoundError.
    `heads` is also how a second branch finds the down_revision to parent onto."""
    ini = _scratch_ini(tmp_path, env, tmp_path / f"{env}.db")
    assert migrations.head(env) in _alembic(ini, env, "heads", cwd=tmp_path)


def test_the_cli_keeps_a_wal_corpus_in_wal(tmp_path):
    """A served corpus is in WAL and stays there: the mode is persistent in the file header, so a
    tool that opened it in rollback-journal mode would rewrite the header for every reader. The
    control plane is the mirror image (test_a_pre_alembic_control_db_catches_up_on_open)."""
    db = tmp_path / "analyzer.db"
    _cli_legacy("corpus", str(db))
    con = sqlite3.connect(db)
    assert con.execute("PRAGMA journal_mode=wal").fetchone()[0] == "wal"
    con.close()

    _alembic(_scratch_ini(tmp_path, "corpus", db), "corpus", "upgrade", "head", cwd=tmp_path)

    con = sqlite3.connect(db)
    try:
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        con.close()


@pytest.mark.parametrize("env", ["corpus", "telemetry", "control"])
def test_one_head_per_environment(env):
    """Two branches cutting a revision off the same head would leave `upgrade head` ambiguous, and
    it would fail at container boot on prod, not in review."""
    from alembic.script import ScriptDirectory
    assert len(ScriptDirectory.from_config(migrations._config(env)).get_heads()) == 1


def test_no_create_all_outside_the_migrations():
    """A create_all in a fixture builds tables the migrations never emitted: the column then exists
    in tests and is missing on prod. Table creation happens in a revision, nowhere else."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    hits = subprocess.run(["grep", "-rn", "create_all(", "--include=*.py", root],
                          capture_output=True, text=True).stdout.splitlines()
    allowed = (f"{root}/migrations/", f"{root}/analyzer/migrations.py", f"{root}/tests/test_migrations.py")
    hits = [h for h in hits if "/.venv/" not in h and not h.startswith(allowed)]
    assert hits == [], hits


# --- catch-up: a database built before Alembic ------------------------------------------------

def _legacy_corpus(path, ddl):
    con = sqlite3.connect(path)
    con.executescript(ddl)
    con.execute("INSERT INTO sessions(session_id,project,started_at,source,user) "
                "VALUES('old-1','csa','2026-07-01T09:00:00Z','session','mara@example.com')")
    con.execute("INSERT INTO hidden_context(session_id,type,claim,evidence,ref) "
                "VALUES('old-1','decision','we ship on fridays','the quote','ref1')")
    con.execute("INSERT INTO entities(id,type,name) VALUES('e1','file','store.py')")
    con.commit()
    con.close()


def _legacy_control(path, ddl):
    con = sqlite3.connect(path)
    con.executescript(ddl)
    con.execute("INSERT INTO orgs(id,name,invite_hash,created) VALUES('acme','Acme','h','2026-07-01T00:00:00Z')")
    con.execute("INSERT INTO user_tokens(token_hash,email,org_id,hostname,created,last_used_at) "
                "VALUES('deadbeef','dev@acme.com','acme','laptop','2026-07-01T00:00:00Z',?)",
                (orgs._now(-3600),))
    con.commit()
    con.close()


@pytest.mark.parametrize("ddl", [legacy_ddl.SEP_2026_CORPUS, legacy_ddl.JUL_2026_CORPUS],
                         ids=["sep-2026", "jul-2026"])
def test_a_pre_alembic_corpus_catches_up_on_open(tmp_path, ddl):
    path = str(tmp_path / "analyzer.db")
    _legacy_corpus(path, ddl)
    con = store.sqlite_db(path)
    try:
        assert con.execute("SELECT version_num FROM alembic_version").fetchone()[0] \
            == migrations.head("corpus")
        for table in tenant.Base.metadata.sorted_tables:
            have = {r[1] for r in con.execute(f"PRAGMA table_info({table.name})")}
            assert have >= {c.name for c in table.columns}, table.name
        assert con.execute("SELECT project, user FROM sessions WHERE session_id='old-1'").fetchone() \
            == ("csa", "mara@example.com")
        assert con.execute("SELECT claim, evidence FROM hidden_context WHERE ref='ref1'").fetchone() \
            == ("we ship on fridays", "the quote")
        assert con.execute("SELECT name FROM entities WHERE id='e1'").fetchone() == ("store.py",)
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert con.execute("PRAGMA busy_timeout").fetchone()[0] == 15000
        assert _diffs(con, "corpus", tenant.Base.metadata) == []
        assert _diffs(con, "telemetry", models.telemetry) == []
    finally:
        con.close()


def test_the_legacy_in_corpus_telemetry_tables_are_copied_then_dropped(tmp_path):
    """The July corpus carries `served` and `queries` inside it. store._migrate_telemetry copies those
    rows to the sibling file and then drops the originals. The ALEMBIC environment must still not
    notice them: _cold_open runs the chain BEFORE the copy, so a migration that dropped them would
    delete the rows before anything read them."""
    path = str(tmp_path / "analyzer.db")
    _legacy_corpus(path, legacy_ddl.JUL_2026_CORPUS)
    con = sqlite3.connect(path)
    con.execute("INSERT INTO served(session,ref,kind) VALUES('old-1','ref1','hidden_context')")
    con.commit()
    con.close()
    store._TEL_MIGRATED.clear()
    con = store.sqlite_db(path)
    try:
        assert con.execute("SELECT ref FROM tel.served").fetchall() == [("ref1",)], "copied first"
        assert not con.execute("SELECT 1 FROM main.sqlite_master WHERE name='served'").fetchone(), \
            "then dropped, so no frozen copy survives to be misread"
        assert _diffs(con, "corpus", tenant.Base.metadata) == []
    finally:
        con.close()


@pytest.mark.parametrize("ddl", [legacy_ddl.SEP05_2026_CONTROL, legacy_ddl.SEP_2026_CONTROL,
                                 legacy_ddl.JUL_2026_CONTROL],
                         ids=["sep05-2026", "sep-2026", "jul-2026"])
def test_a_pre_alembic_control_db_catches_up_on_open(tmp_path, monkeypatch, ddl):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CSA_TOKEN_TTL_DAYS", "30")
    path = str(tmp_path / "control.db")
    _legacy_control(path, ddl)
    con = orgs.control_db(path)
    try:
        assert con.execute("SELECT version_num FROM alembic_version").fetchone()[0] \
            == migrations.head("control")
        for table in tenant.ControlBase.metadata.sorted_tables:
            have = {r[1] for r in con.execute(f"PRAGMA table_info({table.name})")}
            assert have >= {c.name for c in table.columns}, table.name
        assert con.execute("SELECT name FROM orgs WHERE id='acme'").fetchone() == ("Acme",)
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert _diffs(con, "control", tenant.ControlBase.metadata) == []
        # the data step the old ALTER carried: a token minted before expiry existed is immortal,
        # and gets dated from its LAST USE, so a laptop still shipping keeps working.
        expires = con.execute("SELECT expires_at FROM user_tokens WHERE token_hash='deadbeef'").fetchone()[0]
        assert expires > orgs._now(), expires
    finally:
        con.close()
    fresh = sqlite3.connect(path)
    assert fresh.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    fresh.close()
    assert not os.path.exists(path + "-wal") and not os.path.exists(path + "-shm")


def _affinity(declared):
    """SQLite's declared-type -> affinity rules: `INT` and `INTEGER` are the same column."""
    d = declared.upper()
    if "INT" in d:
        return "INTEGER"
    return "TEXT" if any(t in d for t in ("CHAR", "CLOB", "TEXT")) else d


def _shape(path, schema="main"):
    con = sqlite3.connect(path)
    tables = {}
    for (name,) in con.execute(f"SELECT name FROM {schema}.sqlite_master WHERE type='table' "
                               "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'docs%' "
                               "AND name != 'alembic_version'"):
        # keyed by column NAME, not position: the ALTER list appended columns in the order it ran,
        # so two prod files of different ages already disagree on order (nothing reads a row
        # positionally; there is no `SELECT *` outside the model-authored planner).
        tables[name] = {r[1]: (_affinity(r[2]), r[3], r[4], r[5])
                        for r in con.execute(f"PRAGMA {schema}.table_info({name})")}
    indexes = {}
    for name, tbl in con.execute(f"SELECT name, tbl_name FROM {schema}.sqlite_master WHERE type='index' "
                                 "AND sql IS NOT NULL"):
        info = con.execute(f"PRAGMA {schema}.index_xinfo({name})").fetchall()
        unique = con.execute(f"PRAGMA {schema}.index_list({tbl})").fetchall()
        indexes[name] = (tbl, [r[2] for r in info if r[2]],
                         next(r[2] for r in unique if r[1] == name))
    con.close()
    return tables, indexes


def test_a_fresh_file_is_schema_identical_to_a_pre_alembic_one(tmp_path):
    """The models were aligned to what SQLite actually holds (TEXT, and a hand-written
    `x TEXT PRIMARY KEY` is nullable), so a workspace created today gets the same schema as one
    created last month once that one has been opened, which is when a deployed image brings it to
    head. Without this, new tenants quietly fork from every existing install."""
    legacy = str(tmp_path / "legacy.db")
    _legacy_corpus(legacy, legacy_ddl.SEP_2026_CORPUS)
    con = sqlite3.connect(legacy)
    for ddl in ("ALTER TABLE sessions ADD COLUMN harness TEXT DEFAULT 'claude-code'",
                "ALTER TABLE hidden_context ADD COLUMN as_of TEXT",
                "ALTER TABLE mentions ADD COLUMN dominant INT DEFAULT 0",
                "ALTER TABLE reviews ADD COLUMN flagger TEXT",
                "ALTER TABLE costs ADD COLUMN trace_id TEXT",
                "ALTER TABLE tenant_config ADD COLUMN archive_days TEXT"):
        con.execute(ddl)                                 # the ALTER list, verbatim from sqlite_db
    con.commit()
    con.close()
    store.sqlite_db(legacy).close()
    fresh = str(tmp_path / "fresh" / "analyzer.db")
    store.sqlite_db(fresh).close()

    _assert_same_shape(legacy, fresh)


def _assert_same_shape(legacy, fresh):
    old_tables, old_ix = _shape(legacy)
    new_tables, new_ix = _shape(fresh)
    assert set(new_tables) == set(old_tables)
    for name, cols in old_tables.items():
        assert new_tables[name] == cols, name          # name -> (affinity, notnull, default, pk)
    assert old_ix == new_ix


def test_a_fresh_control_db_is_schema_identical_to_a_pre_alembic_one(tmp_path, monkeypatch):
    """A workspace whose control.db predates Alembic ends up with exactly the shape a new one has.

    BOTH files are opened through orgs.control_db, which is the claim: the catch-up creates what the
    old DDL missed, then every revision since runs on both. Comparing a RAW legacy file against a
    migrated one instead would only hold until the first revision added a table, and would then fail
    on the revision rather than on a real drift."""
    monkeypatch.chdir(tmp_path)
    legacy = str(tmp_path / "legacy_control.db")
    _legacy_control(legacy, legacy_ddl.SEP05_2026_CONTROL)
    con = sqlite3.connect(legacy)
    for ddl in ("ALTER TABLE user_tokens ADD COLUMN expires_at TEXT",
                "ALTER TABLE orgs ADD COLUMN domain TEXT",
                "ALTER TABLE user_tokens ADD COLUMN role TEXT"):
        con.execute(ddl)                                 # the _ADDED_COLUMNS list, verbatim
    con.commit()
    con.close()
    orgs.control_db(legacy).close()                      # catch up, then every revision since
    fresh = str(tmp_path / "fresh_control.db")
    orgs.control_db(fresh).close()
    _assert_same_shape(legacy, fresh)


def test_a_fresh_telemetry_file_is_schema_identical_to_a_pre_alembic_one(tmp_path):
    legacy = str(tmp_path / "legacy" / "telemetry.db")
    os.makedirs(os.path.dirname(legacy))
    con = sqlite3.connect(legacy)
    con.executescript(legacy_ddl.SEP05_2026_TELEMETRY.replace("tel.", ""))
    con.commit()
    con.close()
    fresh = str(tmp_path / "fresh" / "analyzer.db")
    store.sqlite_db(fresh).close()
    _assert_same_shape(legacy, store.telemetry_path(fresh))


def test_the_tables_a_master_image_already_created_are_left_alone(tmp_path, monkeypatch):
    """tel.ui_events (#106) and cli_pairings (#107) merged as DDL strings after the baselines were
    drafted and before this change ships, so they are IN the baselines. A box that pulled a master
    image already has both, and the catch-up must skip them: same declared types, same column
    ORDER, rows kept. Recreating either would be a hard raise on a file every request touches."""
    monkeypatch.chdir(tmp_path)
    tel = str(tmp_path / "telemetry.db")
    con = sqlite3.connect(tel)
    con.executescript(legacy_ddl.SEP05_2026_TELEMETRY.replace("tel.", ""))
    con.execute("INSERT INTO ui_events(at,user,event,page) "
                "VALUES('2026-09-05T10:00:00Z','mara@example.com','page_view','#/memory')")
    before_tel = [r[1:] for r in con.execute("PRAGMA table_info(ui_events)")]
    con.commit()
    con.close()

    control = str(tmp_path / "control.db")
    con = sqlite3.connect(control)
    con.executescript(legacy_ddl.SEP05_2026_CONTROL)
    con.execute("INSERT INTO cli_pairings(nonce_hash,hostname,created,email,verified) "
                "VALUES('nh','laptop','2026-09-05T10:00:00Z','mara@example.com',1)")
    before_control = [r[1:] for r in con.execute("PRAGMA table_info(cli_pairings)")]
    con.commit()
    con.close()

    corpus = store.sqlite_db(str(tmp_path / "analyzer.db"))
    try:
        assert [r[1:] for r in corpus.execute("PRAGMA tel.table_info(ui_events)")] == before_tel
        assert corpus.execute("SELECT user, page FROM tel.ui_events").fetchall() \
            == [("mara@example.com", "#/memory")]
        assert _diffs(corpus, "telemetry", models.telemetry) == []
    finally:
        corpus.close()

    con = orgs.control_db(control)
    try:
        after = [r[1:] for r in con.execute("PRAGMA table_info(cli_pairings)")]
        assert after[:len(before_control)] == before_control, "later revisions only append columns"
        assert con.execute("SELECT hostname, verified FROM cli_pairings").fetchall() \
            == [("laptop", 1)]
        assert _diffs(con, "control", tenant.ControlBase.metadata) == []
    finally:
        con.close()


def test_the_tables_fuzzy_axes_skips_are_real(tmp_path):
    """`fuzzy_axes.SKIP_TABLES` keeps a table out of the fuzzy index. Every entry with a model is
    named through that model, so a rename in models.py moves the set with it. The rest are literals
    because there is nothing to ask: the FTS5 `docs` table and the five shadow tables SQLite builds
    beside it, the two session_workspace views, and alembic_version.

    Both halves can drift silently. A literal that stops existing leaves a dead entry and the axes
    quietly widen; a literal that GAINS a model is a hard-coded name again."""
    con = _built(tmp_path, "corpus")
    try:
        on_disk = {r[0] for r in con.execute(
            "SELECT name FROM main.sqlite_master WHERE type IN ('table','view') "
            "UNION SELECT name FROM tel.sqlite_master WHERE type IN ('table','view')")}
    finally:
        con.close()
    assert fuzzy_axes.SKIP_TABLES <= on_disk, fuzzy_axes.SKIP_TABLES - on_disk

    modelled = {t.name for meta in (tenant.Base.metadata, models.telemetry,
                                    tenant.ControlBase.metadata) for t in meta.tables.values()}
    named_by_hand = fuzzy_axes._SKIP_UNMODELLED & modelled
    assert named_by_hand == set(), \
        f"{named_by_hand} now have models; skip them through the model, not by name"


def test_the_docs_table_is_the_fts5_shape_store_expects(tmp_path):
    con = store.sqlite_db(str(tmp_path / "analyzer.db"))
    try:
        sql = con.execute("SELECT sql FROM sqlite_master WHERE name='docs'").fetchone()[0]
        assert store.DOCS_COLS in sql
        con.execute("SELECT first_at, valid_at, invalid_at, state FROM docs LIMIT 0")
    finally:
        con.close()


def test_a_telemetry_file_tracer_created_first_still_catches_up(tmp_path):
    """tracer.py writes spans on its own stdlib connection, so telemetry.db can exist, with one
    table and no version row, before any corpus open."""
    from analyzer import tracer
    path = str(tmp_path / "analyzer.db")
    # stamped now: _write_db prunes spans older than the workspace's trace_days in the same write
    tracer._write_db(path, [{"trace_id": "t1", "span_id": "s1", "parent_id": None, "name": "ask",
                             "kind": "CHAIN", "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "dur_ms": 5,
                             "status": "OK", "attrs": {}}])
    con = store.sqlite_db(path)
    try:
        assert con.execute("SELECT version_num FROM tel.alembic_version").fetchone()[0] \
            == migrations.head("telemetry")
        assert con.execute("SELECT COUNT(*) FROM tel.spans").fetchone()[0] == 1, "spans must survive"
        assert _diffs(con, "telemetry", models.telemetry) == []
    finally:
        con.close()


def test_repeated_planner_opens_do_not_accumulate_locks(tmp_path):
    """`ro_connect` migrates an in-memory telemetry stand-in on every call. A lock keyed per
    connection would leave one entry per ask, forever, in a long-lived api process."""
    path = str(tmp_path / "analyzer.db")
    store.sqlite_db(path).close()
    os.remove(store.telemetry_path(path))
    before = len(migrations._locks)
    for _ in range(25):
        store.ro_connect(path).close()
    assert len(migrations._locks) == before


def test_ro_connect_serves_an_org_with_no_telemetry_file(tmp_path):
    """The planner path (analyzer.ask) opens the corpus read-only and attaches an in-memory
    stand-in when telemetry.db is missing, so `tel.served` parses. The migration must run into the
    stand-in without writing to the read-only main, and ATTACH must still be denied afterwards."""
    path = str(tmp_path / "analyzer.db")
    store.sqlite_db(path).close()
    os.remove(store.telemetry_path(path))
    con = store.ro_connect(path)
    try:
        assert con.execute("SELECT COUNT(*) FROM tel.served").fetchone()[0] == 0
        with pytest.raises(sqlite3.DatabaseError):
            con.execute("ATTACH DATABASE ':memory:' AS other")
    finally:
        con.close()


# --- cost and concurrency ---------------------------------------------------------------------

def test_an_up_to_date_open_costs_one_extra_statement(tmp_path):
    """The warm path is every open on a running box: three containers, every request. It must be
    one SELECT against alembic_version, never a script directory walk or a DDL pass."""
    path = str(tmp_path / "analyzer.db")
    store.sqlite_db(path).close()
    seen = []
    real_connect = sqlite3.connect

    def traced(*a, **kw):
        con = real_connect(*a, **kw)
        con.set_trace_callback(seen.append)
        return con

    import analyzer.store as store_module
    store_module.sqlite3.connect = traced
    try:
        store.sqlite_db(path).close()
    finally:
        store_module.sqlite3.connect = real_connect
    version_reads = [s for s in seen if "alembic_version" in s]
    assert len(version_reads) == 2, seen                 # corpus + telemetry, one each
    assert not [s for s in seen if s.strip().upper().startswith(("CREATE", "ALTER"))], seen


def test_an_unreadable_file_reports_what_actually_broke(tmp_path):
    """A file that cannot be read is not a file that needs migrating. If the version read swallowed
    every error, the open would go on to take the migration lock and report whatever failed next
    (on a read-only directory, a permission error about the lock file) instead of SQLite's own."""
    path = str(tmp_path / "analyzer.db")
    store.sqlite_db(path).close()                        # already at head
    os.remove(path + ".migrate.lock")
    os.remove(store.telemetry_path(path) + ".migrate.lock")
    os.chmod(tmp_path, 0o555)                            # a WAL db needs to write beside itself
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly|unable to open"):
            store.sqlite_db(path)
    finally:
        os.chmod(tmp_path, 0o755)


def test_racing_openers_migrate_a_fresh_file_once(tmp_path):
    """api, mcp and etl open every org file within milliseconds of each other after a deploy."""
    path = str(tmp_path / "analyzer.db")
    errs = []

    def opener():
        try:
            con = store.sqlite_db(path)
            con.execute("SELECT session_id, harness, shipped_by FROM sessions").fetchall()
            con.execute("SELECT COUNT(*) FROM tel.served").fetchall()
            con.close()
        except Exception as e:                           # noqa: BLE001 — any opener crash fails the test
            errs.append(e)

    threads = [threading.Thread(target=opener) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errs, errs
    con = sqlite3.connect(path)
    assert con.execute("SELECT COUNT(*) FROM alembic_version").fetchone()[0] == 1
    con.close()


def test_threads_migrating_DIFFERENT_workspaces_do_not_collide(tmp_path):
    """The per-path lock does not cover this: `alembic.context` is a module-global proxy and the
    Config is cached per environment, so two threads upgrading two workspaces used to drive each
    other's connection and leave both files unstamped. The ETL drains CSA_DRAIN_WORKERS orgs in
    parallel and the API's boot sweep runs beside request threads, so this is the deploy shape."""
    paths = [str(tmp_path / f"org{i}" / "analyzer.db") for i in range(4)]
    for path in paths:
        os.makedirs(os.path.dirname(path))
        _legacy_corpus(path, legacy_ddl.SEP_2026_CORPUS)
    errs = []

    def opener(path):
        try:
            con = store.sqlite_db(path)
            con.execute("SELECT harness FROM sessions").fetchall()
            con.close()
        except Exception as e:                           # noqa: BLE001
            errs.append((path, repr(e)))

    threads = [threading.Thread(target=opener, args=(p,)) for p in paths]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errs, errs
    for path in paths:
        con = sqlite3.connect(path)
        assert con.execute("SELECT version_num FROM alembic_version").fetchone()[0] \
            == migrations.head("corpus"), path
        assert con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        con.close()


def test_racing_processes_migrate_a_fresh_file_once(tmp_path):
    """The flock, not the in-process lock: prod runs api, mcp and etl as separate containers."""
    path = str(tmp_path / "analyzer.db")
    server = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = ("import sys; sys.path.insert(0, %r)\n" % server +
            "from analyzer import store\n"
            "con = store.sqlite_db(%r)\n" % path +
            "con.execute('SELECT COUNT(*) FROM tel.served').fetchall()\n"
            "con.close()\n")
    procs = [subprocess.Popen([sys.executable, "-c", code], cwd=server,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(3)]
    for p in procs:
        out, err = p.communicate(timeout=120)
        assert p.returncode == 0, err.decode()[-2000:]
    con = sqlite3.connect(path)
    assert con.execute("SELECT COUNT(*) FROM alembic_version").fetchone()[0] == 1
    con.execute("ATTACH DATABASE ? AS tel", (store.telemetry_path(path),))
    assert con.execute("SELECT COUNT(*) FROM tel.alembic_version").fetchone()[0] == 1
    con.close()


def test_a_telemetry_migration_does_not_take_the_corpus_writer(tmp_path):
    """The telemetry upgrade is the one that runs with the OTHER file attached (`main` is the
    corpus by then), so it is where an IMMEDIATE transaction would take the corpus writer and undo
    the split. A deferred BEGIN that only writes `tel` must go through with the corpus held."""
    path = str(tmp_path / "analyzer.db")
    store.sqlite_db(path).close()
    os.remove(store.telemetry_path(path))                # the telemetry file has to migrate again
    blocker = store.sqlite_db(path)
    blocker.execute("PRAGMA busy_timeout=0")
    blocker.execute("BEGIN")                             # what index_session holds on the corpus
    blocker.execute("INSERT INTO entities VALUES ('e1','file','e1',NULL)")
    try:
        con = store.sqlite_db(path)
        assert con.execute("SELECT version_num FROM tel.alembic_version").fetchone()[0] \
            == migrations.head("telemetry")
        assert con.execute("SELECT COUNT(*) FROM tel.served").fetchone()[0] == 0
        con.close()
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
