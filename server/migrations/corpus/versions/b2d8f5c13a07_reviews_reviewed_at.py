"""reviews.reviewed_at, in the model at last

Revision ID: b2d8f5c13a07
Revises: 514a6b6c565f

The column exists on old corpora (prod has it) but was never on the Review model, so `set_review`
could not write it and every one of prod's 3,933 rows has a NULL timestamp. That makes "was this
fact labelled before or after we served it" unanswerable, which is the exact question you ask when
deciding whether a rejected fact leaked into someone's context.

Adding it to the model is the fix; this migration only reconciles corpora that lack the column.
Rows written before it stay NULL forever, so any before/after analysis has to exclude them.
"""
import sqlalchemy as sa
from alembic import op

revision = 'b2d8f5c13a07'
down_revision = '514a6b6c565f'


def upgrade():
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("reviews")}
    if "reviewed_at" not in cols:                    # pre-Alembic corpora already carry it
        op.add_column('reviews', sa.Column('reviewed_at', sa.Text(), nullable=True))


def downgrade():
    op.drop_column('reviews', 'reviewed_at')
