"""corpus baseline: the tables as of the Alembic adoption

Revision ID: corpus_base
Revises:

A CATCH-UP, not a create: a corpus that predates Alembic was built by some past version of the old
`store.SCHEMA` string plus a try/except ALTER list, so this creates what is missing and adds the
columns a file lacks, then the file is stamped. Later revisions are ordinary op.add_column calls.
The table snapshot below is FROZEN: it must not be re-derived from the models, or a model change
with no revision would still produce a correct fresh file and be missed until prod.
"""
import sqlalchemy as sa
from alembic import op

from analyzer import migrations

revision = "corpus_base"
down_revision = None

_META = sa.MetaData()

sa.Table(
    "actions", _META,
    sa.Column('session_id', sa.Text()),
    sa.Column('verb', sa.Text()),
    sa.Column('target_id', sa.Text()),
    sa.Column('context', sa.Text()),
)

sa.Table(
    "aliases", _META,
    sa.Column('alias_id', sa.Text(), primary_key=True, nullable=True),
    sa.Column('canonical_id', sa.Text()),
)

sa.Table(
    "claim_clusters", _META,
    sa.Column('ref', sa.Text(), primary_key=True, nullable=True),
    sa.Column('cluster_id', sa.Text()),
    sa.Column('sessions', sa.Integer()),
)

sa.Table(
    "costs", _META,
    sa.Column('at', sa.Text()),
    sa.Column('step', sa.Text()),
    sa.Column('model', sa.Text()),
    sa.Column('session_id', sa.Text()),
    sa.Column('input_tokens', sa.Integer()),
    sa.Column('output_tokens', sa.Integer()),
    sa.Column('cache_read_tokens', sa.Integer()),
    sa.Column('cost_usd', sa.REAL()),
    sa.Column('trace_id', sa.Text()),
)

sa.Table(
    "custom_entity_types", _META,
    sa.Column('key', sa.Text(), primary_key=True, nullable=True),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('created_at', sa.Text()),
    sa.Column('updated_at', sa.Text()),
    sa.Column('updated_by', sa.Text()),
)

sa.Table(
    "data_sources", _META,
    sa.Column('session_id', sa.Text()),
    sa.Column('name', sa.Text()),
)

sa.Table(
    "entities", _META,
    sa.Column('id', sa.Text(), primary_key=True, nullable=True),
    sa.Column('type', sa.Text()),
    sa.Column('name', sa.Text()),
    sa.Column('attributes', sa.Text()),
)

sa.Table(
    "entity_stats", _META,
    sa.Column('entity_id', sa.Text(), primary_key=True, nullable=True),
    sa.Column('sessions', sa.Integer()),
    sa.Column('distinct_authors', sa.Integer()),
    sa.Column('dominant_n', sa.Integer()),
    sa.Column('acted_n', sa.Integer()),
    sa.Column('last_seen', sa.Text()),
    sa.Column('name_sessions', sa.Integer()),
    sa.Column('headline_n', sa.Integer()),
)

sa.Table(
    "entity_types", _META,
    sa.Column('id', sa.Text(), primary_key=True, nullable=True),
    sa.Column('type', sa.Text()),
)

sa.Table(
    "fact_edges", _META,
    sa.Column('src_ref', sa.Text(), primary_key=True, nullable=True),
    sa.Column('rel', sa.Text(), primary_key=True, nullable=True),
    sa.Column('dst_ref', sa.Text(), primary_key=True, nullable=True),
    sa.Column('at', sa.Text()),
    sa.Column('actor', sa.Text()),
    sa.Column('note', sa.Text()),
)

sa.Table(
    "facts", _META,
    sa.Column('ref', sa.Text(), primary_key=True, nullable=True),
    sa.Column('claim', sa.Text()),
    sa.Column('type', sa.Text()),
    sa.Column('scope', sa.Text()),
    sa.Column('author', sa.Text()),
    sa.Column('domain', sa.Text()),
    sa.Column('created_at', sa.Text()),
    sa.Column('valid_at', sa.Text()),
    sa.Column('invalid_at', sa.Text()),
    sa.Column('expired_at', sa.Text()),
    sa.Column('state', sa.Text(), server_default=sa.text("'extracted'")),
    sa.Column('confidence', sa.REAL()),
)

