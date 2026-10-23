from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Encounter, MedicalRecord, Patient, RoundingSession

router = APIRouter(prefix="/api/medical-records", tags=["medical-records"])


class RecordCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_number: str = Field(min_length=1, max_length=60)
    doctor_id: int = Field(gt=0)
    patient_id: int = Field(gt=0)
    encounter_id: int = Field(gt=0)
    record_type: str = Field(default="日常病程记录", max_length=80)
    content: str = Field(min_length=1, max_length=100_000)
    source_rounding_session_id: int | None = Field(default=None, gt=0)


@router.post("", status_code=201)
def create_record(payload: RecordCreate, db: Session = Depends(get_db)):
    patient = db.get(Patient, payload.patient_id)
    encounter = db.get(Encounter, payload.encounter_id)
    if patient is None or encounter is None or encounter.patient_id != patient.id:
        raise HTTPException(status_code=404, detail="patient and encounter scope not found")
    if patient.doctor_id != payload.doctor_id and encounter.attending_doctor_id != payload.doctor_id:
        raise HTTPException(status_code=403, detail="doctor is outside encounter scope")
    if db.query(MedicalRecord).filter(MedicalRecord.record_number == payload.record_number).first():
        raise HTTPException(status_code=409, detail="record_number already exists")
    if payload.source_rounding_session_id:
        source = db.get(RoundingSession, payload.source_rounding_session_id)
        if not source or source.encounter_id != encounter.id:
            raise HTTPException(status_code=422, detail="rounding source is outside encounter scope")
    row = MedicalRecord(source_rounding_session_id=payload.source_rounding_session_id, record_number=payload.record_number, patient_id=patient.id, doctor_id=payload.doctor_id, encounter_id=encounter.id, record_type=payload.record_type, content=payload.content, status="待审核", created_at=datetime.now(timezone.utc))
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"id": row.id, "record_number": row.record_number, "patient_id": row.patient_id, "encounter_id": row.encounter_id, "status": row.status, "created_at": row.created_at.isoformat(), "source_rounding_session_id": payload.source_rounding_session_id}
