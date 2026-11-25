"""Tenant model + per-tenant configuration.

A TENANT is an org: its identity row lives in the control-plane db (analyzer/orgs.py, `orgs`
table). Settings come in two scopes, both read per use so a change needs no restart:

  workspace   the single-row `tenant_config` table in the org's own analyzer.db
  deployment  the single-row `settings` table in control.db, shared by every workspace

Resolution per field: the row, else the built-in default in _FIELDS. No environment variable is
consulted; the environment carries only bootstrap values and secrets.

This module is the SQLAlchemy beachhead (project decision 2026-08-03: SQLAlchemy project-wide).
Models and the read/write repository run on SQLAlchemy 2.0. `config(con)` is the deliberate bridge
for the legacy raw-sqlite3 modules: they pass the per-org connection they already hold, and the
connection's own db file IS the tenant scope — no org-id plumbing. The bridge retires module by
module as the migration lands.
"""
from __future__ import annotations

import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from sqlalchemy import REAL, CheckConstraint, Column, Index, Integer, Table, Text, select
from sqlalchemy.exc import DatabaseError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from analyzer import db as _db


class Base(DeclarativeBase):
    """The corpus plane: one org's analyzer.db (analyzer/models.py holds the rest of it)."""


class ControlBase(DeclarativeBase):
    """The control plane: data/control.db, the org-identity file, and its own migration
    environment. Separate MetaData from the corpus, so no create_all or migration can conjure a
    control table into an org db (a data->control credential-escalation surface) or the reverse.

    control.db is BRIDGE-ONLY: statements execute via db.run/run_many on orgs.control_db()'s raw
    connection, NEVER through db.engine_for, whose connect-time `PRAGMA journal_mode=WAL` is
    persistent: one routing mistake flips the file header and spawns -wal/-shm siblings on every
    existing install. tests/test_orgs_models.py pins journal_mode == 'delete'."""


class Org(ControlBase):
    """Control-plane identity of a tenant."""
    __tablename__ = "orgs"

    id: Mapped[str] = mapped_column(Text, primary_key=True, nullable=True)
    name: Mapped[str | None] = mapped_column(Text)
    invite_hash: Mapped[str | None] = mapped_column(Text)
    created: Mapped[str | None] = mapped_column(Text)
    domain: Mapped[str | None] = mapped_column(Text)  # Workspace domain that SSO-maps to this org


class UserToken(ControlBase):
    """Per-user bearer token (orgs.mint_token writes, orgs.lookup reads). All TEXT on purpose:
    timestamps are fixed-width UTC ISO strings, so a plain string compare IS a correct time
    compare (see orgs._now)."""
    __tablename__ = "user_tokens"
    __table_args__ = (Index("ix_user_tokens_org", "org_id"),)

    token_hash: Mapped[str] = mapped_column(Text, primary_key=True, nullable=True)   # sha256; plaintext never stored
    email: Mapped[str | None] = mapped_column(Text)
    org_id: Mapped[str | None] = mapped_column(Text)
    hostname: Mapped[str | None] = mapped_column(Text)
    created: Mapped[str | None] = mapped_column(Text)
    revoked_at: Mapped[str | None] = mapped_column(Text)
    last_used_at: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[str | None] = mapped_column(Text)   # NULL == never expires
    role: Mapped[str | None] = mapped_column(Text)         # NULL == "member" (orgs.lookup)
    service: Mapped[int | None] = mapped_column(Integer)   # 1 == a Team > Service token: always a member


class WorkspaceRequest(ControlBase):
    """A stranger's "Request a workspace" from the pre-auth landing page (control.db
    `workspace_requests`; orgs.request_workspace writes, orgs.approve_request resolves).
    Control-plane metadata only — never corpus content. The UNIQUE (company_norm, email_norm)
    index is what lets a duplicate submit be an OR-IGNORE no-op, so the public endpoint answers
    identically whether or not the pair was ever seen (no existence oracle)."""
    __tablename__ = "workspace_requests"
    __table_args__ = (Index("ux_workspace_requests_norm", "company_norm", "email_norm", unique=True),)

    id: Mapped[str] = mapped_column(Text, primary_key=True, nullable=True)   # short hex the operator types into `org approve <id>`
    company: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    company_norm: Mapped[str | None] = mapped_column(Text)    # lowercased, whitespace-collapsed (orgs._clean_text)
    email_norm: Mapped[str | None] = mapped_column(Text)      # stripped + lowered
    created: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(Text)          # 'pending' | 'approved'
    handled_at: Mapped[str | None] = mapped_column(Text)
    org_id: Mapped[str | None] = mapped_column(Text)          # the org an approval produced


