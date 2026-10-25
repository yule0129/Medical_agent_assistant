"""Report versions, verified evidence binding and persisted workflow memory."""
from alembic import op
import sqlalchemy as sa
from backend.models import LabReport

revision = "0004_report_followup"
down_revision = "0003_rounding_transcript_columns"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    columns = {c["name"] for c in sa.inspect(bind).get_columns("open_loops")}
    for name in ("matching_criteria", "workflow_memory"):
        if name not in columns:
            op.add_column("open_loops", sa.Column(name, sa.JSON(), nullable=False, server_default=sa.text("'{}'")))
    if "current_evidence_id" not in columns:
        op.add_column("open_loops", sa.Column("current_evidence_id", sa.Integer(), sa.ForeignKey("evidence_nodes.id"), nullable=True))
    if "version" not in {c["name"] for c in sa.inspect(bind).get_columns("findings")}:
        op.add_column("findings", sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
    names = {c["name"] for c in sa.inspect(bind).get_unique_constraints("findings")}
    if "uq_finding_loop_evidence" not in names:
        op.create_unique_constraint("uq_finding_loop_evidence", "findings", ["loop_id", "evidence_id"])
    LabReport.__table__.create(bind=bind, checkfirst=True)


def downgrade():
    # Preserve report versions, evidence and historical state.
    pass
