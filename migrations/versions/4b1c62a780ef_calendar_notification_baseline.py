"""Persist the notification boundary after enabling a calendar."""

import sqlalchemy as sa
from alembic import op

revision = "4b1c62a780ef"
down_revision = "2dc34cb21c87"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("calendars", sa.Column("notifications_since", sa.DateTime(timezone=True)))


def downgrade():
    op.drop_column("calendars", "notifications_since")
