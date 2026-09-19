"""Track which event components changed without notifying about historical edits."""

import sqlalchemy as sa
from alembic import op

revision = "b783d140f632"
down_revision = "79843f9da12b"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("events", sa.Column("change_state", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("events", "change_state")
