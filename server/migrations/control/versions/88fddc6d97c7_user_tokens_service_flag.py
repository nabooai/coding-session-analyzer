"""user_tokens: service flag

Revision ID: 88fddc6d97c7
Revises: 11480cc6f08a

Marks a token minted from Team > Service token, so orgs.set_role never promotes it.
"""
import sqlalchemy as sa
from alembic import op

revision = '88fddc6d97c7'
down_revision = '11480cc6f08a'


def upgrade():
    op.add_column('user_tokens', sa.Column('service', sa.Integer(), nullable=True))


def downgrade():
    raise NotImplementedError("schemas are forward only; a rollback is an image revert")
