"""Source-grounded patient matters and idempotent model scans."""
from alembic import op

revision = '0009_patient_matters'
down_revision = '0008_bedside_rounding'
branch_labels = None
depends_on = None


def upgrade():
    from agent.patient_memory.models import MatterScan, PatientMatter
    for model in (MatterScan, PatientMatter):
        model.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    pass  # Keep clinician-confirmed matters and evidence.
