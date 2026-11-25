"""telemetry baseline: served, queries and spans as of the Alembic adoption

Revision ID: telemetry_base
Revises:

Runs against the telemetry FILE, attached as `tel` on the corpus connection, so every statement and
this file's own alembic_version land there and not in the corpus. Catch-up, same as the corpus
baseline: tracer.py may have created `spans` first on its own stdlib connection.
"""
import sqlalchemy as sa

from analyzer import migrations

revision = "telemetry_base"
down_revision = None

_META = sa.MetaData()

sa.Table(
    "queries", _META,
    sa.Column('at', sa.Text()),
    sa.Column('asked_by', sa.Text()),
    sa.Column('session', sa.Text()),
    sa.Column('engine', sa.Text()),
    sa.Column('query_text', sa.Text()),
    sa.Column('planned_query', sa.Text()),
    sa.Column('row_count', sa.Integer()),
    sa.Column('latency_ms', sa.Integer()),
    sa.Column('acted_on', sa.Integer(), server_default=sa.text('0')),
    sa.Column('result_ids', sa.Text()),
    sa.Column('trace_id', sa.Text()),
    schema="tel",
)

sa.Table(
    "served", _META,
    sa.Column('session', sa.Text()),
    sa.Column('asked_at', sa.Text()),
    sa.Column('question', sa.Text()),
    sa.Column('ref', sa.Text()),
    sa.Column('kind', sa.Text()),
    sa.Column('project', sa.Text()),
    sa.Column('person', sa.Text()),
    sa.Column('asked_by', sa.Text()),
    sa.Column('trace_id', sa.Text()),
    schema="tel",
)

sa.Table(
    "spans", _META,
    sa.Column('trace_id', sa.Text()),
    sa.Column('span_id', sa.Text()),
    sa.Column('parent_id', sa.Text()),
    sa.Column('name', sa.Text()),
    sa.Column('kind', sa.Text()),
    sa.Column('at', sa.Text()),
    sa.Column('dur_ms', sa.Integer()),
    sa.Column('status', sa.Text()),
    sa.Column('attrs', sa.Text()),
    schema="tel",
)

sa.Table(
    "ui_events", _META,
    sa.Column('id', sa.Integer(), nullable=True),
    sa.Column('at', sa.Text(), nullable=False),
    sa.Column('user', sa.Text(), nullable=False),
    sa.Column('event', sa.Text(), nullable=False),
    sa.Column('page', sa.Text(), nullable=False),
    sa.Column('meta', sa.Text()),
    sa.PrimaryKeyConstraint('id'),
    schema="tel",
)

sa.Index("ix_queries_engine", _META.tables['tel.queries'].c.engine)

sa.Index("ix_queries_session", _META.tables['tel.queries'].c.session)

sa.Index("ix_served_ref", _META.tables['tel.served'].c.ref, _META.tables['tel.served'].c.session)

sa.Index("ix_served_session", _META.tables['tel.served'].c.session)

sa.Index("ix_spans_at", _META.tables['tel.spans'].c.at)

sa.Index("ix_spans_trace", _META.tables['tel.spans'].c.trace_id)

sa.Index("ix_tel_ui_events_at", _META.tables['tel.ui_events'].c.at)


def upgrade():
    migrations.catch_up(_META)


def downgrade():
    raise NotImplementedError("schemas are forward only; a rollback is an image revert")
