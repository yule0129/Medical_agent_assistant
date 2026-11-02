"""Read a rounding's disposition independently from its follow-up tasks."""
from sqlalchemy import select
from datetime import datetime, timedelta, timezone

from ..models import ClinicalEvent, ClinicalIntent, MedicalRecord, OpenLoop, Encounter, Patient, RoundingSession
from .intent_service import require_scope
from .report_service import utc


def rounding_summary(db, row):
    event_ids = [event.id for event in db.scalars(select(ClinicalEvent).where(
        ClinicalEvent.encounter_id == row.encounter_id,
        ClinicalEvent.source == "rounding_session",
    )) if event.payload.get("rounding_session_id") == row.id]
    intents = db.scalars(select(ClinicalIntent).where(ClinicalIntent.source_event_id.in_(event_ids))).all() if event_ids else []
    intent_ids = [intent.id for intent in intents]
    tasks = db.scalars(select(OpenLoop).where(OpenLoop.intent_id.in_(intent_ids))).all() if intent_ids else []
    record = db.scalar(select(MedicalRecord).where(
        MedicalRecord.encounter_id == row.encounter_id,
        (MedicalRecord.source_rounding_session_id == row.id) |
        (MedicalRecord.record_number == f"ROUND-{row.id:06d}"),
    ).order_by(MedicalRecord.id.desc()))
    return {
        "id": row.id, "encounter_id": row.encounter_id, "doctor_id": row.doctor_id,
        "record_mode": row.record_mode, "version": row.version,
        "status": row.status, "started_at": utc(row.started_at).isoformat(),
        "ended_at": utc(row.ended_at).isoformat() if row.ended_at else None,
        "record_id": record.id if record else None, "record_status": record.status if record else None,
        "candidate_count": sum(i.status == "CANDIDATE" for i in intents),
        "intent_ids": intent_ids,
        "task_count": len(tasks),
        "open_task_count": sum(t.status not in {"RESOLVED", "CANCELLED", "REJECTED"} for t in tasks),
        "completed_task_count": sum(t.status == "RESOLVED" for t in tasks),
        "cancelled_task_count": sum(t.status == "CANCELLED" for t in tasks),
    }


def rounding_tasks(db, doctor_id, now=None):
    """One first-visit obligation per admission, due after 48 elapsed hours."""
    now = now or datetime.now(timezone.utc)
    tasks = []
    for encounter in db.scalars(select(Encounter).where(Encounter.status == 'INPATIENT')):
        try:
            require_scope(db, encounter.id, doctor_id)
        except ValueError:
            continue
        admitted = utc(encounter.admitted_at)
        if admitted > now:
            continue
        due = admitted + timedelta(hours=48)
        rows = db.scalars(select(RoundingSession).where(RoundingSession.encounter_id == encounter.id)
                          .order_by(RoundingSession.started_at, RoundingSession.id)).all()
        completed = next((r for r in rows if r.status == 'COMPLETED' and r.transcript.strip()
                          and admitted <= utc(r.started_at) <= now and r.ended_at
                          and utc(r.started_at) <= utc(r.ended_at) <= now), None)
        draft = next((r for r in reversed(rows) if r.record_mode == 'TRANSCRIPT' and r.status == 'DRAFT'), None)
        patient = db.get(Patient, encounter.patient_id)
        tasks.append({'id': f'first-rounding-{encounter.id}', 'intent_type': 'FIRST_ROUNDING',
            'patient_id': patient.id, 'patient': patient.name, 'encounter_id': encounter.id,
            'waiting_for': '完成入院后首次查房', 'status': 'RESOLVED' if completed else 'WAITING_EVENT',
            'due_at': due.isoformat(), 'overdue': not completed and now >= due,
            'rounding_id': completed.id if completed else draft.id if draft else None,
            'rounding_mode': completed.record_mode if completed else 'TRANSCRIPT',
            'completed_at': utc(completed.ended_at).isoformat() if completed else None,
            'late': bool(completed and utc(completed.started_at) > due),
            'state_reason': '已完成本次住院首次查房' if completed else
                            '查房问诊已暂存，仍需确认完成' if draft else '入院后48小时内至少完成一次查房'})
    return sorted(tasks, key=lambda t: t['due_at'])
