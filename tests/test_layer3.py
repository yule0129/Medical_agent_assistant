from datetime import datetime, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from backend.db import Base
from backend.models import ClinicalEvent, Doctor, Encounter, EventJob, Patient, AgentRun
from backend.providers.rules import extract_intent
from backend.tools.repository_tools import build_default_registry
from backend.tools.schemas import ToolContext, ToolStatus
from backend.worker.processor import run_once


def make_db():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def seed_scope(db: Session):
    doctor = Doctor(username="layer3", name="测试医生", department="呼吸内科")
    db.add(doctor)
    db.flush()
    patient = Patient(name="合成患者", admission_number="L3-P-001", bed_number="01床", gender="男", age=50, diagnosis="肺炎", doctor_id=doctor.id)
    db.add(patient)
    db.flush()
    encounter = Encounter(encounter_number="L3-E-001", patient_id=patient.id, attending_doctor_id=doctor.id, admitted_at=datetime.now(timezone.utc), diagnosis_snapshot=patient.diagnosis)
    db.add(encounter)
    db.flush()
    return doctor, patient, encounter


def test_rules_extract_follow_result_and_ignore_completed_statement():
    result = extract_intent("血培养结果出来后通知我")
    assert result and result[0].intent_type == "FOLLOW_RESULT"
    assert extract_intent("昨日已经查看血培养结果") == []


def test_read_tool_rejects_cross_patient_scope():
    db = make_db()
    doctor, patient, encounter = seed_scope(db)
    registry = build_default_registry()
    result = registry.call("get_patient_snapshot", db, ToolContext(actor_id=doctor.id, patient_id=patient.id + 999, encounter_id=encounter.id))
    assert result.status == ToolStatus.ERROR
    assert result.error_code == "SCOPE_NOT_FOUND"


def test_event_worker_is_idempotent_for_same_event():
    db = make_db()
    doctor, patient, encounter = seed_scope(db)
    event = ClinicalEvent(event_key="L3-EVENT-001", event_type="NOTE_CREATED", patient_id=patient.id, encounter_id=encounter.id, occurred_at=datetime.now(timezone.utc), received_at=datetime.now(timezone.utc), source="test", payload={"doctor_id": doctor.id, "text": "血培养结果出来后通知我"})
    db.add(event)
    db.flush()
    db.add(EventJob(event_id=event.id))
    db.commit()
    first = run_once(db)
    second = run_once(db)
    assert first and first["event_id"] == event.id
    assert second is None
    assert db.scalar(select(AgentRun).where(AgentRun.trigger_event_id == event.id)) is not None
