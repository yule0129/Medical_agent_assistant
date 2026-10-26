"""Admission writing reminders derived from the current encounter and saved records."""
import hashlib
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from ..models import Encounter, MedicalRecord, Patient
from .intent_service import require_scope
from .report_service import utc

ADMISSION_TYPES = ("入院记录", "再次或多次入院记录")


def admission_state(db, encounter, now=None):
    now = now or datetime.now(timezone.utc)
    records = db.scalars(select(MedicalRecord).where(
        MedicalRecord.patient_id == encounter.patient_id,
        MedicalRecord.encounter_id == encounter.id,
        MedicalRecord.record_type.in_(ADMISSION_TYPES),
    ).order_by(MedicalRecord.created_at.desc(), MedicalRecord.id.desc())).all()
    record = next((r for r in records if r.content.strip()), records[0] if records else None)
    due = utc(encounter.admitted_at) + timedelta(hours=24)
    written = bool(record and record.content.strip())
    reviewed = bool(written and record.status == "已签署")
    active = encounter.status == "INPATIENT" and utc(encounter.admitted_at) <= now
    revision = hashlib.sha256(str((record.id, record.content, record.status)).encode()).hexdigest() if record else "empty"
    from agent.admission.service import workspace_out
    agent = workspace_out(db, encounter, record)
    return {"patient_id": encounter.patient_id, "encounter_id": encounter.id,
            "encounter_number": encounter.encounter_number,
            "admitted_at": utc(encounter.admitted_at).isoformat(), "due_at": due.isoformat(),
            "written": written, "reviewed": reviewed, "agent": agent,
            "reminder_needed": active and not reviewed,
            "overdue": active and not reviewed and now >= due,
            "remaining_seconds": int((due - now).total_seconds()), "revision": revision,
            "record": {"id": record.id, "content": record.content, "status": record.status,
                       "record_type": record.record_type, "created_at": utc(record.created_at).isoformat(),
                       "signed_at": utc(record.signed_at).isoformat() if record.signed_at else None} if record else None}


def admission_tasks(db, doctor_id, now=None):
    tasks = []
    now = now or datetime.now(timezone.utc)
    for encounter in db.scalars(select(Encounter).where(Encounter.status == "INPATIENT")):
        try:
            require_scope(db, encounter.id, doctor_id)
        except ValueError:
            continue
        if utc(encounter.admitted_at) > now:
            continue
        state = admission_state(db, encounter, now)
        patient = db.get(Patient, encounter.patient_id)
        record = state["record"]
        tasks.append({"id": f"admission-{encounter.id}", "patient": patient.name,
                      "patient_id": patient.id, "encounter_id": encounter.id,
                      "intent_type": "ADMISSION_RECORD", "waiting_for": "审查入院记录",
                      "status": "RESOLVED" if state["reviewed"] else "WAITING_EVENT",
                      "stage": state["agent"]["status"], "draft_ready": state["written"],
                      "due_at": state["due_at"], "overdue": state["overdue"],
                      "record_id": record["id"] if record else None,
                      "record_status": record["status"] if record else None,
                      "state_reason": "医生已审查并签署" if state["reviewed"] else
                          "问诊或检查已更新，需重新生成并审查" if state["agent"]["stale"] else
                          "草稿已生成或保存，等待医生审查签署" if state["written"] else
                          "尚无草稿，请先采集问诊资料并由Agent生成，再审查签署"})
    return sorted(tasks, key=lambda row: row["due_at"])
