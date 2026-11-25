"""telemetry environment: its telemetry.db sibling, attached as tel"""
from alembic import context

from analyzer import migrations, models

migrations.run(context.config, models.telemetry, "telemetry")
