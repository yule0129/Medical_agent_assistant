from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import ClinicalEvent, Encounter, EventJob, MedicalRecord, OpenLoop, RoundingSession

router = APIRouter(prefix="/api/encounters", tags=["timeline"])


@router.get("/{encounter_id}/timeline")
def get_timeline(encounter_id: int, db: Session = Depends(get_db)):
    encounter = db.get(Encounter, encounter_id)
    if encounter is None:
        raise HTTPException(status_code=404, detail="encounter not found")
    records = db.scalars(select(MedicalRecord).where(MedicalRecord.encounter_id == encounter_id)).all()
    sessions = db.scalars(select(RoundingSession).where(RoundingSession.encounter_id == encounter_id)).all()
    events = db.scalars(select(ClinicalEvent).where(ClinicalEvent.encounter_id == encounter_id)).all()
    loops = db.scalars(select(OpenLoop).where(OpenLoop.encounter_id == encounter_id)).all()
    items = []
    items.extend({"kind": "medical_record", "id": row.id, "occurred_at": row.created_at.isoformat(), "title": row.record_type, "status": row.status, "source_ref": row.record_number} for row in records)
    items.extend({"kind": "rounding_session", "id": row.id, "occurred_at": (row.ended_at or row.started_at).isoformat(), "title": "查房记录", "status": row.status, "source_ref": f"rounding:{row.id}"} for row in sessions)
    jobs = {job.event_id: job.status for job in db.scalars(select(EventJob).where(EventJob.event_id.in_([event.id for event in events])))}
    items.extend({"kind": "clinical_event", "id": row.id, "occurred_at": row.occurred_at.isoformat(), "title": row.event_type, "status": jobs.get(row.id, "RECORDED"), "source_ref": row.event_key} for row in events)
    items.extend({"kind": "open_loop", "id": row.id, "occurred_at": row.updated_at.isoformat(), "title": "临床跟进任务", "status": row.status, "source_ref": row.loop_key} for row in loops)
    items.sort(key=lambda item: item["occurred_at"], reverse=True)
    return {"encounter_id": encounter_id, "items": items}
