"""control baseline: orgs, tokens, requests and directives as of the Alembic adoption

Revision ID: control_base
Revises:

Catch-up over the old orgs.SCHEMA + _ADDED_COLUMNS. control.db stays in rollback-journal mode: this
runs on orgs.control_db()'s connection, which sets no pragmas, and nothing here sets one.
"""
import sqlalchemy as sa
from alembic import op

from analyzer import migrations

revision = "control_base"
down_revision = None

_META = sa.MetaData()

sa.Table(
    "admin_log", _META,
    sa.Column('at', sa.Text()),
    sa.Column('action', sa.Text()),
    sa.Column('org_id', sa.Text()),
    sa.Column('detail', sa.Text()),
)

sa.Table(
    "client_directive_acks", _META,
    sa.Column('directive_id', sa.Text(), primary_key=True, nullable=True),
    sa.Column('token_hash', sa.Text(), primary_key=True, nullable=True),
    sa.Column('email', sa.Text()),
    sa.Column('hostname', sa.Text()),
    sa.Column('at', sa.Text()),
    sa.Column('status', sa.Text()),
    sa.Column('shipped', sa.Integer()),
    sa.Column('detail', sa.Text()),
)

sa.Table(
    "cli_pairings", _META,
    sa.Column('nonce_hash', sa.Text(), primary_key=True, nullable=True),
    sa.Column('hostname', sa.Text()),
    sa.Column('created', sa.Text()),
    sa.Column('approved_at', sa.Text()),
    sa.Column('email', sa.Text()),
    sa.Column('org_id', sa.Text()),
    sa.Column('verified', sa.Integer()),
    sa.Column('claimed_at', sa.Text()),
)

sa.Table(
    "client_directives", _META,
    sa.Column('id', sa.Text(), primary_key=True, nullable=True),
    sa.Column('org_id', sa.Text()),
    sa.Column('target_email', sa.Text()),
    sa.Column('target_hostname', sa.Text()),
    sa.Column('since', sa.Text()),
    sa.Column('created', sa.Text()),
    sa.Column('created_by', sa.Text()),
    sa.Column('revoked_at', sa.Text()),
)

sa.Table(
    "orgs", _META,
    sa.Column('id', sa.Text(), primary_key=True, nullable=True),
    sa.Column('name', sa.Text()),
    sa.Column('invite_hash', sa.Text()),
    sa.Column('created', sa.Text()),
    sa.Column('domain', sa.Text()),
)

sa.Table(
    "user_tokens", _META,
    sa.Column('token_hash', sa.Text(), primary_key=True, nullable=True),
    sa.Column('email', sa.Text()),
    sa.Column('org_id', sa.Text()),
    sa.Column('hostname', sa.Text()),
    sa.Column('created', sa.Text()),
    sa.Column('revoked_at', sa.Text()),
    sa.Column('last_used_at', sa.Text()),
    sa.Column('expires_at', sa.Text()),
    sa.Column('role', sa.Text()),
)

sa.Table(
    "workspace_requests", _META,
    sa.Column('id', sa.Text(), primary_key=True, nullable=True),
    sa.Column('company', sa.Text()),
    sa.Column('email', sa.Text()),
    sa.Column('company_norm', sa.Text()),
    sa.Column('email_norm', sa.Text()),
    sa.Column('created', sa.Text()),
    sa.Column('status', sa.Text()),
    sa.Column('handled_at', sa.Text()),
    sa.Column('org_id', sa.Text()),
)

sa.Index("ix_client_directives_org", _META.tables['client_directives'].c.org_id)

sa.Index("ix_user_tokens_org", _META.tables['user_tokens'].c.org_id)

sa.Index("ux_workspace_requests_norm", _META.tables['workspace_requests'].c.company_norm, _META.tables['workspace_requests'].c.email_norm, unique=True)


def upgrade():
    tokens = _META.tables["user_tokens"]
    dating = "expires_at" not in {r[1] for r in op.get_bind().exec_driver_sql("PRAGMA table_info(user_tokens)")}
    migrations.catch_up(_META)
    if dating and op.get_bind().scalar(sa.select(sa.func.count()).select_from(tokens)):
        from analyzer import orgs

        orgs.date_undated(op.get_bind().connection)      # tokens minted before expiry existed are
        #   immortal by construction. Dating them from their LAST USE keeps a laptop that still
        #   ships working and lets one that went quiet longer ago than the TTL go cold on its own.


def downgrade():
    raise NotImplementedError("schemas are forward only; a rollback is an image revert")
