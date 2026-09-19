"""Add an optional catalog view for upcoming events."""

import sqlalchemy as sa
from alembic import op

revision = "ce0bfe7244a1"
down_revision = "a28c65b971f3"
branch_labels = None
depends_on = None


def upgrade():
    # Add the column in place to preserve users and all related calendar data.
    op.add_column(
        "users",
        sa.Column("upcoming_catalog_mode", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade():
    op.drop_column("users", "upcoming_catalog_mode")
