"""Durable automatic interview/rounding extraction retries."""
from alembic import op
import sqlalchemy as sa

revision='0012_auto_matter_extraction'
down_revision='0011_matter_actions'
branch_labels=None
depends_on=None


def upgrade():
    columns={c['name'] for c in sa.inspect(op.get_bind()).get_columns('patient_matter_scans')}
    if 'attempt' not in columns:
        op.add_column('patient_matter_scans',sa.Column('attempt',sa.Integer(),nullable=False,server_default='0'))
    if 'retry_at' not in columns:
        op.add_column('patient_matter_scans',sa.Column('retry_at',sa.DateTime(timezone=True),nullable=True))


def downgrade():
    pass  # Retain extraction history and retry state.
