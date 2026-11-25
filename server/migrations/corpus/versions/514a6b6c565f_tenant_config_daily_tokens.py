"""tenant_config daily_tokens

Revision ID: 514a6b6c565f
Revises: corpus_base

The per-workspace daily token cap beside daily_usd (tenant.TenantConfigRow). NULL inherits the
default of 0, which is off.
"""
import sqlalchemy as sa
from alembic import op

revision = '514a6b6c565f'
down_revision = 'corpus_base'


def upgrade():
    op.add_column('tenant_config', sa.Column('daily_tokens', sa.Integer(), nullable=True))


def downgrade():
    raise NotImplementedError("schemas are forward only; a rollback is an image revert")
