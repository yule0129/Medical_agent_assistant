from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import ClinicalIntent, OpenLoop
from ..services.intent_service import candidate_target

router = APIRouter(prefix="/api/encounters", tags=["workflow"])


@router.get("/{encounter_id}/intents")
def list_intents(encounter_id: int, db: Session = Depends(get_db)):
    rows = db.scalars(select(ClinicalIntent).where(ClinicalIntent.encounter_id == encounter_id).order_by(ClinicalIntent.created_at.desc())).all()
    return [{"id": row.id, "intent_key": row.intent_key, "patient_id": row.patient_id, "raw_text": row.raw_text, "target": candidate_target(row), "source_event_id": row.source_event_id, "intent_type": row.intent_type, "status": row.status, "created_at": row.created_at.isoformat()} for row in rows]


@router.get("/{encounter_id}/loops")
def list_loops(encounter_id: int, db: Session = Depends(get_db)):
    rows = db.scalars(select(OpenLoop).where(OpenLoop.encounter_id == encounter_id).order_by(OpenLoop.updated_at.desc())).all()
    return [{"id": row.id, "loop_key": row.loop_key, "patient_id": row.patient_id, "status": row.status, "waiting_for": row.waiting_for, "version": row.version, "state_reason": row.state_reason, "updated_at": row.updated_at.isoformat()} for row in rows]
