"""Clinician-approved action proposals and parent-child matter links."""
from alembic import op

revision='0011_matter_actions'
down_revision='0010_matter_progress'
branch_labels=None
depends_on=None


def upgrade():
    from agent.patient_memory.actions.models import MatterAction,MatterLink
    for model in (MatterAction,MatterLink):model.__table__.create(op.get_bind(),checkfirst=True)


def downgrade():
    pass  # Keep approved effects, receipts and lineage.
