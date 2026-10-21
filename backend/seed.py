from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Doctor, Encounter, MedicalRecord, Patient


def seed_demo(db: Session) -> dict[str, object]:
    """Insert deterministic synthetic data without duplicating existing rows."""
    doctors = {
        "doctor001": ("张医生", "呼吸内科"),
        "doctor002": ("李医生", "呼吸内科"),
    }
    doctor_rows: dict[str, Doctor] = {}
    for username, (name, department) in doctors.items():
        row = db.scalar(select(Doctor).where(Doctor.username == username))
        if row is None:
            row = Doctor(username=username, name=name, department=department)
            db.add(row)
            db.flush()
        doctor_rows[username] = row

    patients_data = [
        ("李秀兰", "P-DEMO-001", "08床", "女", 68, "社区获得性肺炎", "doctor001"),
        ("陈明远", "P-DEMO-002", "15床", "男", 52, "支气管哮喘", "doctor001"),
        ("王建国", "P-DEMO-003", "12床", "男", 71, "慢性阻塞性肺疾病", "doctor002"),
    ]
    patient_rows: dict[str, Patient] = {}
    for name, admission, bed, gender, age, diagnosis, username in patients_data:
        row = db.scalar(select(Patient).where(Patient.admission_number == admission))
        if row is None:
            row = Patient(name=name, admission_number=admission, bed_number=bed, gender=gender, age=age, diagnosis=diagnosis, doctor_id=doctor_rows[username].id)
            db.add(row)
            db.flush()
        patient_rows[admission] = row

    now = datetime.now(timezone.utc)
    encounters = [
        ("ENC-DEMO-001-A", "P-DEMO-001", "2026-09-18T08:00:00+00:00"),
        ("ENC-DEMO-001-B", "P-DEMO-001", "2025-11-02T08:00:00+00:00"),
        ("ENC-DEMO-002-A", "P-DEMO-002", "2026-09-19T09:00:00+00:00"),
        ("ENC-DEMO-003-A", "P-DEMO-003", "2026-09-16T10:00:00+00:00"),
    ]
    encounter_rows: dict[str, Encounter] = {}
    for number, admission, admitted_at in encounters:
        row = db.scalar(select(Encounter).where(Encounter.encounter_number == number))
        if row is None:
            patient = patient_rows[admission]
            row = Encounter(encounter_number=number, patient_id=patient.id, attending_doctor_id=patient.doctor_id, admitted_at=datetime.fromisoformat(admitted_at), diagnosis_snapshot=patient.diagnosis)
            db.add(row)
            db.flush()
        encounter_rows[number] = row

    records = [
        ("MR-DEMO-001", "P-DEMO-001", "ENC-DEMO-001-A", "待审核", "患者今日精神状态尚可，诉咳嗽较前减轻。继续观察体温及血氧变化。"),
        ("MR-DEMO-002", "P-DEMO-002", "ENC-DEMO-002-A", "待审核", "患者昨夜仍有阵发性喘息，晨起后缓解。继续吸入治疗并观察。"),
        ("MR-DEMO-003", "P-DEMO-003", "ENC-DEMO-003-A", "已签署", "患者咳嗽、咳痰较前减少，活动后气促缓解。今日血钾偏低，已予补钾。"),
    ]
    inserted_records = 0
    for number, admission, encounter_number, status, content in records:
        if db.scalar(select(MedicalRecord).where(MedicalRecord.record_number == number)):
            continue
        patient = patient_rows[admission]
        record = MedicalRecord(record_number=number, patient_id=patient.id, doctor_id=patient.doctor_id, encounter_id=encounter_rows[encounter_number].id, record_type="日常病程记录", content=content, status=status, created_at=now, signed_at=now if status == "已签署" else None)
        db.add(record)
        inserted_records += 1
    db.commit()
    return {"message": "demo data ready", "doctors": len(doctor_rows), "patients": len(patient_rows), "encounters": len(encounter_rows), "records_inserted": inserted_records}
