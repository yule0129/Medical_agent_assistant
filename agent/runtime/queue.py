"""Idempotent investigation enqueueing; no model calls."""
import hashlib
from sqlalchemy import select
from agent.persistence.models import AgentJob
from backend.services.intent_service import require_scope
from .clock import now


def enqueue(db, loop, event, doctor_id, key):
    require_scope(db, loop.encounter_id, doctor_id)
    if (event.patient_id, event.encounter_id) != (loop.patient_id, loop.encounter_id):
        raise ValueError("EVENT_SCOPE_MISMATCH")
    if loop.status in {"CANCELLED", "REJECTED"} or not loop.matching_criteria:
        raise ValueError("ACTIVE_REPORT_TARGET_REQUIRED")
    job_key = "AGENT-" + hashlib.sha256(f"{loop.id}|{key}".encode()).hexdigest()[:48]
    existing = db.scalar(select(AgentJob).where(AgentJob.job_key == job_key))
    if existing:
        if (existing.event_id, existing.doctor_id) != (event.id, doctor_id):
            raise ValueError("REQUEST_KEY_CONFLICT")
        return existing
    job = AgentJob(job_key=job_key, loop_id=loop.id, event_id=event.id, doctor_id=doctor_id, status="PENDING", attempt=0, result={}, created_at=now())
    db.add(job)
    db.flush()
    return job
