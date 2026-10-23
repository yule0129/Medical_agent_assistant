from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from .config import settings
from .db import Base, check_database, engine, get_db
from .models import Doctor, Encounter, MedicalRecord, Patient, RoundingSession
from .routes.events import router as events_router
from .routes.intents import router as intents_router
from .routes.workflow import router as workflow_router
from .routes.rounding import router as rounding_router
from .routes.records import router as records_router
from .routes.timeline import router as timeline_router
from .routes.tasks import router as tasks_router
from .services.intent_service import require_scope
from .services.rounding_service import rounding_summary


class RecordOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    record_number: str
    patient: str
    bed: str
    admission_number: str
    record_type: str
    time: str
    status: str
    author: str
    diagnosis: str
    content: str
    encounter_id: int | None = None
    source_rounding_session_id: int | None = None
    patient_id: int
    encounter_number: str | None = None
    created_at: str
    signed_at: str | None = None


class RecordUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_record(record: MedicalRecord, db: Session) -> RecordOut:
    patient = db.get(Patient, record.patient_id)
    doctor = db.get(Doctor, record.doctor_id) if record.doctor_id else None
    encounter = db.get(Encounter, record.encounter_id) if record.encounter_id else None
    source_id = record.source_rounding_session_id
    if source_id is None and record.record_number.startswith("ROUND-"):
        suffix = record.record_number.removeprefix("ROUND-")
        source = db.get(RoundingSession, int(suffix)) if suffix.isdigit() else None
        if source and source.encounter_id == record.encounter_id:
            source_id = source.id
    created_at = record.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return RecordOut(
        id=record.id,
        record_number=record.record_number,
        patient=patient.name,
        bed=patient.bed_number,
        admission_number=patient.admission_number,
        record_type=record.record_type,
        time=created_at.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M"),
        status=record.status,
        author=doctor.name if doctor else "未指定",
        diagnosis=encounter.diagnosis_snapshot if encounter and encounter.diagnosis_snapshot else patient.diagnosis,
        content=record.content,
        encounter_id=record.encounter_id,
        source_rounding_session_id=source_id,
        patient_id=record.patient_id,
        encounter_number=encounter.encounter_number if encounter else None,
        created_at=created_at.isoformat(),
        signed_at=record.signed_at.isoformat() if record.signed_at else None,
    )


