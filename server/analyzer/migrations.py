"""Schema migrations: three Alembic environments, one per SQLite FILE.

    corpus     data/orgs/<org>/analyzer.db, attached as `main`   models: tenant.Base + analyzer.models
    telemetry  its telemetry.db sibling, attached as `tel`       models: models.telemetry
    control    data/control.db                                   models: tenant.ControlBase

Each file carries its own `alembic_version`, so a file can be copied, backed up or restored on its
own. `ensure()` is the runtime face: every open reads that one row and runs `upgrade head` only
when the file is behind. A file at head costs one SELECT.

Migrating runs on the CALLER's connection, not through db.engine_for: an org corpus has its
telemetry sibling ATTACHed (so the engine's pragmas would reach two files), a `:memory:` corpus has
no path to reconnect to, and control.db must never meet engine_for's persistent journal_mode=WAL.
The upgrade is one deferred transaction; never BEGIN IMMEDIATE, which would take the writer lock on
every attached database. Call it at open, never inside a caller's transaction.
"""
import fcntl
import functools
import logging
import os
import sqlite3
import threading
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
import sqlalchemy as sa
from sqlalchemy import create_engine, pool

log = logging.getLogger("csa.migrations")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INI = os.path.join(_ROOT, "alembic.ini")
#: which attached schema each environment's tables and its own alembic_version live in
VERSION_SCHEMA = {"corpus": None, "telemetry": "tel", "control": None}

#: One upgrade at a time per PROCESS. `alembic.context` is a module-global proxy and the Config this
#: hands it is cached per environment, so two threads upgrading DIFFERENT files drove each other's
#: connection (measured: three org corpora opened at once, all three left unstamped). The per-path
#: flock still guards the cross-process race; this costs nothing, because an upgrade happens once
#: per file per deploy and every other open returns before reaching it.
_upgrading = threading.Lock()


@functools.cache
def _config(env):
    return Config(INI, ini_section=env)


@functools.cache
def head(env):
    return ScriptDirectory.from_config(_config(env)).get_current_head()


def image_version():
    """The Dockerfile's version build arg, written to VERSION beside alembic.ini; "dev" for a
    source checkout. Read at call time, not cached, so a redeploy of the file is picked up."""
    try:
        return (Path(_ROOT) / "VERSION").read_text().strip()
    except OSError:
        return "dev"


def _current(con, env):
    """The file's revision, or None when it has no version table yet (pre-Alembic, or fresh).

    Only a missing table reads as None. A file that cannot be READ (locked past busy_timeout, a
    read-only directory a WAL database needs to write beside, a corrupt page) must surface SQLite's
    own error here, or the caller goes on to take the migration lock and reports whatever fails
    next instead of what actually broke.
    """
    schema = VERSION_SCHEMA[env]
    table = f"{schema}.alembic_version" if schema else "alembic_version"
    try:
        row = con.execute(f"SELECT version_num FROM {table}").fetchone()
    except sqlite3.OperationalError as e:
        if "no such table" not in str(e):
            raise
        return None
    return row[0] if row else None


_locks = {}
_locks_guard = threading.Lock()


def _lock(key):
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def ensure(con, env, path=None):
    """Bring the file behind `con` to head. Returns True if this call migrated it.

    `path` names the file whose lock guards the upgrade (the telemetry sibling for the telemetry
    environment). None means `:memory:`, which is private to this connection: no lock at all, and
    never a per-connection lock, which `ro_connect`'s in-memory telemetry stand-in would leak one of
    per call.
    """
    if _current(con, env) == head(env):
        return False
    if path is None:
        return _upgrade(con, env)
    with _lock(path):
        if _current(con, env) == head(env):              # another thread migrated it while we waited
            return False
        with open(path + ".migrate.lock", "w") as lockf:
            fcntl.flock(lockf, fcntl.LOCK_EX)            # blocking: api, mcp and etl race at boot
            if _current(con, env) == head(env):          # another PROCESS migrated it
                return False
            return _upgrade(con, env, path)


