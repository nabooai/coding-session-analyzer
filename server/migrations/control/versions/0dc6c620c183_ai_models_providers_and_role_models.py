"""ai models: providers and role models

Revision ID: 0dc6c620c183
Revises: b6aa76d1820d

AI Models (analyzer.providers): the registered providers, and the two role choices that replace
llm_provider / extraction_model / openai_model. SQLite 3.35+ drops a column in place (no table copy),
and nothing on that row carries a CHECK besides id = 1.
"""
import sqlalchemy as sa
from alembic import op

revision = '0dc6c620c183'
down_revision = 'b6aa76d1820d'


def upgrade():
    op.create_table('llm_providers',
    sa.Column('id', sa.Text(), nullable=True),
    sa.Column('name', sa.Text(), nullable=True),
    sa.Column('kind', sa.Text(), nullable=True),
    sa.Column('endpoint', sa.Text(), nullable=True),
    sa.Column('region', sa.Text(), nullable=True),
    sa.Column('project', sa.Text(), nullable=True),
    sa.Column('api_version', sa.Text(), nullable=True),
    sa.Column('credential', sa.Text(), nullable=True),
    sa.Column('models', sa.Text(), nullable=True),
    sa.Column('created_at', sa.Text(), nullable=True),
    sa.Column('updated_at', sa.Text(), nullable=True),
    sa.Column('updated_by', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.add_column('settings', sa.Column('main_model', sa.Text(), nullable=True))
    op.add_column('settings', sa.Column('small_model', sa.Text(), nullable=True))
    op.drop_column('settings', 'extraction_model')
    op.drop_column('settings', 'openai_model')
    op.drop_column('settings', 'llm_provider')


def downgrade():
    raise NotImplementedError("schemas are forward only; a rollback is an image revert")
