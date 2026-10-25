from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from ..models import ClinicalEvent, Encounter, EventJob, LabReport
from ..domain.matching import normalized_test
from .intent_service import require_scope
from .report_service import utc


def ingest_report(db, *, event_key, doctor_id, patient_id, encounter_id, reported_at, payload, source="development_lab"):
    require_scope(db, encounter_id, doctor_id)
    encounter = db.get(Encounter, encounter_id)
    if encounter.patient_id != patient_id:
        raise ValueError("patient and encounter mismatch")
    if utc(reported_at) < utc(encounter.admitted_at):
        raise ValueError("报告时间早于本次入院时间")
    if utc(reported_at) > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise ValueError("报告时间超出允许的时钟偏差，请核对来源时间")
    existing_event = db.scalar(select(ClinicalEvent).where(ClinicalEvent.event_key == event_key))
    data = payload.model_dump()
    if existing_event:
        if (existing_event.patient_id, existing_event.encounter_id, existing_event.event_type, existing_event.source, utc(existing_event.occurred_at), existing_event.payload) != (patient_id, encounter_id, "LAB_RESULT_CREATED", source, utc(reported_at), {**data, "doctor_id": doctor_id}):
            raise ValueError("event_key 已用于不同事件")
        return existing_event, db.scalar(select(EventJob).where(EventJob.event_id == existing_event.id)), True
    reports = db.scalars(select(LabReport).where(LabReport.patient_id == patient_id, LabReport.encounter_id == encounter_id,
        LabReport.report_id == payload.report_id).order_by(LabReport.report_version.desc()).with_for_update()).all()
    for old in reports:
        if old.report_version == payload.report_version:
            event = db.get(ClinicalEvent, old.event_id)
            if (event.payload, event.source, utc(old.reported_at)) != ({**data, "doctor_id": doctor_id}, source, utc(reported_at)):
                raise ValueError("同一报告版本不能覆盖，修订请使用更高版本号")
            return event, db.scalar(select(EventJob).where(EventJob.event_id == event.id)), True
    if reports:
        latest = reports[0]
        if (normalized_test(latest.test_name), latest.specimen_id, latest.order_id) != (normalized_test(payload.test_name), payload.specimen_id, payload.order_id):
            raise ValueError("报告修订不能改变项目或标本／医嘱归属")
        if payload.report_version > latest.report_version and utc(reported_at) < utc(latest.reported_at):
            raise ValueError("修订版本的报告时间不能早于旧版本")
    event = ClinicalEvent(event_key=event_key, event_type="LAB_RESULT_CREATED", patient_id=patient_id, encounter_id=encounter_id,
        occurred_at=reported_at, received_at=datetime.now(timezone.utc), source=source, payload={**data, "doctor_id": doctor_id})
    db.add(event)
    db.flush()
    report = LabReport(event_id=event.id, patient_id=patient_id, encounter_id=encounter_id, reported_at=reported_at, **data)
    job = EventJob(event_id=event.id)
    db.add_all([report, job])
    db.flush()
    return event, job, False
