from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import ClinicalIntent, AuditLog
from datetime import timezone
from ..services.intent_service import confirm_intent, require_scope
from ..providers.rules import extract_intent

router = APIRouter(prefix="/api/intents", tags=["intents"])


class IntentConfirm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    doctor_id: int = Field(gt=0)
    waiting_for: str | None = Field(default=None, min_length=1, max_length=120)
    due_at: datetime | None = None

    @field_validator("due_at")
    @classmethod
    def timezone_required(cls, value):
        if value is not None and value.utcoffset() is None:
            raise ValueError("时间必须带时区")
        return value


class IntentReject(BaseModel):
    model_config = ConfigDict(extra="forbid")
    doctor_id: int = Field(gt=0)
    reason: str = Field(min_length=1, max_length=500)


@router.get("/{intent_id}")
def get_intent(intent_id: int, db: Session = Depends(get_db)):
    intent = db.get(ClinicalIntent, intent_id)
    if intent is None:
        raise HTTPException(status_code=404, detail="intent not found")
    return {"id": intent.id, "intent_key": intent.intent_key, "patient_id": intent.patient_id, "encounter_id": intent.encounter_id, "raw_text": intent.raw_text, "intent_type": intent.intent_type, "status": intent.status}


@router.post("/{intent_id}/confirm")
def confirm(intent_id: int, payload: IntentConfirm, db: Session = Depends(get_db)):
    try:
        loop = confirm_intent(db, intent_id, payload.waiting_for, payload.doctor_id, payload.due_at)
        db.commit()
        return {"intent_id": intent_id, "loop_id": loop.id, "loop_key": loop.loop_key, "status": loop.status, "waiting_for": loop.waiting_for}
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{intent_id}/reject")
def reject(intent_id: int, payload: IntentReject, db: Session = Depends(get_db)):
    intent = db.get(ClinicalIntent, intent_id)
    if intent is None:
        raise HTTPException(status_code=404, detail="intent not found")
    try:
        require_scope(db, intent.encounter_id, payload.doctor_id)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if intent.status != "CANDIDATE":
        raise HTTPException(status_code=409, detail="only candidate intent can be rejected")
    intent.status = "REJECTED"
    db.add(AuditLog(actor_type="DOCTOR", actor_id=str(payload.doctor_id), action="REJECT_INTENT", resource_type="ClinicalIntent", resource_id=str(intent.id), new_state={"reason": payload.reason}, created_at=datetime.now(timezone.utc)))
    db.commit()
    return {"intent_id": intent.id, "status": intent.status, "reason": payload.reason}