def _upgrade(con, env, path=None):
    """Run the upgrade in one deferred transaction. SQLite reports DDL as non-transactional to
    Alembic, so without the explicit BEGIN each statement would autocommit and an interrupted
    upgrade would leave a half-built file. Deferred, never IMMEDIATE, which would take the writer
    lock on every attached database."""
    if con.in_transaction:
        raise RuntimeError("migrations.ensure runs at open, not inside a caller's transaction")
    cfg = _config(env)
    engine = create_engine("sqlite://", poolclass=pool.StaticPool, creator=lambda: con)
    with _upgrading:
        con.execute("BEGIN")
        try:
            with engine.connect() as bound:
                cfg.attributes["connection"] = bound
                command.upgrade(cfg, "head")
                bound.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            cfg.attributes.pop("connection", None)
    log.info("%s schema at %s: %s", env, path or ":memory:", head(env))
    return True


def compare_opts(env, metadata):
    """How this environment compares the models to a file, for both `upgrade` and `check`.

    Only the tables in `metadata` are its business. Anything else on the file is left alone and
    never diffed: the FTS5 `docs` table and its shadow tables, the two views, and the legacy
    in-corpus served/queries/spans the telemetry split left behind. So a table is never
    auto-dropped either; removing one is a hand-written revision.
    """
    schema = VERSION_SCHEMA[env]
    names = {t.name for t in metadata.tables.values()}

    def include_name(name, type_, _parent):
        if type_ == "schema":
            return name == schema
        if type_ == "table":
            return name in names
        return True

    return {"target_metadata": metadata, "version_table_schema": schema,
            "include_schemas": schema is not None, "include_name": include_name,
            "compare_server_default": True}


def run(config, metadata, env):
    """The body of every environment's env.py."""
    from alembic import context

    def go(connection):
        context.configure(connection=connection, **compare_opts(env, metadata))
        with context.begin_transaction():
            context.run_migrations()

    bound = config.attributes.get("connection")
    if bound is not None:                                # the server, on the connection it holds
        return go(bound)
    logging.basicConfig(level=logging.INFO)              # the CLI has no [loggers] section to read
    with _cli_engine(config, env).connect() as connection:   # the developer, from alembic.ini
        go(connection)
        connection.commit()


def _cli_engine(config, env):
    """The connection `alembic ... -n <env>` uses from a shell: the telemetry file ATTACHed as `tel`
    on an empty main, the way the server sees it, so a revision reads identically either way."""
    url = config.get_main_option("sqlalchemy.url")
    path = url.split("///", 1)[1]
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    if VERSION_SCHEMA[env] is None:
        return create_engine(url)
    con = sqlite3.connect(":memory:")
    con.execute("ATTACH DATABASE ? AS tel", (path,))
    return create_engine("sqlite://", poolclass=pool.StaticPool, creator=lambda: con)


def catch_up(metadata):
    """Bring a file built before Alembic up to `metadata`: create the tables it lacks, add the
    columns it lacks, touch nothing else. A table it already has keeps its rows, its column order
    and its declared types, so an upgrade interrupted halfway is safe to repeat.

    BASELINE REVISIONS ONLY. A later revision runs against a shape the version row guarantees, so it
    says op.add_column; "add if missing" there would mean the version row is not trusted.
    """
    from alembic import op

    bind = op.get_bind()
    schema = op.get_context().version_table_schema
    existing = {r[0] for r in bind.exec_driver_sql(
        f"SELECT name FROM {schema or 'main'}.sqlite_master WHERE type='table'")}
    for table in metadata.sorted_tables:
        if table.name not in existing:
            table.create(bind)
            log.info("%s.%s: created", schema or "main", table.name)
            continue
        have = {r[1] for r in bind.exec_driver_sql(
            f"PRAGMA {schema or 'main'}.table_info({table.name})")}
        for col in table.columns:
            if col.name not in have:
                op.add_column(table.name, sa.Column(col.name, col.type, nullable=True,
                                                    server_default=col.server_default), schema=schema)
                log.info("%s.%s: added column %s", schema or "main", table.name, col.name)
        for index in table.indexes:
            index.create(bind, checkfirst=True)
