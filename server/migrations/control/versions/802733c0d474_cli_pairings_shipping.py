"""cli_pairings.shipping

Revision ID: 802733c0d474
Revises: c3a91f0d5e72

Which plugin mode started a pairing, so /cli-auth states that mode's consent from the row rather
than from anything in the link.
"""
import sqlalchemy as sa
from alembic import op

revision = '802733c0d474'
down_revision = 'c3a91f0d5e72'


def upgrade():
    op.add_column('cli_pairings', sa.Column('shipping', sa.Text(), nullable=True))


def downgrade():
    raise NotImplementedError("schemas are forward only; a rollback is an image revert")
