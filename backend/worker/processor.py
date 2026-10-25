from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ClinicalEvent, EventJob, JobStatus, OpenLoop, LoopStatus
from ..services.intent_service import create_candidates
from ..services.run_service import add_step, start_run
from ..tools.repository_tools import build_default_registry
from ..tools.schemas import ToolContext, ToolStatus


def process_job(db: Session, job: EventJob) -> dict[str, object]:
    event = db.get(ClinicalEvent, job.event_id)
    if event is None:
        raise ValueError("event not found")
    job.attempt += 1
    job.status = JobStatus.LEASED
    job.lease_until = datetime.now(timezone.utc) + timedelta(minutes=5)
    intents = create_candidates(db, event)
    registry = build_default_registry()
    outputs: list[dict[str, object]] = []
    for intent in intents:
        loop = db.scalar(select(OpenLoop).where(OpenLoop.intent_id == intent.id, OpenLoop.parent_loop_id.is_(None)))
        run = start_run(db, event, loop, intent_id=intent.id)
        if run.status != "RUNNING" or run.finished_at is not None:
            outputs.append({"intent_id": intent.id, "run_id": run.id, "tool_status": "already_processed"})
            continue
        add_step(db, run, 1, "OBSERVE")
        add_step(db, run, 2, "PLAN", result={"candidate_intent_id": intent.id})
        tool = registry.call("get_patient_snapshot", db, ToolContext(actor_id=event.payload.get("doctor_id", 1), patient_id=event.patient_id, encounter_id=event.encounter_id))
        add_step(db, run, 3, "ACT", "get_patient_snapshot", {"status": tool.status.value})
        run.status = "SUSPENDED"
        run.stop_reason = "WAITING_EXTERNAL_EVENT"
        run.finished_at = datetime.now(timezone.utc)
        outputs.append({"intent_id": intent.id, "run_id": run.id, "tool_status": tool.status.value})
    job.status = JobStatus.SUCCEEDED
    job.lease_until = None
    job.last_error = None
    return {"event_id": event.id, "intents": outputs}


def claim_next(db: Session) -> EventJob | None:
    now = datetime.now(timezone.utc)
    job = db.scalar(select(EventJob).where((EventJob.status == JobStatus.PENDING) | ((EventJob.status == JobStatus.LEASED) & (EventJob.lease_until < now))).order_by(EventJob.id).with_for_update(skip_locked=True))
    return job


def run_once(db: Session) -> dict[str, object] | None:
    job = claim_next(db)
    if job is None:
        return None
    job_id, attempt = job.id, job.attempt + 1
    try:
        result = process_job(db, job)
        db.commit()
        return result
    except Exception as exc:
        db.rollback()
        job = db.get(EventJob, job_id)
        job.attempt = attempt
        job.lease_until = None
        job.status = JobStatus.FAILED if attempt >= 3 else JobStatus.PENDING
        job.last_error = str(exc)
        job.retry_at = datetime.now(timezone.utc) + timedelta(seconds=min(attempt * 10, 60))
        db.commit()
        raise
