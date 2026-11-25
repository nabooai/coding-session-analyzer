"""tenant_config autojudge and trace_days

Revision ID: 9bb4882bc98f
Revises: b2d8f5c13a07

The per-workspace switches that were CSA_AUTOJUDGE and CSA_SPANS_KEEP_DAYS. NULL is the built-in
default: judging on, traces kept 30 days.
"""
import sqlalchemy as sa
from alembic import op

revision = '9bb4882bc98f'
down_revision = 'b2d8f5c13a07'


def upgrade():
    op.add_column('tenant_config', sa.Column('autojudge', sa.Text(), nullable=True))
    op.add_column('tenant_config', sa.Column('trace_days', sa.Integer(), nullable=True))


def downgrade():
    raise NotImplementedError("schemas are forward only; a rollback is an image revert")
