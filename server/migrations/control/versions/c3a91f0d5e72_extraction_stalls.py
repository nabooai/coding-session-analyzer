"""extraction_stalls

Revision ID: c3a91f0d5e72
Revises: 0dc6c620c183

One row per workspace whose extraction is failing for a systemic reason (a bad API key), so the api
can say why nothing is draining.
"""
import sqlalchemy as sa
from alembic import op

revision = 'c3a91f0d5e72'
down_revision = '0dc6c620c183'


def upgrade():
    op.create_table(
        'extraction_stalls',
        sa.Column('org_id', sa.Text(), nullable=False),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('since', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('org_id'),
    )


def downgrade():
    raise NotImplementedError("schemas are forward only; a rollback is an image revert")
