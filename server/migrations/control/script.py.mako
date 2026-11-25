"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
"""
import sqlalchemy as sa
from alembic import op

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
${imports if imports else ""}

def upgrade():
    ${upgrades if upgrades else "pass"}


def downgrade():
    raise NotImplementedError("schemas are forward only; a rollback is an image revert")
