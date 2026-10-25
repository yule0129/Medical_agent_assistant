"""Persist rounding provenance and request identity; preserve existing history."""
from alembic import op
import sqlalchemy as sa

revision = "0002_rounding_workflow"
down_revision = "0001_workflow_foundation"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if "target" not in {c["name"] for c in sa.inspect(bind).get_columns("clinical_intents")}:
        op.add_column("clinical_intents", sa.Column("target", sa.String(120), nullable=True))
    if "request_key" not in {c["name"] for c in sa.inspect(bind).get_columns("rounding_sessions")}:
        op.add_column("rounding_sessions", sa.Column("request_key", sa.String(100), nullable=True))
        op.create_unique_constraint("uq_rounding_request_key", "rounding_sessions", ["request_key"])
    if "source_rounding_session_id" not in {c["name"] for c in sa.inspect(bind).get_columns("medical_records")}:
        op.add_column("medical_records", sa.Column("source_rounding_session_id", sa.Integer(), sa.ForeignKey("rounding_sessions.id"), nullable=True))


def downgrade():
    # Keep provenance and history. Destructive removal requires a separate plan.
    pass
