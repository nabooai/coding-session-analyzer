"""tenant_config person_aliases

Revision ID: c4e7a19d02b6
Revises: 9bb4882bc98f

The per-workspace handle=local-part list that resolve_person and resolve_owner read
(tenant.TenantConfigRow). NULL inherits the default of none.
"""
import sqlalchemy as sa
from alembic import op

revision = 'c4e7a19d02b6'
down_revision = '9bb4882bc98f'


def upgrade():
    op.add_column('tenant_config', sa.Column('person_aliases', sa.Text(), nullable=True))


def downgrade():
    raise NotImplementedError("schemas are forward only; a rollback is an image revert")
