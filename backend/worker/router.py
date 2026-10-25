from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ClinicalEvent, EventJob, OpenLoop


def matching_loops(db: Session, event: ClinicalEvent) -> list[OpenLoop]:
    """Return only loops in the same patient and encounter scope."""
    return list(
        db.scalars(
            select(OpenLoop).where(
                OpenLoop.patient_id == event.patient_id,
                OpenLoop.encounter_id == event.encounter_id,
            )
        ).all()
    )


def pending_jobs(db: Session) -> list[EventJob]:
    return list(db.scalars(select(EventJob).order_by(EventJob.id)).all())
