"""Create the workflow foundation without deleting prototype data."""

from alembic import op
import sqlalchemy as sa

from backend.db import Base
from backend import models  # noqa: F401

revision = "0001_workflow_foundation"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # checkfirst=True makes this additive for an existing prototype database.
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind, checkfirst=True)
    existing_columns = {column["name"] for column in sa.inspect(bind).get_columns("medical_records")}
    if "encounter_id" not in existing_columns:
        op.add_column("medical_records", sa.Column("encounter_id", sa.Integer(), sa.ForeignKey("encounters.id"), nullable=True))


def downgrade() -> None:
    # Workflow tables are intentionally not dropped automatically. Removing
    # clinical history is a destructive operation and needs an explicit DBA plan.
    pass
