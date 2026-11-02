"""Differentiate bedside transcripts from the existing task workflow, preserving records."""
from alembic import op
import sqlalchemy as sa

revision = "0008_bedside_rounding"
down_revision = "0007_admission_agent"
branch_labels = None
depends_on = None


def upgrade():
    columns = {c['name'] for c in sa.inspect(op.get_bind()).get_columns('rounding_sessions')}
    if 'record_mode' not in columns:
        op.add_column('rounding_sessions', sa.Column('record_mode', sa.String(20), nullable=False, server_default='WORKFLOW'))
    if 'version' not in columns:
        op.add_column('rounding_sessions', sa.Column('version', sa.Integer(), nullable=False, server_default='1'))


def downgrade():
    pass  # Keep transcript mode and revision history.
