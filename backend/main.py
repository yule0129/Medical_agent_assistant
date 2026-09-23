from __future__ import annotations

import os
from datetime import datetime
from typing import Generator, Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict
from sqlalchemy import DateTime, ForeignKey, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+psycopg://postgres:postgres@localhost:5432/health_agent")
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


class Base(DeclarativeBase):
    pass


class Doctor(Base):
    __tablename__ = "doctors"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(80))
    department: Mapped[str] = mapped_column(String(120))
    patients: Mapped[list[Patient]] = relationship(back_populates="doctor")


class Patient(Base):
    __tablename__ = "patients"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    admission_number: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    bed_number: Mapped[str] = mapped_column(String(20))
    gender: Mapped[str] = mapped_column(String(10), default="未记录")
    age: Mapped[int] = mapped_column(default=0)
    diagnosis: Mapped[str] = mapped_column(String(255))
    doctor_id: Mapped[Optional[int]] = mapped_column(ForeignKey("doctors.id"))
    doctor: Mapped[Optional[Doctor]] = relationship(back_populates="patients")
    records: Mapped[list[MedicalRecord]] = relationship(back_populates="patient", cascade="all, delete-orphan")


class MedicalRecord(Base):
    __tablename__ = "medical_records"
    id: Mapped[int] = mapped_column(primary_key=True)
    record_number: Mapped[str] = mapped_column(String(60), unique=True, index=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    doctor_id: Mapped[Optional[int]] = mapped_column(ForeignKey("doctors.id"))
    record_type: Mapped[str] = mapped_column(String(80), default="日常病程记录")
    content: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="待审核")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    signed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    patient: Mapped[Patient] = relationship(back_populates="records")


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
    content: str


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def to_record(record: MedicalRecord, db: Session) -> RecordOut:
    patient = db.get(Patient, record.patient_id)
    doctor = db.get(Doctor, record.doctor_id) if record.doctor_id else None
    return RecordOut(
        id=record.id,
        record_number=record.record_number,
        patient=patient.name,
        bed=patient.bed_number,
        admission_number=patient.admission_number,
        record_type=record.record_type,
        time=record.created_at.strftime("%Y-%m-%d %H:%M"),
        status=record.status,
        author=doctor.name if doctor else "未指定",
        diagnosis=patient.diagnosis,
        content=record.content,
    )


app = FastAPI(title="Health Agent API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in os.getenv("CORS_ORIGINS", "*").split(",")],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup() -> None:
    Base.metadata.create_all(bind=engine)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "database": "postgresql"}


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
    record.signed_at = datetime.utcnow()
    db.commit()
    db.refresh(record)
    return to_record(record, db)


@app.post("/api/dev/seed")
def seed_demo_data(db: Session = Depends(get_db)):
    """Development-only seed endpoint. Use only with synthetic/demo data."""
    if db.scalar(select(Doctor).where(Doctor.username == "doctor001")):
        return {"message": "demo data already exists"}
    doctor = Doctor(username="doctor001", name="张医生", department="呼吸内科")
    db.add(doctor)
    db.flush()
    patients = [
        Patient(name="李秀兰", admission_number="202609180028", bed_number="08床", gender="女", age=68, diagnosis="社区获得性肺炎", doctor_id=doctor.id),
        Patient(name="陈明远", admission_number="202609190036", bed_number="15床", gender="男", age=52, diagnosis="支气管哮喘", doctor_id=doctor.id),
        Patient(name="王建国", admission_number="202609160014", bed_number="12床", gender="男", age=71, diagnosis="慢性阻塞性肺疾病", doctor_id=doctor.id),
    ]
    db.add_all(patients)
    db.flush()
    db.add_all([
        MedicalRecord(record_number="MR-20260923-001", patient_id=patients[0].id, doctor_id=doctor.id, content="患者今日精神状态尚可，诉咳嗽较前减轻，无明显胸闷、气促。夜间体温最高37.2℃。查体：双肺呼吸音稍粗，未闻及明显干湿性啰音。今日复查血常规示白细胞计数12.4×10⁹/L，较昨日下降，C反应蛋白36mg/L，感染指标呈下降趋势。\n\n目前抗感染治疗有效，继续予头孢曲松抗感染及雾化吸入治疗，密切观察体温、呼吸道症状及血氧饱和度变化。"),
        MedicalRecord(record_number="MR-20260923-002", patient_id=patients[1].id, doctor_id=doctor.id, content="患者昨夜仍有阵发性喘息，晨起后症状缓解，无发热，无明显胸痛。查体：双肺呼吸音粗，可闻及散在哮鸣音。血氧饱和度96%。\n\n继续予吸入性糖皮质激素及支气管舒张剂治疗，观察夜间喘息频率，必要时复查肺功能。"),
        MedicalRecord(record_number="MR-20260922-006", patient_id=patients[2].id, doctor_id=doctor.id, status="已签署", signed_at=datetime.utcnow(), content="患者咳嗽、咳痰较前减少，活动后气促有所缓解，无发热。查体：双肺呼吸音低，可闻及少量湿性啰音。今日血钾3.2mmol/L。\n\n继续当前抗感染、平喘及氧疗方案，予补钾处理，复查电解质，继续观察呼吸困难变化。"),
    ])
    db.commit()
    return {"message": "demo data seeded", "patients": 3, "records": 3}
