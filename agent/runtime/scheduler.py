"""Create persisted due-review events without continuously running a model."""
from sqlalchemy import select
from backend.models import ClinicalEvent, OpenLoop
from backend.services.report_service import utc
from .clock import now
from .events import enqueue_event


def schedule_due(db, timestamp=None):
    timestamp = timestamp or now()
    for loop in db.scalars(select(OpenLoop).where(OpenLoop.due_at <= timestamp, OpenLoop.status.notin_(["RESOLVED", "CANCELLED", "REJECTED"]))):
        memory = loop.workflow_memory or {}
        if not memory.get("agent_enabled"):
            continue
        # One timer event per due-date/loop-version; new clinician action or report
        # creates a new version, rather than repeated reminders every scan.
        key = f"DUE-{loop.id}-{loop.version}-{utc(loop.due_at).isoformat()}"
        event = db.scalar(select(ClinicalEvent).where(ClinicalEvent.event_key == key))
        if not event:
            event = ClinicalEvent(event_key=key, event_type="DUE_RECHECK", patient_id=loop.patient_id, encounter_id=loop.encounter_id,
                occurred_at=timestamp, received_at=timestamp, source="agent_timer", payload={"doctor_id": memory["agent_actor_id"], "loop_id": loop.id})
            db.add(event)
            db.flush()
            enqueue_event(db, event)
    db.commit()
