"""Route relevant clinical events to subscribed follow-up goals."""
from sqlalchemy import select
from backend.models import OpenLoop
from .queue import enqueue


def enqueue_event(db, event):
    if event.event_type not in {"LAB_RESULT_CREATED", "PROGRESS_NOTE_CREATED", "DUE_RECHECK"}:
        return
    loops = db.scalars(select(OpenLoop).where(OpenLoop.patient_id == event.patient_id, OpenLoop.encounter_id == event.encounter_id,
        OpenLoop.status.notin_(["CANCELLED", "REJECTED"])))
    for loop in loops:
        memory = loop.workflow_memory or {}
        if not memory.get("agent_enabled"):
            continue
        if event.payload.get("loop_id") not in {None, loop.id}:
            continue
        if event.event_type == "LAB_RESULT_CREATED":
            from backend.domain.matching import matches_report
            from backend.models import LabReport
            report = db.scalar(select(LabReport).where(LabReport.event_id == event.id))
            if not report or not matches_report(loop, report):
                continue
        enqueue(db, loop, event, memory["agent_actor_id"], f"EVENT-{event.id}")
