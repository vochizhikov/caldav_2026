"""Configure how many dates with upcoming events to show."""

import sqlalchemy as sa
from alembic import op

revision = "a28c65b971f3"
down_revision = "d9f7b2a6c410"
branch_labels = None
depends_on = None


def upgrade():
    # An inline constraint avoids rebuilding users and cascading deletion of related rows.
    op.add_column(
        "users",
        sa.Column(
            "upcoming_event_days",
            sa.Integer(),
            sa.CheckConstraint(
                "upcoming_event_days BETWEEN 1 AND 7", name="ck_users_upcoming_event_days"
            ),
            nullable=False,
            server_default="1",
        ),
    )


def downgrade():
    op.drop_column("users", "upcoming_event_days")
