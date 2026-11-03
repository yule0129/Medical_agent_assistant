"""Direct transcript capture; completion queues plan extraction, never a summary."""
from datetime import datetime, timezone
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..db import get_db
from ..models import AuditLog, Encounter, RoundingSession
from ..services.rounding_service import rounding_summary, rounding_tasks
from ..services.report_service import utc
from .patients import scoped_admission
from .rounding import require_rounding

router = APIRouter(prefix='/api', tags=['bedside-rounding'])


@router.get('/rounding-tasks')
def list_rounding_tasks(doctor_id: int = 1, db: Session = Depends(get_db)):
    return rounding_tasks(db, doctor_id)


class BedsideWrite(BaseModel):
    model_config = ConfigDict(extra='forbid')
    doctor_id: int = Field(gt=0)
    patient_id: int = Field(gt=0)
    encounter_id: int = Field(gt=0)
    transcript: str = Field(min_length=1, max_length=100000)
    started_at: datetime
    transcript_source: Literal['manual', 'browser_speech'] = 'manual'
    request_key: str = Field(min_length=1, max_length=100)
    expected_version: int | None = Field(default=None, ge=1)

    @field_validator('transcript')
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError('请填写查房问诊记录')
        return value.strip()

    @field_validator('started_at')
    @classmethod
    def timezone_required(cls, value):
        if value.utcoffset() is None:
            raise ValueError('查房时间必须带时区')
        return value


def result(db, row):
    return {**rounding_summary(db, row), 'transcript': row.transcript,
            'transcript_source': row.transcript_source, 'request_key': row.request_key}


def audit(db, row, doctor_id, action, before=None):
    db.add(AuditLog(actor_type='DOCTOR', actor_id=str(doctor_id), action=action,
        resource_type='RoundingSession', resource_id=str(row.id), old_state=before or {},
        new_state={'status': row.status, 'version': row.version, 'transcript': row.transcript},
        created_at=datetime.now(timezone.utc)))


def save(db, payload, session_id=None):
    encounter = scoped_admission(db, payload.patient_id, payload.encounter_id, payload.doctor_id, lock=True)
    now = datetime.now(timezone.utc)
    if encounter.status != 'INPATIENT':
        raise HTTPException(409, '当前住院已结束，不能新增或修改查房')
    if not utc(encounter.admitted_at) <= payload.started_at <= now:
        raise HTTPException(422, '查房时间必须在本次入院后且不能晚于当前时间')
    row = require_rounding(db, session_id, payload.doctor_id, lock=True) if session_id else db.scalar(
        select(RoundingSession).where(RoundingSession.request_key == payload.request_key))
    if row:
        if row.record_mode != 'TRANSCRIPT' or row.encounter_id != encounter.id or row.doctor_id != payload.doctor_id or row.request_key != payload.request_key:
            raise HTTPException(409, '提交标识或查房记录不属于当前操作范围')
        same = row.transcript == payload.transcript and utc(row.started_at) == payload.started_at and row.transcript_source == payload.transcript_source
        if not session_id:
            if not same:
                raise HTTPException(409, '该提交标识已用于其他查房内容')
            return result(db, row)
        if row.status == 'COMPLETED':
            raise HTTPException(409, '本次查房已完成，记录只读；请新增一次查房')
        if row.version != payload.expected_version:
            raise HTTPException(409, '查房记录已更新，请保留文字并刷新后核对')
        before = {'transcript': row.transcript, 'version': row.version}
        row.transcript, row.started_at, row.transcript_source = payload.transcript, payload.started_at, payload.transcript_source
        row.version += 1
    else:
        before = {}
        row = RoundingSession(encounter_id=encounter.id, doctor_id=payload.doctor_id,
            request_key=payload.request_key, started_at=payload.started_at, transcript=payload.transcript,
            transcript_source=payload.transcript_source, generated_note=None, record_mode='TRANSCRIPT', status='DRAFT')
        db.add(row)
    db.flush()
    audit(db, row, payload.doctor_id, 'SAVE_BEDSIDE_TRANSCRIPT', before)
    db.commit()
    return result(db, row)


@router.post('/bedside-roundings')
def create_bedside(payload: BedsideWrite, db: Session = Depends(get_db)):
    return save(db, payload)


@router.put('/bedside-roundings/{session_id}')
def update_bedside(session_id: int, payload: BedsideWrite, db: Session = Depends(get_db)):
    return save(db, payload, session_id)


class BedsideFinish(BaseModel):
    model_config = ConfigDict(extra='forbid')
    doctor_id: int = Field(gt=0)
    expected_version: int = Field(ge=1)


@router.post('/bedside-roundings/{session_id}/complete')
def complete_bedside(session_id: int, payload: BedsideFinish, db: Session = Depends(get_db)):
    row = require_rounding(db, session_id, payload.doctor_id, lock=True)
    if row.record_mode != 'TRANSCRIPT':
        raise HTTPException(409, '请从原查房工作流完成该记录')
    if row.status == 'COMPLETED':
        if payload.expected_version not in (row.version, row.version-1):
            raise HTTPException(409, '查房版本已更新')
        return result(db, row)
    encounter = db.get(Encounter, row.encounter_id)
    if encounter.status != 'INPATIENT' or not utc(encounter.admitted_at) <= utc(row.started_at) <= datetime.now(timezone.utc):
        raise HTTPException(409, '住院状态或查房时间已变化，请刷新后核对')
    if row.version != payload.expected_version or not row.transcript.strip():
        raise HTTPException(409, '请先保存当前查房文字并核对版本')
    row.status, row.ended_at, row.version = 'COMPLETED', datetime.now(timezone.utc), row.version+1
    audit(db, row, payload.doctor_id, 'COMPLETE_BEDSIDE_ROUNDING')
    from agent.patient_memory.automatic import enqueue
    enqueue(db,encounter,payload.doctor_id,'ROUNDING',row.id)
    db.commit()
    return result(db, row)
