"""Independent agent jobs and clinician-reviewed proposals."""
from alembic import op
from backend.models import AgentJob, AgentProposal

revision = "0005_agent_runtime"
down_revision = "0004_report_followup"
branch_labels = None
depends_on = None


def upgrade():
    for table in (AgentJob.__table__, AgentProposal.__table__):
        table.create(bind=op.get_bind(), checkfirst=True)


def downgrade():
    # Retain investigation history and clinical review evidence.
    pass
