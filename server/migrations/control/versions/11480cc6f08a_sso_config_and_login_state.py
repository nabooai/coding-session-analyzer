"""sso config and login state

Revision ID: 11480cc6f08a
Revises: aac2cbc6b106

The one OpenID issuer a deployment signs in with (tenant.Sso) and the browser sign-ins in flight
between /api/sso/start and /api/sso/callback (tenant.SsoLogin).
"""
import sqlalchemy as sa
from alembic import op

revision = '11480cc6f08a'
down_revision = 'aac2cbc6b106'


def upgrade():
    op.create_table('sso',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('issuer', sa.Text(), nullable=True),
    sa.Column('client_id', sa.Text(), nullable=True),
    sa.Column('client_secret', sa.Text(), nullable=True),
    sa.Column('display_name', sa.Text(), nullable=True),
    sa.Column('updated_at', sa.Text(), nullable=True),
    sa.Column('updated_by', sa.Text(), nullable=True),
    sa.CheckConstraint('id = 1'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('sso_logins',
    sa.Column('state_hash', sa.Text(), nullable=False),
    sa.Column('nonce', sa.Text(), nullable=True),
    sa.Column('verifier', sa.Text(), nullable=True),
    sa.Column('join_token', sa.Text(), nullable=True),
    sa.Column('return_path', sa.Text(), nullable=True),
    sa.Column('created', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('state_hash')
    )


def downgrade():
    raise NotImplementedError("schemas are forward only; a rollback is an image revert")
