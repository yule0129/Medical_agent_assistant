"""Persist admission interview, generated draft, source versions and trace."""
from alembic import op
import sqlalchemy as sa

revision = "0007_admission_agent"
down_revision = "0006_patient_profile"
branch_labels = None
depends_on = None


def upgrade():
    from agent.admission.models import AdmissionWorkspace
    AdmissionWorkspace.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    # Preserve interview and review history.
    pass
