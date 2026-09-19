"""Silently rebuild fingerprints after normalizing repeated iCalendar properties."""

import sqlalchemy as sa
from alembic import op

revision = "79843f9da12b"
down_revision = "4b1c62a780ef"
branch_labels = None
depends_on = None


def upgrade():
    # Old hashes cannot be compared to the new canonical representation.
    # Keep calendar selection and credentials, but start with a silent baseline.
    calendars = sa.table(
        "calendars",
        sa.column("initialized", sa.Boolean()),
        sa.column("last_synced_at", sa.DateTime(timezone=True)),
        sa.column("notifications_since", sa.DateTime(timezone=True)),
    )
    notifications = sa.table(
        "notifications",
        sa.column("status", sa.String()),
    )
    op.execute(
        calendars.update().values(
            initialized=False,
            last_synced_at=None,
            notifications_since=None,
        )
    )
    op.execute(
        notifications.update().where(notifications.c.status == "pending").values(status="discarded")
    )


def downgrade():
    # Previously discarded messages must never be replayed by a rollback.
    pass
