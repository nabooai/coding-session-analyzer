"""invites: one link, one named person

Revision ID: aac2cbc6b106
Revises: control_base

The per-person invite link (tenant.Invite). A revision, not a baseline edit: the Alembic image is
already deployed, so every control.db in the fleet carries a version row that describes it.
"""
import sqlalchemy as sa
from alembic import op

revision = 'aac2cbc6b106'
down_revision = 'control_base'


def upgrade():
    op.create_table('invites',
    sa.Column('id', sa.Text(), nullable=True),
    sa.Column('org_id', sa.Text(), nullable=True),
    sa.Column('email', sa.Text(), nullable=True),
    sa.Column('token_hash', sa.Text(), nullable=True),
    sa.Column('created_by', sa.Text(), nullable=True),
    sa.Column('created_at', sa.Text(), nullable=True),
    sa.Column('expires_at', sa.Text(), nullable=True),
    sa.Column('used_at', sa.Text(), nullable=True),
    sa.Column('revoked_at', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_invites_org', 'invites', ['org_id'], unique=False)


def downgrade():
    raise NotImplementedError("schemas are forward only; a rollback is an image revert")