sa.Table(
    "harvest_actions", _META,
    sa.Column('session_id', sa.Text(), primary_key=True, nullable=True),
    sa.Column('verb', sa.Text(), primary_key=True, nullable=True),
    sa.Column('target_id', sa.Text(), primary_key=True, nullable=True),
    sa.Column('context', sa.Text(), primary_key=True, nullable=True),
)

sa.Table(
    "harvest_links", _META,
    sa.Column('session_id', sa.Text(), primary_key=True, nullable=True),
    sa.Column('url', sa.Text(), primary_key=True, nullable=True),
    sa.Column('title', sa.Text(), primary_key=True, nullable=True),
)

sa.Table(
    "harvest_mentions", _META,
    sa.Column('session_id', sa.Text(), primary_key=True, nullable=True),
    sa.Column('entity_id', sa.Text(), primary_key=True, nullable=True),
)

sa.Table(
    "hc_types", _META,
    sa.Column('ref', sa.Text(), primary_key=True, nullable=True),
    sa.Column('type', sa.Text()),
)

sa.Table(
    "hidden_context", _META,
    sa.Column('session_id', sa.Text()),
    sa.Column('type', sa.Text()),
    sa.Column('scope', sa.Text()),
    sa.Column('claim', sa.Text()),
    sa.Column('evidence', sa.Text()),
    sa.Column('ref', sa.Text()),
    sa.Column('confidence', sa.Text()),
    sa.Column('repo', sa.Text()),
    sa.Column('path', sa.Text()),
    sa.Column('as_of', sa.Text()),
)

sa.Table(
    "links", _META,
    sa.Column('session_id', sa.Text()),
    sa.Column('url', sa.Text()),
    sa.Column('title', sa.Text()),
)

sa.Table(
    "mentions", _META,
    sa.Column('session_id', sa.Text()),
    sa.Column('entity_id', sa.Text()),
    sa.Column('dominant', sa.Integer(), server_default=sa.text('0')),
)

sa.Table(
    "pr_links", _META,
    sa.Column('session_id', sa.Text(), primary_key=True, nullable=True),
    sa.Column('repo', sa.Text(), primary_key=True, nullable=True),
    sa.Column('pr_number', sa.Integer(), primary_key=True, nullable=True),
    sa.Column('url', sa.Text()),
    sa.Column('first_seen', sa.Text()),
    sa.Column('last_seen', sa.Text()),
    sa.Column('n_events', sa.Integer()),
)

sa.Table(
    "registry", _META,
    sa.Column('kind', sa.Text(), primary_key=True, nullable=True),
    sa.Column('key', sa.Text(), primary_key=True, nullable=True),
    sa.Column('value', sa.Text()),
    sa.Column('as_of', sa.Text()),
    sa.Column('source', sa.Text(), primary_key=True, nullable=True),
)

sa.Table(
    "relations", _META,
    sa.Column('session_id', sa.Text()),
    sa.Column('source_id', sa.Text()),
    sa.Column('rel', sa.Text()),
    sa.Column('target_id', sa.Text()),
    sa.Column('context', sa.Text()),
)

sa.Table(
    "reviews", _META,
    sa.Column('kind', sa.Text(), primary_key=True, nullable=True),
    sa.Column('ref', sa.Text(), primary_key=True, nullable=True),
    sa.Column('status', sa.Text()),
    sa.Column('note', sa.Text()),
    sa.Column('flagger', sa.Text()),
)

sa.Table(
    "session_metrics", _META,
    sa.Column('session_id', sa.Text(), primary_key=True, nullable=True),
    sa.Column('model', sa.Text()),
    sa.Column('tool_calls', sa.Integer()),
    sa.Column('tool_errors', sa.Integer()),
    sa.Column('dup_calls', sa.Integer()),
    sa.Column('reread_calls', sa.Integer()),
    sa.Column('top_read', sa.Text()),
    sa.Column('top_read_n', sa.Integer()),
    sa.Column('top_tool', sa.Text()),
    sa.Column('in_tokens', sa.Integer()),
    sa.Column('out_tokens', sa.Integer()),
    sa.Column('cache_read_tokens', sa.Integer()),
    sa.Column('cache_write_tokens', sa.Integer()),
    sa.Column('cost_usd', sa.REAL()),
    sa.Column('wall_s', sa.Integer()),
    sa.Column('idle_s', sa.Integer()),
    sa.Column('max_gap_s', sa.Integer()),
)

