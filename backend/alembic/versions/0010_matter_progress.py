"""Durable patient-matter progress jobs and doctor-facing observations."""
from alembic import op

revision='0010_matter_progress'
down_revision='0009_patient_matters'
branch_labels=None
depends_on=None


def upgrade():
    from agent.patient_memory.progress.models import MatterProgressJob,MatterProgress
    for model in (MatterProgressJob,MatterProgress):model.__table__.create(op.get_bind(),checkfirst=True)


def downgrade():
    pass  # Preserve investigations and clinician acknowledgement history.
