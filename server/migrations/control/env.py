"""control environment: data/control.db"""
from alembic import context

from analyzer import migrations, tenant

migrations.run(context.config, tenant.ControlBase.metadata, "control")