app = FastAPI(title="Health Agent API", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(events_router)
app.include_router(intents_router)
app.include_router(workflow_router)
app.include_router(rounding_router)
app.include_router(records_router)
app.include_router(timeline_router)
app.include_router(tasks_router)


@app.on_event("startup")
def startup() -> None:
    if settings.auto_create_tables:
        Base.metadata.create_all(bind=engine)


@app.get("/api/health")
def health() -> dict[str, str | bool]:
    database_ok = check_database()
    return {"status": "ok" if database_ok else "degraded", "database": "postgresql", "database_ready": database_ok}


@app.get("/api/medical-records", response_model=list[RecordOut])
def list_records(status: Optional[str] = None, q: Optional[str] = None, patient_id: int | None = None, encounter_id: int | None = None, db: Session = Depends(get_db)):
    query = select(MedicalRecord).order_by(MedicalRecord.created_at.desc(), MedicalRecord.id.desc())
    if patient_id is not None:
        query = query.where(MedicalRecord.patient_id == patient_id)
    if encounter_id is not None:
        query = query.where(MedicalRecord.encounter_id == encounter_id)
    records = db.scalars(query).all()
    result = [to_record(record, db) for record in records]
    if status:
        result = [record for record in result if record.status == status]
    if q:
        result = [record for record in result if q in f"{record.patient}{record.admission_number}{record.record_type}"]
    return result


@app.get("/api/patients/{patient_id}/history")
def patient_history(patient_id: int, encounter_id: int | None = None, doctor_id: int = 1, db: Session = Depends(get_db)):
    patient = db.get(Patient, patient_id)
    if patient is None:
        raise HTTPException(404, "患者不存在")
    encounters = db.scalars(select(Encounter).where(Encounter.patient_id == patient_id).order_by(Encounter.admitted_at.desc(), Encounter.id.desc())).all()
    accessible = []
    for encounter in encounters:
        try:
            require_scope(db, encounter.id, doctor_id)
            accessible.append(encounter)
        except ValueError:
            continue
    if patient.doctor_id != doctor_id and not accessible:
        raise HTTPException(403, "doctor is outside patient scope")
    accessible_ids = {row.id for row in accessible}
    if encounter_id is not None and encounter_id not in accessible_ids:
        raise HTTPException(404, "该患者的可访问住院记录不存在")
    record_query = select(MedicalRecord).where(MedicalRecord.patient_id == patient_id).order_by(MedicalRecord.created_at.desc(), MedicalRecord.id.desc())
    if encounter_id is not None:
        record_query = record_query.where(MedicalRecord.encounter_id == encounter_id)
    records = [row for row in db.scalars(record_query) if row.encounter_id in accessible_ids or (row.encounter_id is None and patient.doctor_id == doctor_id)]
    selected_ids = [encounter_id] if encounter_id is not None else list(accessible_ids)
    roundings = db.scalars(select(RoundingSession).where(RoundingSession.encounter_id.in_(selected_ids)).order_by(RoundingSession.started_at.desc(), RoundingSession.id.desc())).all() if selected_ids else []
    numbers = {row.id: row.encounter_number for row in accessible}
    return {
        "patient": {"id": patient.id, "name": patient.name, "bed": patient.bed_number, "diagnosis": patient.diagnosis},
        "encounters": [{"id": row.id, "encounter_number": row.encounter_number, "admitted_at": row.admitted_at.isoformat(), "status": row.status} for row in accessible],
        "records": [{**to_record(row, db).model_dump(exclude={"content"}), "preview": row.content[:160]} for row in records],
        "roundings": [{**rounding_summary(db, row), "encounter_number": numbers[row.encounter_id]} for row in roundings],
    }


@app.get("/api/patients")
def list_patients(doctor_id: int = 1, db: Session = Depends(get_db)):
    patients = db.scalars(select(Patient).order_by(Patient.id)).all()
    result = []
    for patient in patients:
        encounter = db.scalar(select(Encounter).where(Encounter.patient_id == patient.id, Encounter.status == "INPATIENT").order_by(Encounter.admitted_at.desc()))
        if patient.doctor_id != doctor_id and (encounter is None or encounter.attending_doctor_id != doctor_id):
            continue
        latest = db.scalar(select(RoundingSession).where(RoundingSession.encounter_id == encounter.id).order_by(RoundingSession.started_at.desc(), RoundingSession.id.desc())) if encounter else None
        local_tz = timezone(timedelta(hours=8))
        started = latest.started_at.replace(tzinfo=timezone.utc) if latest and latest.started_at.tzinfo is None else latest.started_at if latest else None
        today = bool(started and started.astimezone(local_tz).date() == utc_now().astimezone(local_tz).date())
        rounding_status = ("已完成" if latest.status == "COMPLETED" else "待结束") if today else "待查房"
        day_start = utc_now().astimezone(local_tz).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        today_query = select(func.count()).select_from(RoundingSession).where(RoundingSession.encounter_id == encounter.id, RoundingSession.started_at >= day_start, RoundingSession.started_at < day_start + timedelta(days=1)) if encounter else None
        today_count = db.scalar(today_query) if today_query is not None else 0
        completed_count = db.scalar(today_query.where(RoundingSession.status == "COMPLETED")) if today_query is not None else 0
        result.append({"id": patient.id, "name": patient.name, "admission_number": patient.admission_number, "bed": patient.bed_number, "diagnosis": patient.diagnosis, "gender": patient.gender, "age": patient.age, "encounter_id": encounter.id if encounter else None, "encounter_number": encounter.encounter_number if encounter else None, "rounding_status": rounding_status, "latest_rounding_id": latest.id if latest else None, "today_rounding_count": today_count, "today_completed_rounding_count": completed_count})
    return result


@app.get("/api/medical-records/{record_id}", response_model=RecordOut)
def get_record(record_id: int, db: Session = Depends(get_db)):
    record = db.get(MedicalRecord, record_id)
    if not record:
        raise HTTPException(status_code=404, detail="medical record not found")
    return to_record(record, db)


@app.patch("/api/medical-records/{record_id}", response_model=RecordOut)
def update_record(record_id: int, payload: RecordUpdate, db: Session = Depends(get_db)):
    record = db.get(MedicalRecord, record_id)
    if not record:
        raise HTTPException(status_code=404, detail="medical record not found")
    if record.status == "已签署":
        raise HTTPException(status_code=409, detail="signed medical record cannot be edited")
    record.content = payload.content
    db.commit()
    db.refresh(record)
    return to_record(record, db)


@app.post("/api/medical-records/{record_id}/sign", response_model=RecordOut)
def sign_record(record_id: int, db: Session = Depends(get_db)):
    record = db.get(MedicalRecord, record_id)
    if not record:
        raise HTTPException(status_code=404, detail="medical record not found")
    record.status = "已签署"
    record.signed_at = utc_now()
    db.commit()
    db.refresh(record)
    return to_record(record, db)


@app.post("/api/dev/seed")
def seed_demo_data(db: Session = Depends(get_db)):
    from .seed import seed_demo
    return seed_demo(db)
