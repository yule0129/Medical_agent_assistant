from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from backend.db import Base
from backend.models import Encounter, MedicalRecord, Patient
from backend.seed import seed_demo


def test_demo_profiles_repeat_without_overwriting_existing_data():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        first = seed_demo(db)
        assert first["patients"] == 8 and first["records_inserted"] == 8
        patient = db.scalar(select(Patient).where(Patient.admission_number == "P-DEMO-001"))
        patient.occupation = "已维护的职业"
        encounter = db.scalar(select(Encounter).where(Encounter.patient_id == patient.id))
        encounter.history_reporter = "已维护的陈述者"
        record = db.scalar(select(MedicalRecord).where(MedicalRecord.patient_id == patient.id))
        record.content = "医生已修改的病历"
        db.add(Patient(name="其他患者", admission_number="OTHER", bed_number="99", diagnosis="测试"))
        db.commit()
        second = seed_demo(db)
        assert second["records_inserted"] == 0
        assert db.scalar(select(func.count()).select_from(Patient)) == 9
        assert db.scalar(select(func.count()).select_from(Encounter)) == 9
        assert patient.occupation == "已维护的职业"
        assert encounter.history_reporter == "已维护的陈述者"
        assert record.content == "医生已修改的病历"
        for row in db.scalars(select(Patient).where(Patient.admission_number.like("P-DEMO-%"))):
            assert all(getattr(row, key) for key in ("ethnicity", "marital_status", "birthplace", "occupation"))
        for row in db.scalars(select(Encounter)):
            assert row.recorded_at and row.history_reporter
        other = db.scalar(select(Patient).where(Patient.admission_number == "OTHER"))
        assert other.occupation is None
    engine.dispose()
