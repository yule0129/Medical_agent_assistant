"""Extraction quality audit and physician clarification, retaining existing records."""
from alembic import op
import sqlalchemy as sa

revision='0013_extraction_quality'
down_revision='0012_auto_matter_extraction'
branch_labels=None
depends_on=None


def upgrade():
    for table,columns in {
        'patient_matter_scans':[sa.Column('policy_version',sa.String(40),nullable=False,server_default='legacy'),
                                sa.Column('quality_report',sa.JSON(),nullable=False,server_default='{}')],
        'patient_matters':[sa.Column('quality',sa.JSON(),nullable=False,server_default='{}')],
    }.items():
        existing={c['name'] for c in sa.inspect(op.get_bind()).get_columns(table)}
        for column in columns:
            if column.name not in existing:op.add_column(table,column)


def downgrade():
    pass  # Keep evidence, quality decisions and clinician clarifications.
