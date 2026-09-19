"""Keep meeting rooms and conference links for event cards and notifications."""

import sqlalchemy as sa
from alembic import op

revision = "d9f7b2a6c410"
down_revision = "b783d140f632"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("events", sa.Column("location", sa.Text(), nullable=False, server_default=""))
    op.add_column("events", sa.Column("meeting_url", sa.Text(), nullable=False, server_default=""))
    op.add_column(
        "occurrences", sa.Column("meeting_url", sa.Text(), nullable=False, server_default="")
    )


def downgrade():
    op.drop_column("occurrences", "meeting_url")
    op.drop_column("events", "meeting_url")
    op.drop_column("events", "location")
