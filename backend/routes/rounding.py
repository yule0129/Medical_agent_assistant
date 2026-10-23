from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import AuditLog, ClinicalEvent, Encounter, EventJob, JobStatus, MedicalRecord, Patient, RoundingSession
from ..services.intent_service import require_scope
from ..services.rounding_service import rounding_summary
from ..worker.processor import process_job

router = APIRouter(prefix="/api/rounding-sessions", tags=["rounding"])


def require_rounding(db, session_id, doctor_id, lock=False):
    query = select(RoundingSession).where(RoundingSession.id == session_id)
    if lock:
        query = query.with_for_update()
    row = db.scalar(query)
    if row is None:
        raise HTTPException(404, "查房记录不存在")
    try:
        require_scope(db, row.encounter_id, doctor_id)
    except ValueError as exc:
        raise HTTPException(403, str(exc)) from exc
    return row


@router.get("")
def list_roundings(encounter_id: int, doctor_id: int = 1, db: Session = Depends(get_db)):
    try:
        require_scope(db, encounter_id, doctor_id)
    except ValueError as exc:
        raise HTTPException(403, str(exc)) from exc
    rows = db.scalars(select(RoundingSession).where(RoundingSession.encounter_id == encounter_id).order_by(RoundingSession.started_at.desc(), RoundingSession.id.desc())).all()
    return [rounding_summary(db, row) for row in rows]


@router.get("/{session_id}")
def get_rounding(session_id: int, doctor_id: int = 1, db: Session = Depends(get_db)):
    row = require_rounding(db, session_id, doctor_id)
    return {**rounding_summary(db, row), "transcript": row.transcript, "generated_note": row.generated_note}


class RoundingFinish(BaseModel):
    model_config = ConfigDict(extra="forbid")
    doctor_id: int = Field(gt=0)


@router.post("/{session_id}/finish")
def finish_rounding(session_id: int, payload: RoundingFinish, db: Session = Depends(get_db)):
    row = require_rounding(db, session_id, payload.doctor_id, lock=True)
    summary = rounding_summary(db, row)
    if row.status == "COMPLETED":
        return summary
    if row.status != "SUBMITTED" or summary["record_id"] is None:
        raise HTTPException(409, "请先保存病历并提交本次查房")
    if summary["candidate_count"]:
        raise HTTPException(409, f"还有 {summary['candidate_count']} 个候选任务未处理，请确认加入安排或填写原因忽略")
    # Follow-up tasks may remain open after the bedside encounter has ended.
    row.status = "COMPLETED"
    row.ended_at = datetime.now(timezone.utc)
    db.add(AuditLog(actor_type="DOCTOR", actor_id=str(payload.doctor_id), action="FINISH_ROUNDING",
        resource_type="RoundingSession", resource_id=str(row.id),
        new_state={"status": "COMPLETED", "open_task_count": summary["open_task_count"]}, created_at=row.ended_at))
    db.commit()
    return rounding_summary(db, row)


class RoundingCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    doctor_id: int = Field(gt=0)
    patient_id: int = Field(gt=0)
    encounter_id: int = Field(gt=0)
    started_at: datetime
    ended_at: datetime | None = None
    transcript: str = Field(default="", max_length=100_000)
    generated_note: str | None = Field(default=None, max_length=100_000)
    submit_event: bool = True
    audio_filename: str | None = Field(default=None, max_length=255)
    transcript_source: str = Field(default="manual", max_length=30)
    request_key: str | None = Field(default=None, min_length=1, max_length=100)

    @field_validator("started_at", "ended_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("时间必须带时区")
        return value


@router.post("")
def create_rounding(payload: RoundingCreate, db: Session = Depends(get_db)):
    patient = db.get(Patient, payload.patient_id)
    encounter = db.get(Encounter, payload.encounter_id)
    if patient is None or encounter is None or encounter.patient_id != patient.id:
        raise HTTPException(status_code=404, detail="patient and encounter scope not found")
    if patient.doctor_id != payload.doctor_id and encounter.attending_doctor_id != payload.doctor_id:
        raise HTTPException(status_code=403, detail="doctor is outside encounter scope")
    if not payload.transcript.strip():
        raise HTTPException(status_code=422, detail="查房记录不能为空")
    if payload.ended_at and payload.ended_at < payload.started_at:
        raise HTTPException(status_code=422, detail="结束时间不能早于开始时间")
    if payload.request_key:
        previous = db.scalar(select(RoundingSession).where(RoundingSession.request_key == payload.request_key))
        if previous:
            if previous.encounter_id != encounter.id or previous.doctor_id != payload.doctor_id or previous.transcript != payload.transcript or previous.generated_note != (payload.generated_note or f"患者本次查房记录：{payload.transcript.strip()}"):
                raise HTTPException(status_code=409, detail="该提交标识已用于不同的查房内容")
            record = db.scalar(select(MedicalRecord).where(MedicalRecord.source_rounding_session_id == previous.id))
            return {"id": previous.id, "encounter_id": previous.encounter_id, "record_id": record.id if record else None, "status": previous.status, "replayed": True}
    draft = payload.generated_note or f"患者本次查房记录：{payload.transcript.strip()}"
    session = RoundingSession(request_key=payload.request_key, encounter_id=encounter.id, doctor_id=payload.doctor_id, started_at=payload.started_at, ended_at=payload.ended_at, transcript=payload.transcript, generated_note=draft, audio_filename=payload.audio_filename, transcript_source=payload.transcript_source, status="SUBMITTED" if payload.submit_event else "DRAFT")
    db.add(session)
    db.flush()
    record = MedicalRecord(record_number=f"ROUND-{session.id:06d}", source_rounding_session_id=session.id, patient_id=patient.id, doctor_id=payload.doctor_id, encounter_id=encounter.id, record_type="日常病程记录（查房草稿）", content=draft, status="待审核", created_at=datetime.now(timezone.utc))
    db.add(record)
    event_id = None
    job_id = None
    if payload.submit_event:
        event_key = f"ROUNDING-{session.id}"
        event = ClinicalEvent(event_key=event_key, event_type="NOTE_CREATED", patient_id=patient.id, encounter_id=encounter.id, occurred_at=payload.ended_at or payload.started_at, received_at=datetime.now(timezone.utc), source="rounding_session", payload={"doctor_id": payload.doctor_id, "rounding_session_id": session.id, "text": payload.transcript, "transcript_source": payload.transcript_source, "audio_filename": payload.audio_filename})
        db.add(event)
        db.flush()
        job = EventJob(event_id=event.id, status=JobStatus.PENDING, attempt=0)
        db.add(job)
        db.flush()
        event_id, job_id = event.id, job.id
        workflow = process_job(db, job)
    else:
        workflow = {"event_id": None, "intents": []}
    db.flush()
    db.commit()
    return {"id": session.id, "encounter_id": session.encounter_id, "status": session.status, "record_id": record.id, "draft": draft, "event_id": event_id, "job_id": job_id, "workflow": workflow}
