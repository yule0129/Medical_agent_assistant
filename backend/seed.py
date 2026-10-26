from __future__ import annotations

from datetime import datetime, timedelta, timezone

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
        ("赵文华", "P-DEMO-004", "16床", "男", 63, "社区获得性肺炎（合成演示）", "doctor001"),
        ("周晓梅", "P-DEMO-005", "17床", "女", 45, "支气管哮喘急性发作（合成演示）", "doctor001"),
        ("刘志强", "P-DEMO-006", "18床", "男", 76, "慢性阻塞性肺疾病急性加重（合成演示）", "doctor001"),
        ("吴静", "P-DEMO-007", "19床", "女", 32, "急性支气管炎（合成演示）", "doctor001"),
        ("郑国平", "P-DEMO-008", "20床", "男", 58, "肺部感染（合成演示）", "doctor001"),
    ]
    # Fixed synthetic profiles only; existing recorded values are never overwritten.
    profiles = {
        "P-DEMO-001": ("汉族", "已婚", "浙江省杭州市", "退休教师", "患者本人及女儿"),
        "P-DEMO-002": ("汉族", "已婚", "江苏省苏州市", "工程师", "患者本人"),
        "P-DEMO-003": ("汉族", "已婚", "安徽省合肥市", "退休工人", "患者本人及配偶"),
        "P-DEMO-004": ("汉族", "已婚", "山东省济南市", "退休职工", "患者本人"),
        "P-DEMO-005": ("回族", "已婚", "宁夏回族自治区银川市", "会计", "患者本人"),
        "P-DEMO-006": ("汉族", "丧偶", "四川省成都市", "退休司机", "患者本人及儿子"),
        "P-DEMO-007": ("壮族", "未婚", "广西壮族自治区南宁市", "设计师", "患者本人"),
        "P-DEMO-008": ("汉族", "已婚", "湖北省武汉市", "企业职员", "患者本人及配偶"),
    }
    patient_rows: dict[str, Patient] = {}
    for name, admission, bed, gender, age, diagnosis, username in patients_data:
        row = db.scalar(select(Patient).where(Patient.admission_number == admission))
        if row is None:
            row = Patient(name=name, admission_number=admission, bed_number=bed, gender=gender, age=age, diagnosis=diagnosis, doctor_id=doctor_rows[username].id)
            db.add(row)
            db.flush()
        patient_rows[admission] = row
        for field, value in zip(("ethnicity", "marital_status", "birthplace", "occupation"), profiles[admission][:4]):
            if getattr(row, field) in (None, "", "未记录"):
                setattr(row, field, value)
        if row.gender in (None, "", "未记录"):
            row.gender = gender
        if not row.age:
            row.age = age

    now = datetime.now(timezone.utc)
    encounters = [
        ("ENC-DEMO-001-A", "P-DEMO-001", "2026-09-18T08:00:00+00:00"),
        ("ENC-DEMO-001-B", "P-DEMO-001", "2025-11-02T08:00:00+00:00"),
        ("ENC-DEMO-002-A", "P-DEMO-002", "2026-09-19T09:00:00+00:00"),
        ("ENC-DEMO-003-A", "P-DEMO-003", "2026-09-16T10:00:00+00:00"),
        ("ENC-DEMO-004-A", "P-DEMO-004", "2026-10-22T01:30:00+00:00"),
        ("ENC-DEMO-005-A", "P-DEMO-005", "2026-10-23T02:00:00+00:00"),
        ("ENC-DEMO-006-A", "P-DEMO-006", "2026-10-21T06:00:00+00:00"),
        ("ENC-DEMO-007-A", "P-DEMO-007", "2026-10-24T03:00:00+00:00"),
        ("ENC-DEMO-008-A", "P-DEMO-008", "2026-10-25T07:00:00+00:00"),
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
        if row.recorded_at is None:
            row.recorded_at = row.admitted_at + timedelta(minutes=30)
        if not row.history_reporter or row.history_reporter == "未记录":
            row.history_reporter = profiles[admission][4]

    records = [
        ("MR-DEMO-001", "P-DEMO-001", "ENC-DEMO-001-A", "待审核", "患者今日精神状态尚可，诉咳嗽较前减轻。继续观察体温及血氧变化。"),
        ("MR-DEMO-002", "P-DEMO-002", "ENC-DEMO-002-A", "待审核", "患者昨夜仍有阵发性喘息，晨起后缓解。继续吸入治疗并观察。"),
        ("MR-DEMO-003", "P-DEMO-003", "ENC-DEMO-003-A", "已签署", "患者咳嗽、咳痰较前减少，活动后气促缓解。今日血钾偏低，已予补钾。"),
    ]
    for index in range(4, 9):
        admission = f"P-DEMO-{index:03}"
        patient = patient_rows[admission]
        records.append((f"MR-DEMO-{index:03}", admission, f"ENC-DEMO-{index:03}-A", "待审核",
                        f"【合成演示数据，不用于临床决策】入院记录：{patient.name}，{patient.gender}，{patient.age}岁。"
                        f"入院诊断：{patient.diagnosis}。病史陈述者：{profiles[admission][4]}。"
                        "该记录用于患者资料和历史病历功能演示，后续可录入查房内容。"))
    inserted_records = 0
    for number, admission, encounter_number, status, content in records:
        if db.scalar(select(MedicalRecord).where(MedicalRecord.record_number == number)):
            continue
        patient = patient_rows[admission]
        is_new_demo = admission in {f"P-DEMO-{i:03}" for i in range(4, 9)}
        created_at = encounter_rows[encounter_number].recorded_at if is_new_demo else now
        record = MedicalRecord(record_number=number, patient_id=patient.id, doctor_id=patient.doctor_id, encounter_id=encounter_rows[encounter_number].id, record_type="入院记录" if is_new_demo else "日常病程记录", content=content, status=status, created_at=created_at, signed_at=created_at if status == "已签署" else None)
        db.add(record)
        inserted_records += 1
    db.commit()
    return {"message": "demo data ready", "doctors": len(doctor_rows), "patients": len(patient_rows), "encounters": len(encounter_rows), "records_inserted": inserted_records}
