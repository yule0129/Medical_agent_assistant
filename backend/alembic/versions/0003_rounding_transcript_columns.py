"""Add recording metadata omitted from existing rounding tables.

create_all(checkfirst=True) only creates missing tables; it cannot add columns
to a table created before recording support was introduced.
"""
from alembic import op
import sqlalchemy as sa

revision = "0003_rounding_transcript_columns"
down_revision = "0002_rounding_workflow"
branch_labels = None
depends_on = None


def upgrade():
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("rounding_sessions")}
    if "audio_filename" not in columns:
        op.add_column("rounding_sessions", sa.Column("audio_filename", sa.String(255), nullable=True))
    if "transcript_source" not in columns:
        op.add_column("rounding_sessions", sa.Column("transcript_source", sa.String(30), nullable=False, server_default="manual"))


def downgrade():
    # Retain provenance and history; destructive removal needs a separate plan.
    pass
