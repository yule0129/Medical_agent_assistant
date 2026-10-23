from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..config import settings
from ..models import ClinicalEvent, Encounter, EventJob, JobStatus, Patient
from ..contracts.events import EventType
from ..worker.processor import run_once

router = APIRouter(prefix="/api/events", tags=["events"])


class EventCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_key: str = Field(min_length=1, max_length=100)
    event_type: EventType
    patient_id: int = Field(gt=0)
    encounter_id: int = Field(gt=0)
    doctor_id: int = Field(gt=0)
    occurred_at: datetime
    source: str = Field(min_length=1, max_length=80)
    payload: dict = Field(default_factory=dict)

    @field_validator("occurred_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at 必须带时区")
        return value


@router.post("")
def create_event(payload: EventCreate, db: Session = Depends(get_db)):
    if settings.environment != "development":
        raise HTTPException(status_code=403, detail="模拟事件接口仅在 development 环境可用")
    patient = db.get(Patient, payload.patient_id)
    encounter = db.get(Encounter, payload.encounter_id)
    if patient is None or encounter is None or encounter.patient_id != patient.id:
        raise HTTPException(status_code=404, detail="patient and encounter scope not found")
    if patient.doctor_id != payload.doctor_id and encounter.attending_doctor_id != payload.doctor_id:
        raise HTTPException(status_code=403, detail="doctor is outside encounter scope")
    existing = db.scalar(select(ClinicalEvent).where(ClinicalEvent.event_key == payload.event_key))
    if existing:
        same = existing.patient_id == payload.patient_id and existing.encounter_id == payload.encounter_id and existing.payload == {**payload.payload, "doctor_id": payload.doctor_id}
        if not same:
            raise HTTPException(status_code=409, detail="event_key already exists with different content")
        job = db.scalar(select(EventJob).where(EventJob.event_id == existing.id))
        return {"event_id": existing.id, "job_id": job.id if job else None, "duplicate": True}
    event_payload = dict(payload.payload)
    event_payload["doctor_id"] = payload.doctor_id
    event = ClinicalEvent(event_key=payload.event_key, event_type=payload.event_type.value, patient_id=payload.patient_id, encounter_id=payload.encounter_id, occurred_at=payload.occurred_at, received_at=datetime.now(timezone.utc), source=payload.source, payload=event_payload)
    db.add(event)
    db.flush()
    job = EventJob(event_id=event.id, status=JobStatus.PENDING, attempt=0)
    db.add(job)
    db.commit()
    return {"event_id": event.id, "job_id": job.id, "status": job.status}


@router.post("/dev/worker/run-once")
def run_worker_once(db: Session = Depends(get_db)):
    if settings.environment != "development":
        raise HTTPException(status_code=403, detail="开发 Worker 接口仅在 development 环境可用")
    return run_once(db) or {"status": "idle"}
