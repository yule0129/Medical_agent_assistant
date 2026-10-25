"""Read a rounding's disposition independently from its follow-up tasks."""
from sqlalchemy import select

from ..models import ClinicalEvent, ClinicalIntent, MedicalRecord, OpenLoop


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
        "status": row.status, "started_at": row.started_at.isoformat(),
        "ended_at": row.ended_at.isoformat() if row.ended_at else None,
        "record_id": record.id if record else None, "record_status": record.status if record else None,
        "candidate_count": sum(i.status == "CANDIDATE" for i in intents),
        "intent_ids": intent_ids,
        "task_count": len(tasks),
        "open_task_count": sum(t.status not in {"RESOLVED", "CANCELLED", "REJECTED"} for t in tasks),
        "completed_task_count": sum(t.status == "RESOLVED" for t in tasks),
        "cancelled_task_count": sum(t.status == "CANCELLED" for t in tasks),
    }