sa.Table(
    "session_workspace", _META,
    sa.Column('session_id', sa.Text(), primary_key=True, nullable=True),
    sa.Column('kind', sa.Text(), primary_key=True, nullable=True),
    sa.Column('value', sa.Text(), primary_key=True, nullable=True),
)

sa.Table(
    "sessions", _META,
    sa.Column('session_id', sa.Text(), primary_key=True, nullable=True),
    sa.Column('project', sa.Text()),
    sa.Column('git_branch', sa.Text()),
    sa.Column('started_at', sa.Text()),
    sa.Column('ended_at', sa.Text()),
    sa.Column('n_user', sa.Integer()),
    sa.Column('n_asst', sa.Integer()),
    sa.Column('summary', sa.Text()),
    sa.Column('deployed_to_prod', sa.Integer()),
    sa.Column('source', sa.Text(), server_default=sa.text("'session'")),
    sa.Column('user', sa.Text(), server_default=sa.text("'owner'")),
    sa.Column('harness', sa.Text(), server_default=sa.text("'claude-code'")),
    sa.Column('redaction_mode', sa.Text(), server_default=sa.text("'secrets'")),
    sa.Column('shipped_by', sa.Text()),
)

sa.Table(
    "signals", _META,
    sa.Column('ref', sa.Text(), primary_key=True, nullable=True),
    sa.Column('kind', sa.Text(), primary_key=True, nullable=True),
    sa.Column('session_id', sa.Text(), primary_key=True, nullable=True),
    sa.Column('at', sa.Text()),
)

sa.Table(
    "tenant_config", _META,
    sa.Column('id', sa.Integer(), primary_key=True, nullable=True),
    sa.Column('entity_floor', sa.Text()),
    sa.Column('entity_gc_days', sa.Integer()),
    sa.Column('daily_usd', sa.REAL()),
    sa.Column('relay_accounts', sa.Text()),
    sa.Column('min_redaction', sa.Text()),
    sa.Column('redaction_deny', sa.Text()),
    sa.Column('redaction_allow', sa.Text()),
    sa.Column('settle_min', sa.Integer()),
    sa.Column('token_ttl_days', sa.Integer()),
    sa.Column('updated_at', sa.Text()),
    sa.Column('updated_by', sa.Text()),
    sa.Column('registry_sources', sa.Text()),
    sa.Column('registry_refute', sa.Text()),
    sa.Column('archive_days', sa.Text()),
    sa.CheckConstraint('id = 1'),
)

sa.Table(
    "worked_on", _META,
    sa.Column('session_id', sa.Text()),
    sa.Column('item', sa.Text()),
)

sa.Index("ix_actions_tid", _META.tables['actions'].c.target_id)

sa.Index("ix_actions_verb", _META.tables['actions'].c.verb.collate("NOCASE"))

sa.Index("ix_entities_type", _META.tables['entities'].c.type)

sa.Index("ix_fact_edges_dst", _META.tables['fact_edges'].c.dst_ref)

sa.Index("ix_hc_ref", _META.tables['hidden_context'].c.ref)

sa.Index("ix_hc_session", _META.tables['hidden_context'].c.session_id)

sa.Index("ix_mentions_eid", _META.tables['mentions'].c.entity_id)

sa.Index("ix_mentions_sid", _META.tables['mentions'].c.session_id)

sa.Index("ix_pr_links_n", _META.tables['pr_links'].c.pr_number)

sa.Index("ix_relations_src", _META.tables['relations'].c.source_id)

sa.Index("ix_relations_tgt", _META.tables['relations'].c.target_id)


def upgrade():
    migrations.catch_up(_META)
    # Raw by design. `docs` is an FTS5 virtual table with a tokenizer (SQLAlchemy has no FTS5
    # dialect worth adopting) and it is a derived index: store.build_fts drops and recreates it
    # from the same DOCS_COLS, so the shape is read from there rather than frozen here.
    from analyzer.store import DOCS_COLS

    op.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5({DOCS_COLS})")
    op.execute("CREATE VIEW IF NOT EXISTS session_dirs AS "
               "SELECT session_id, value AS path FROM session_workspace WHERE kind='dir'")
    op.execute("CREATE VIEW IF NOT EXISTS session_branches AS "
               "SELECT session_id, value AS branch FROM session_workspace WHERE kind='branch'")


def downgrade():
    raise NotImplementedError("schemas are forward only; a rollback is an image revert")
