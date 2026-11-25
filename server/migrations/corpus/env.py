"""corpus environment: one org's analyzer.db, attached as main"""
from alembic import context

from analyzer import migrations, models         # noqa: F401  (models registers the tables)
from analyzer import tenant

migrations.run(context.config, tenant.Base.metadata, "corpus")
