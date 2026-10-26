"""Add patient demographics and encounter-specific basic information; preserve history."""
from alembic import op
import sqlalchemy as sa

revision = "0006_patient_profile"
down_revision = "0005_agent_runtime"
branch_labels = None
depends_on = None


def upgrade():
    additions = {
        "patients": [("ethnicity", sa.String(40)), ("marital_status", sa.String(40)),
                     ("birthplace", sa.String(160)), ("occupation", sa.String(120))],
        "encounters": [("recorded_at", sa.DateTime(timezone=True)), ("history_reporter", sa.String(120))],
    }
    for table, fields in additions.items():
        existing = {col["name"] for col in sa.inspect(op.get_bind()).get_columns(table)}
        for name, kind in fields:
            if name not in existing:
                op.add_column(table, sa.Column(name, kind, nullable=True))


def downgrade():
    # Do not discard patient demographics or encounter provenance.
    pass
