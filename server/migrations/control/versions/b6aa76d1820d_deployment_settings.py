"""deployment settings

Revision ID: b6aa76d1820d
Revises: 88fddc6d97c7

Deployment-wide settings every workspace shares (tenant.DeploymentSettingsRow), one row. NULL is
the built-in default.
"""
import sqlalchemy as sa
from alembic import op

revision = 'b6aa76d1820d'
down_revision = '88fddc6d97c7'


def upgrade():
    op.create_table('settings',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('llm_provider', sa.Text(), nullable=True),
    sa.Column('extraction_model', sa.Text(), nullable=True),
    sa.Column('openai_model', sa.Text(), nullable=True),
    sa.Column('drain_workers', sa.Integer(), nullable=True),
    sa.Column('otlp_endpoint', sa.Text(), nullable=True),
    sa.Column('request_webhook', sa.Text(), nullable=True),
    sa.Column('updated_at', sa.Text(), nullable=True),
    sa.Column('updated_by', sa.Text(), nullable=True),
    sa.CheckConstraint('id = 1'),
    sa.PrimaryKeyConstraint('id')
    )


def downgrade():
    raise NotImplementedError("schemas are forward only; a rollback is an image revert")