#: Admin audit trail. PK-less rowid table -> Core Table by design (the models.costs rule).
#: Appended by orgs._audit, read newest-first (ORDER BY rowid DESC) by orgs.audit_log.
admin_log = Table(
    "admin_log", ControlBase.metadata,
    Column("at", Text), Column("action", Text),
    Column("org_id", Text), Column("detail", Text),
)


class ClientDirective(ControlBase):
    """One admin instruction to a tenant's csa-clients (control.db `client_directives`; written by
    analyzer/fleet.py, served to a machine by /api/client-config).

    Control plane, not corpus: a directive names WHICH MACHINES by the same `email` + `hostname`
    that `user_tokens` already records at mint time, so the match is a join against credentials that
    live in this file — never against session content, which is self-asserted.

    Targeting widens by omission: empty `target_email` = every machine in the org, empty
    `target_hostname` = every machine of that person. All TEXT for the same reason as UserToken —
    fixed-width UTC ISO strings compare correctly as strings (orgs._now)."""
    __tablename__ = "client_directives"
    __table_args__ = (Index("ix_client_directives_org", "org_id"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True, nullable=True)
    org_id: Mapped[str | None] = mapped_column(Text)
    target_email: Mapped[str | None] = mapped_column(Text)      # '' = the whole org
    target_hostname: Mapped[str | None] = mapped_column(Text)   # '' = every machine of target_email
    since: Mapped[str | None] = mapped_column(Text)             # ISO floor; '' = all history
    created: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(Text)        # the admin credential that issued it
    revoked_at: Mapped[str | None] = mapped_column(Text)        # cancelled before every machine saw it


class ClientDirectiveAck(ControlBase):
    """What one machine DID about one directive (control.db `client_directive_acks`).

    A directive is one-shot per machine: `fleet.for_client` stops serving one to a token that has
    already acked it, so a laptop cannot loop on the same fetch every SessionStart. It is also the
    only honest way to render the panel — 'sent' is not 'done', and a client that refused because
    its owner declined backfill must be able to SAY so rather than look like it is still working."""
    __tablename__ = "client_directive_acks"

    directive_id: Mapped[str] = mapped_column(Text, primary_key=True, nullable=True)
    token_hash: Mapped[str] = mapped_column(Text, primary_key=True, nullable=True)   # which machine (never plaintext)
    email: Mapped[str | None] = mapped_column(Text)
    hostname: Mapped[str | None] = mapped_column(Text)
    at: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(Text)   # 'applied' | 'declined' | 'error'
    shipped: Mapped[int | None] = mapped_column(Integer)  # sessions this machine sent for it
    detail: Mapped[str | None] = mapped_column(Text)


class CliPairing(ControlBase):
    """One `csa-client setup` handshake in flight (control.db `cli_pairings`; analyzer/cliauth.py
    owns the statements).

    Holds no credential. The nonce that claims a pairing is stored as its sha256, like a token in
    `user_tokens`, and the per-user token is minted by the claiming poll rather than by the
    Authorize click, so no plaintext token is ever written here. Reading this file tells you that
    a machine was authorized for an address; it does not let you complete the pairing."""
    __tablename__ = "cli_pairings"

    nonce_hash: Mapped[str] = mapped_column(Text, primary_key=True, nullable=True)
    hostname: Mapped[str | None] = mapped_column(Text)
    created: Mapped[str | None] = mapped_column(Text)
    approved_at: Mapped[str | None] = mapped_column(Text)   # NULL until the Authorize click
    email: Mapped[str | None] = mapped_column(Text)         # who the click authorized
    org_id: Mapped[str | None] = mapped_column(Text)
    verified: Mapped[int | None] = mapped_column(Integer)   # 1 when Google vouched for the email
    shipping: Mapped[str | None] = mapped_column(Text)      # which plugin mode asked; NULL for csa-client
    claimed_at: Mapped[str | None] = mapped_column(Text)    # set once, by the poll that took it

class Invite(ControlBase):
    """One invite LINK, for one named person (control.db `invites`; analyzer/orgs.py owns the
    statements). Distinct from `Org.invite_hash`, the workspace-wide shared secret, which stays.

    Stored as the token's sha256, like `user_tokens`, so a leaked control.db cannot redeem one. The
    link admits the address in `email` and nobody else, until `expires_at`: `used_at` records the
    FIRST redemption and never gates, so the same person's second machine is not locked out.
    """
    __tablename__ = "invites"
    __table_args__ = (Index("ix_invites_org", "org_id"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True, nullable=True)
    org_id: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)          # lowercased; the ONE address it admits
    token_hash: Mapped[str | None] = mapped_column(Text)     # sha256; plaintext never stored
    created_by: Mapped[str | None] = mapped_column(Text)     # the admin who issued it
    created_at: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[str | None] = mapped_column(Text)
    used_at: Mapped[str | None] = mapped_column(Text)        # first redemption; never a refusal
    revoked_at: Mapped[str | None] = mapped_column(Text)


class Sso(ControlBase):
    """The one OpenID issuer this deployment signs in with (control.db `sso`; `csa.py sso set`
    writes, auth.sso_config reads). A row here closes the shared-invite mint path.

    `client_secret` is the first plaintext credential in this file: Google's and Entra's token
    endpoints refuse a public client, and the secret is sent, not compared, so it cannot be hashed."""
    __tablename__ = "sso"
    __table_args__ = (CheckConstraint("id = 1"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    issuer: Mapped[str | None] = mapped_column(Text)          # https URL, no trailing slash
    client_id: Mapped[str | None] = mapped_column(Text)
    client_secret: Mapped[str | None] = mapped_column(Text)   # NULL for a public client (PKCE only)
    display_name: Mapped[str | None] = mapped_column(Text)    # "Sign in with {display_name}"
    updated_at: Mapped[str | None] = mapped_column(Text)
    updated_by: Mapped[str | None] = mapped_column(Text)


class SsoLogin(ControlBase):
    """One browser sign-in in flight between /api/sso/start and /api/sso/callback (control.db
    `sso_logins`; analyzer/sso.py owns the statements).

    The state and the /join link are stored as their sha256 like `cli_pairings.nonce_hash` and
    `invites.token_hash`; the callback claims the row in the same statement that deletes it, so a
    replayed state finds nothing. The PKCE verifier is plaintext because the token endpoint needs
    it once, and it is useless without the code the IdP only ever hands to the browser that started
    this row."""
    __tablename__ = "sso_logins"

    state_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    nonce: Mapped[str | None] = mapped_column(Text)
    verifier: Mapped[str | None] = mapped_column(Text)
    join_token: Mapped[str | None] = mapped_column(Text)      # sha256 of the /join link this browser carried, if any
    return_path: Mapped[str | None] = mapped_column(Text)     # where the interstitial sends the browser
    created: Mapped[str | None] = mapped_column(Text)


class DeploymentSettingsRow(ControlBase):
    """Deployment-wide settings (control.db `settings`, id CHECK-pinned to 1): what every workspace
    on this server shares. NULL = the built-in default. Written by the Settings screen on a
    one-workspace server and by `csa.py org config` on the box; read per use by tenant.deployment()."""
    __tablename__ = "settings"
    __table_args__ = (CheckConstraint("id = 1"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    main_model: Mapped[str | None] = mapped_column(Text)         # "<provider id>/<model>": reads each session
    small_model: Mapped[str | None] = mapped_column(Text)        # judging and routing
    drain_workers: Mapped[int | None] = mapped_column(Integer)
    otlp_endpoint: Mapped[str | None] = mapped_column(Text)
    request_webhook: Mapped[str | None] = mapped_column(Text)    # a credential: shown as its host only
    updated_at: Mapped[str | None] = mapped_column(Text)
    updated_by: Mapped[str | None] = mapped_column(Text)


class ExtractionStall(ControlBase):
    """Why a workspace's extraction is not draining (control.db `extraction_stalls`): a row while the
    etl's last batch failed for a systemic reason, deleted by the next batch that extracts a session.
    The etl and the api are separate processes, so the api reads the answer from here."""
    __tablename__ = "extraction_stalls"

    org_id: Mapped[str] = mapped_column(Text, primary_key=True)
    reason: Mapped[str | None] = mapped_column(Text)
    since: Mapped[str | None] = mapped_column(Text)


class LlmProvider(ControlBase):
    """A model provider the admin registered on AI Models (control.db `llm_providers`; analyzer.providers
    writes, llm.complete reads per call). `credential` is stored as entered (founder decision, 2026-10-05): an API
    key, `<access key id>:<secret>` for Bedrock, a service-account JSON for Vertex."""
    __tablename__ = "llm_providers"

    id: Mapped[str] = mapped_column(Text, primary_key=True, nullable=True)
    name: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[str | None] = mapped_column(Text)               # anthropic | bedrock | vertex | openai | azure_openai
    endpoint: Mapped[str | None] = mapped_column(Text)           # openai-compatible base URL, Azure resource URL
    region: Mapped[str | None] = mapped_column(Text)             # Bedrock / Vertex
    project: Mapped[str | None] = mapped_column(Text)            # Vertex project id
    api_version: Mapped[str | None] = mapped_column(Text)        # Azure OpenAI
    credential: Mapped[str | None] = mapped_column(Text)
    models: Mapped[str | None] = mapped_column(Text)             # JSON list of model ids this provider serves
    created_at: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[str | None] = mapped_column(Text)
    updated_by: Mapped[str | None] = mapped_column(Text)


# --- end control plane -----------------------------------------------------------------------


class TenantConfigRow(Base):
    """The tenant's tunables — one row per org db (id is CHECK-pinned to 1).

    Every column is nullable on purpose: NULL = the built-in default, so writing one field never
    freezes the others at their current values.
    """
    __tablename__ = "tenant_config"
    __table_args__ = (CheckConstraint("id = 1"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, nullable=True, default=1)
    entity_floor: Mapped[str | None] = mapped_column(Text)        # 'default' | 'off'
    entity_gc_days: Mapped[int | None] = mapped_column(Integer)
    daily_usd: Mapped[float | None] = mapped_column(REAL)          # 0 = no cap (budget.over_budget skips it)
    daily_tokens: Mapped[int | None] = mapped_column(Integer)       # input + output tokens; 0 disables the cap
    relay_accounts: Mapped[str | None] = mapped_column(Text)      # comma-separated emails
    person_aliases: Mapped[str | None] = mapped_column(Text)      # comma-separated handle=local-part
    min_redaction: Mapped[str | None] = mapped_column(Text)       # secrets | privacy | strict
    redaction_deny: Mapped[str | None] = mapped_column(Text)      # comma-separated terms
    redaction_allow: Mapped[str | None] = mapped_column(Text)
    settle_min: Mapped[int | None] = mapped_column(Integer)         # quiet minutes before "over"
    token_ttl_days: Mapped[int | None] = mapped_column(Integer)     # 0 = tokens never expire
    updated_at: Mapped[str | None] = mapped_column(Text)
    updated_by: Mapped[str | None] = mapped_column(Text)
    registry_sources: Mapped[str | None] = mapped_column(Text)      # comma tokens: sso | github:<org> | git:<path>
    registry_refute: Mapped[str | None] = mapped_column(Text)       # subset allowed to REFUTE (2nd opt-in; verify.py)
    archive_days: Mapped[str | None] = mapped_column(Text)          # transcript retention: days, or 'forever'
    autojudge: Mapped[str | None] = mapped_column(Text)             # 'on' | 'off'
    trace_days: Mapped[int | None] = mapped_column(Integer)         # span retention in telemetry.db


KEEP_FOREVER = -1            # archive_days resolved: kept until its owner or a workspace admin forgets it
MAX_ARCHIVE_DAYS = 3650
MAX_DRAIN_WORKERS = 16       # the ETL pool's fixed size; drain_workers caps how much of it is used
MODES = ("secrets", "privacy", "strict")


def _archive_days(raw):
    """Days a transcript is kept: 0 (store none) .. MAX_ARCHIVE_DAYS, or the literal `forever`.
    Junk and negatives read as 0, fail closed: guessing high would retain text nobody was told about."""
    if str(raw).strip().lower() == "forever":
        return KEEP_FOREVER
    return max(0, min(_num(raw, int, 0), MAX_ARCHIVE_DAYS))


def _csv(raw):
    return tuple(t.strip().lower() for t in (raw or "").split(",") if t.strip())


def _tokens(raw):
    """Like _csv but case-preserving: registry tokens carry GitHub org names and repo PATHS
    (git:/Users/x/repo) where lowercasing would corrupt the value."""
    return tuple(t.strip() for t in str(raw or "").split(",") if t.strip())


def _num(raw, cast, default):
    try:
        return cast(raw)
    except (TypeError, ValueError):
        return default


# --- write-time checks: user input -> the stored value, or ValueError with a sentence to show ------

def _whole(raw, lo, hi, what):
    try:
        n = int(str(raw).strip())
    except ValueError:
        raise ValueError(f"{what} must be a whole number.") from None
    if not lo <= n <= hi:
        raise ValueError(f"{what} must be between {lo:,} and {hi:,}.")
    return n


def _items(raw):
    parts = raw if isinstance(raw, (list, tuple)) else str(raw).split(",")
    return [str(p).strip() for p in parts if str(p).strip()]


def _check_usd(raw):
    try:
        v = float(str(raw).strip())
    except ValueError:
        raise ValueError("Enter an amount in dollars, like 50.") from None
    if v < 0 or v != v or v == float("inf"):
        raise ValueError("The budget can't be negative. Use 0 for no daily cap.")
    return v


def _check_days(raw):
    try:
        v = float(str(raw).strip())
    except ValueError:
        raise ValueError("Enter a number of days, like 30.") from None
    if v < 0 or v > 3650 or v != v:
        raise ValueError("Days must be between 0 and 3,650. Use 0 to never sign anyone out.")
    return v


def _check_emails(raw):
    emails = [e.lower() for e in _items(raw)]
    bad = [e for e in emails if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", e)]
    if bad:
        raise ValueError(f"Not an email address: {bad[0]}.")
    return ",".join(emails)


def _check_aliases(raw):
    pairs = [p.lower() for p in _items(raw)]
    bad = [p for p in pairs if not re.fullmatch(r"[a-z0-9._-]+=[a-z0-9._+-]+", p)]
    if bad:
        raise ValueError(f"Not a handle=local-part pair: {bad[0]}.")
    return ",".join(pairs)


def _aliases(raw):
    return dict(p.split("=", 1) for p in _csv(raw))


def _check_terms(raw):
    terms = _items(raw)
    if any(len(t) > 200 for t in terms):
        raise ValueError("A term can be at most 200 characters.")
    return ",".join(terms)


def _check_mode(raw):
    v = str(raw).strip().lower()
    if v not in MODES:
        raise ValueError("Pick secrets, privacy or strict.")
    return v


def _check_switch(on, off):
    def check(raw):
        v = str(raw).strip().lower()
        if raw is True or v in ("on", "true", "1", on):
            return on
        if raw is False or v in ("off", "false", "0", off):
            return off
        raise ValueError("Turn it on or off.")
    return check


def _check_archive(raw):
    if str(raw).strip().lower() == "forever":
        return "forever"
    return str(_whole(raw, 0, MAX_ARCHIVE_DAYS, "Days to keep transcripts"))


def _check_registry(raw):
    tokens = _items(raw)
    bad = [t for t in tokens if not (t == "sso" or re.fullmatch(r"(github|git):\S+", t))]
    if bad:
        raise ValueError(f"Unknown source {bad[0]}. Use sso, github:<org> or git:<path>.")
    return ",".join(tokens)


def _check_model_ref(raw):
    v = str(raw).strip()
    if v and not re.fullmatch(r"[a-z0-9-]{1,40}/[A-Za-z0-9][A-Za-z0-9._:/@-]{0,159}", v):
        raise ValueError("Pick a model from a registered provider.")
    return v


def _check_url(raw):
    v = str(raw).strip()
    u = urlsplit(v)
    if u.scheme not in ("http", "https") or not u.hostname or " " in v:
        raise ValueError("Needs a full address starting with https://.")
    return v.rstrip("/")


@dataclass(frozen=True)
class Field:
    scope: str            # "workspace" (the org's tenant_config row) | "deployment" (control.db settings)
    default: object
    parse: Callable       # the stored value (or the default) -> what consumers read; never raises
    check: Callable       # user input -> the value to store; raises ValueError with a sentence to show
    secret: bool = False  # shown as its host only, never echoed back whole
    egress: bool = False  # sends data off this server: never set from a browser session, which an XSS could ride
    server_only: bool = False  # drives a subprocess with the operator's credentials: never set over HTTP


_FIELDS = {
    "daily_usd": Field("workspace", 0.0, lambda raw: _num(raw, float, 0.0), _check_usd),
    "daily_tokens": Field("workspace", 0, lambda raw: _num(raw, int, 0),
                          lambda raw: _whole(raw, 0, 10**12, "The token cap")),
    "autojudge": Field("workspace", "on", lambda raw: str(raw) != "off", _check_switch("on", "off")),
    "entity_floor": Field("workspace", "default", lambda raw: str(raw) != "off", _check_switch("default", "off")),
    "entity_gc_days": Field("workspace", 45, lambda raw: _num(raw, int, 45),
                            lambda raw: _whole(raw, 1, 3650, "Days before an unused entity is dropped")),
    "settle_min": Field("workspace", 30, lambda raw: _num(raw, int, 30),
                        lambda raw: _whole(raw, 1, 1440, "Quiet minutes")),
    "min_redaction": Field("workspace", "secrets", lambda raw: raw if raw in MODES else "secrets", _check_mode),
    "redaction_deny": Field("workspace", "", _csv, _check_terms),
    "redaction_allow": Field("workspace", "", _csv, _check_terms),
    "archive_days": Field("workspace", 0, _archive_days, _check_archive),
    "trace_days": Field("workspace", 30, lambda raw: _num(raw, int, 30),
                        lambda raw: _whole(raw, 1, 3650, "Days to keep traces")),
    "token_ttl_days": Field("workspace", 30.0, lambda raw: _num(raw, float, 30.0), _check_days),
    "relay_accounts": Field("workspace", "", lambda raw: frozenset(_csv(raw)), _check_emails),
    "person_aliases": Field("workspace", "", _aliases, _check_aliases),
    "registry_sources": Field("workspace", "", _tokens, _check_registry, server_only=True),
    "registry_refute": Field("workspace", "", _tokens, _check_registry, server_only=True),
    "main_model": Field("deployment", "", str, _check_model_ref),
    "small_model": Field("deployment", "", str, _check_model_ref),
    "drain_workers": Field("deployment", 3, lambda raw: _num(raw, int, 3),
                           lambda raw: _whole(raw, 1, MAX_DRAIN_WORKERS, "Parallel extractions")),
    "otlp_endpoint": Field("deployment", "", str, _check_url, secret=True, egress=True),
    "request_webhook": Field("deployment", "", str, _check_url, secret=True, egress=True),
}
WORKSPACE = tuple(f for f, spec in _FIELDS.items() if spec.scope == "workspace")
DEPLOYMENT = tuple(f for f, spec in _FIELDS.items() if spec.scope == "deployment")


@dataclass(frozen=True)
class TenantConfig:
    """RESOLVED tenant configuration — what consumers read. Frozen: one consistent snapshot per
    call site, never a live view that mutates mid-request."""
    daily_usd: float
    daily_tokens: int
    autojudge: bool
    entity_floor: bool              # True = the entity noise gate is on
    entity_gc_days: int
    settle_min: int
    min_redaction: str
    redaction_deny: tuple[str, ...]
    redaction_allow: tuple[str, ...]
    archive_days: int                   # 0 = no transcripts kept; KEEP_FOREVER = until forgotten
    trace_days: int
    token_ttl_days: float
    relay_accounts: frozenset[str]
    person_aliases: dict[str, str]      # a handle or nickname -> the email local-part it names
    registry_sources: tuple[str, ...]   # connected-and-complete registry tokens (absence gate OFF without)
    registry_refute: tuple[str, ...]    # tokens ALSO allowed to refute; others dry-run (would-refute receipts)


@dataclass(frozen=True)
class DeploymentConfig:
    """RESOLVED deployment-wide settings: one row in control.db, shared by every workspace."""
    main_model: str                 # "" = the Claude credential in the environment, sonnet
    small_model: str                # "" = the same, haiku
    drain_workers: int
    otlp_endpoint: str
    request_webhook: str


def _resolve(names, row: dict) -> dict:
    return {f: _FIELDS[f].parse(_FIELDS[f].default if row.get(f) is None else row[f]) for f in names}


_CONFIG_COLS = tuple(TenantConfigRow.__table__.c[f] for f in WORKSPACE)


def config(con) -> TenantConfig:
    """Resolved config for the tenant whose corpus `con` points at.

    A bridge read (db.run on that raw con), not a Session, because it is called INSIDE
    store.build_fts's BEGIN IMMEDIATE block, where a Session.close() rollback would abort the docs
    rebuild. Any failure (missing table on an old db, a bare test connection, no row yet) resolves
    to the built-in defaults.
    """
    row = {}
    stmt = select(*_CONFIG_COLS).where(TenantConfigRow.id == 1)
    try:
        fetched = _db.run(con, stmt).fetchone()
        if fetched is not None:
            row = dict(zip(stmt.selected_columns.keys(), fetched))
    except Exception:
        row = {}
    return TenantConfig(**_resolve(WORKSPACE, row))


def defaults() -> TenantConfig:
    """The built-in workspace defaults, for pre-auth surfaces that have no tenant yet (the public
    /api/policy a not-yet-signed-in client reads)."""
    return TenantConfig(**_resolve(WORKSPACE, {}))


# --- SQLAlchemy repository (CLI / API / tests write path) -----------------------------------
# Engine mechanics live in analyzer/db.py (per-path cache, NullPool, WAL/busy_timeout at connect).

def load_row(db_path: str) -> TenantConfigRow | None:
    try:
        with Session(_db.engine_for(db_path)) as session:
            return session.scalar(select(TenantConfigRow).where(TenantConfigRow.id == 1))
    except DatabaseError:                                # no table yet, or an unreadable file -> no override row
        return None


def _stored(row, names) -> dict:
    return {} if row is None else {f: getattr(row, f) for f in names}


def get_config(db_path: str) -> TenantConfig:
    return TenantConfig(**_resolve(WORKSPACE, _stored(load_row(db_path), WORKSPACE)))


def redaction_policy(con=None, db_path=None) -> tuple[str, list[str], list[str]]:
    """The redaction floor (min_mode, deny, allow) from the org's tenant_config, through its open
    connection (`con`) or its db file (`db_path`), else the built-in default."""
    try:
        cfg = config(con) if con is not None else get_config(db_path) if db_path is not None else defaults()
    except Exception:                                    # unreadable org db: the default floor
        cfg = defaults()
    return (cfg.min_redaction if cfg.min_redaction in MODES else "secrets",
            list(cfg.redaction_deny), list(cfg.redaction_allow))


def checked(updates: dict) -> tuple[dict, dict]:
    """(values to store, {field: sentence}) for `updates`. None or "" clears a field back to its
    default. An unknown field name is an error, not a no-op: a typo'd knob must not silently do
    nothing."""
    values, errors = {}, {}
    for field, raw in updates.items():
        if field not in _FIELDS:
            errors[field] = f"There is no setting called {field}."
        elif raw is None or (isinstance(raw, str) and not raw.strip()):
            values[field] = None
        else:
            try:
                values[field] = _FIELDS[field].check(raw)
            except ValueError as e:
                errors[field] = str(e)
    return values, errors


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def set_config(db_path: str, updated_by: str = "", **updates) -> TenantConfig:
    """Write workspace fields into the org db's row; None clears one back to the default. Every
    value is checked before anything is written, so a refused batch stores nothing."""
    values, errors = checked(updates)
    wrong_scope = [f for f in values if _FIELDS[f].scope != "workspace"]
    if errors or wrong_scope:
        raise ValueError("; ".join([f"{f}: {m}" for f, m in errors.items()]
                                   + [f"{f} is a deployment setting" for f in wrong_scope]))
    from analyzer import store                          # late: store imports tenant

    store.sqlite_db(db_path).close()                     # a db older than this table gets it here
    with Session(_db.engine_for(db_path)) as session:
        row = session.scalar(select(TenantConfigRow).where(TenantConfigRow.id == 1))
        if row is None:
            row = TenantConfigRow(id=1)
            session.add(row)
        for field, value in values.items():
            setattr(row, field, value)
        row.updated_at = _now()
        row.updated_by = updated_by or None
        session.commit()
        return TenantConfig(**_resolve(WORKSPACE, _stored(row, WORKSPACE)))


_SETTINGS_COLS = tuple(DeploymentSettingsRow.__table__.c[f] for f in DEPLOYMENT)
_ST = DeploymentSettingsRow.__table__


def deployment_row(con=None) -> dict:
    """The stored deployment values (None = default) plus updated_at/updated_by; {} when control.db
    has no row, does not exist yet, or cannot be read. Without `con` the file is opened read-only, so
    a per-use read never creates or migrates it."""
    from analyzer import orgs                           # late: orgs imports tenant

    own = con is None
    try:
        con = con or sqlite3.connect(f"{orgs.control_path().as_uri()}?mode=ro", uri=True)
        stmt = select(*_SETTINGS_COLS, _ST.c.updated_at, _ST.c.updated_by).where(_ST.c.id == 1)
        fetched = _db.run(con, stmt).fetchone()
        return {} if fetched is None else dict(zip(stmt.selected_columns.keys(), fetched))
    except Exception:
        return {}
    finally:
        if own and con is not None:
            con.close()


def deployment(con=None) -> DeploymentConfig:
    """Read from control.db on every call: a change on the Settings screen reaches every process
    on its next use, with no restart and no cache to go stale."""
    return DeploymentConfig(**_resolve(DEPLOYMENT, deployment_row(con)))


def _write_deployment(con, updated_by, values):
    from sqlalchemy.dialects.sqlite import insert

    row = {**values, "updated_at": _now(), "updated_by": updated_by or None}
    _db.run(con, insert(_ST).values(id=1, **row).on_conflict_do_update(index_elements=["id"], set_=row))


def set_deployment(con, updated_by: str = "", **values) -> DeploymentConfig:
    """Write already-checked deployment values into control.db's row and commit."""
    _write_deployment(con, updated_by, values)
    con.commit()
    return deployment(con)


def _shown(field, value):
    if value is None or not _FIELDS[field].secret:
        return value
    return f"{urlsplit(str(value)).scheme}://{urlsplit(str(value)).hostname}/…"


def _editable(field, deployment_editable, browser, http):
    spec = _FIELDS[field]
    return ((spec.scope == "workspace" or deployment_editable) and not (browser and spec.egress)
            and not (http and spec.server_only))


def _why_not(org_id, field, deployment_editable):
    if not deployment_editable and _FIELDS[field].scope == "deployment":
        return "This server hosts several workspaces, so its operator sets this."
    if _FIELDS[field].server_only:
        return f"This runs commands on the server, so it is set there: csa.py org config {org_id} --set {field}=..."
    return f"This sends data off the server, so it is set on the server: csa.py org config {org_id} --set {field}=<url>"


def apply(org_id: str, updates: dict, updated_by: str, deployment_editable: bool, browser: bool = False,
          http: bool = False) -> dict:
    """Check every field of both scopes, probe a changed model, then write and audit. Returns
    {field: sentence} and stores nothing when any field is refused; {} on success. The deployment
    row and every audit line commit together, and only after the workspace row is stored, so a
    failed workspace write leaves control.db untouched."""
    from analyzer import llm, orgs, store                # late: each imports tenant

    values, errors = checked(updates)
    errors.update({f: _why_not(org_id, f, deployment_editable)
                   for f in values if not _editable(f, deployment_editable, browser, http)})
    if errors:
        return errors
    db_path = store.org_db(org_id)
    con = orgs.control_db()
    try:
        before_ws, before_dep = _stored(load_row(db_path), WORKSPACE), deployment_row(con)
        problems = {f: llm.probe(values[f]) for f in ("main_model", "small_model") if values.get(f)}
        problems = {f: why for f, why in problems.items() if why}
        if problems:
            return problems
        ws = {f: v for f, v in values.items() if _FIELDS[f].scope == "workspace"}
        dep = {f: v for f, v in values.items() if _FIELDS[f].scope == "deployment"}
        if dep:
            _write_deployment(con, updated_by, dep)
        for field, value in values.items():
            old = before_ws.get(field) if field in ws else before_dep.get(field)
            if old != value:
                orgs._audit(con, "config_set", org_id if field in ws else None,
                           f"{updated_by or '-'} {field}: {_shown(field, old)} -> {_shown(field, value)}")
        try:
            if ws:
                set_config(db_path, updated_by, **ws)
        except Exception:
            con.rollback()
            raise
        con.commit()
    finally:
        con.close()
    return {}


def describe(org_id: str, deployment_editable: bool, browser: bool = False, http: bool = False) -> dict:
    """Every field's effective value, its stored override, where the value comes from, and
    whether this caller may change it; plus who last changed each scope."""
    from analyzer import store

    row = load_row(store.org_db(org_id))
    stored_ws, stored_dep = _stored(row, WORKSPACE), deployment_row()
    fields = []
    for name, spec in _FIELDS.items():
        stored = (stored_ws if spec.scope == "workspace" else stored_dep).get(name)
        fields.append({"name": name, "scope": spec.scope,
                       "value": _shown(name, stored) if stored is not None else spec.default,
                       "default": spec.default, "source": "default" if stored is None else "set",
                       "editable": _editable(name, deployment_editable, browser, http)})
    return {"fields": fields,
            "workspace_updated": {"at": getattr(row, "updated_at", None), "by": getattr(row, "updated_by", None)},
            "deployment_updated": {"at": stored_dep.get("updated_at"), "by": stored_dep.get("updated_by")},
            "deployment_editable": deployment_editable}
