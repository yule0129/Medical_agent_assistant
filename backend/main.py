from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .db import Base, check_database, engine, get_db
from .models import Doctor, MedicalRecord, Patient


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


class RecordUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_record(record: MedicalRecord, db: Session) -> RecordOut:
    patient = db.get(Patient, record.patient_id)
    doctor = db.get(Doctor, record.doctor_id) if record.doctor_id else None
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
        time=created_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        status=record.status,
        author=doctor.name if doctor else "未指定",
        diagnosis=patient.diagnosis,
        content=record.content,
    )


app = FastAPI(title="Health Agent API", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup() -> None:
    if settings.auto_create_tables:
        Base.metadata.create_all(bind=engine)


@app.get("/api/health")
def health() -> dict[str, str | bool]:
    database_ok = check_database()
    return {"status": "ok" if database_ok else "degraded", "database": "postgresql", "database_ready": database_ok}


@app.get("/api/medical-records", response_model=list[RecordOut])
def list_records(status: Optional[str] = None, q: Optional[str] = None, db: Session = Depends(get_db)):
    records = db.scalars(select(MedicalRecord).order_by(MedicalRecord.created_at.desc())).all()
    result = [to_record(record, db) for record in records]
    if status:
        result = [record for record in result if record.status == status]
    if q:
        result = [record for record in result if q in f"{record.patient}{record.admission_number}{record.record_type}"]
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
