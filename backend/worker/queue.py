from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import EventJob, JobStatus


def claim_job(db: Session) -> EventJob | None:
    """Claim one pending or expired lease; callers must commit the transaction."""
    now = datetime.now(timezone.utc)
    return db.scalar(
        select(EventJob)
        .where(
            (EventJob.status == JobStatus.PENDING)
            | ((EventJob.status == JobStatus.LEASED) & (EventJob.lease_until < now))
        )
        .order_by(EventJob.id)
        .with_for_update(skip_locked=True)
    )
