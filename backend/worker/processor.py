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
    if event.event_type == "LAB_RESULT_CREATED":
        from ..services.report_service import resume_for_event
        outcomes = resume_for_event(db, event)
        if any(outcome.get("reason") == "TOOL_ERROR" for outcome in outcomes):
            raise RuntimeError("report lookup failed")
        job.status, job.lease_until, job.last_error, job.retry_at = JobStatus.SUCCEEDED, None, None, None
        return {"event_id": event.id, "status": "SUCCEEDED", "resumed": outcomes}
    intents = create_candidates(db, event) if event.event_type in {"NOTE_CREATED", "PROGRESS_NOTE_CREATED"} else []
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
    job.retry_at = None
    return {"event_id": event.id, "intents": outputs}


def claim_next(db: Session) -> EventJob | None:
    from .queue import claim_job
    return claim_job(db)


def run_job(db: Session, job_id: int, *, raise_errors=False):
    job = db.scalar(select(EventJob).where(EventJob.id == job_id).with_for_update())
    if job is None:
        raise ValueError("job not found")
    now = datetime.now(timezone.utc)
    from ..services.report_service import utc
    if job.status == JobStatus.SUCCEEDED:
        return {"status": "SUCCEEDED", "duplicate": True, "event_id": job.event_id}
    if job.status == JobStatus.FAILED or (job.retry_at and utc(job.retry_at) > now) or (job.status == JobStatus.LEASED and job.lease_until and utc(job.lease_until) > now):
        return {"status": str(job.status), "deferred": True, "event_id": job.event_id}
    attempt = job.attempt + 1
    try:
        result = process_job(db, job)
        db.commit()
        return result
    except Exception as exc:
        db.rollback()
        job = db.get(EventJob, job_id)
        job.attempt, job.lease_until = attempt, None
        job.status = JobStatus.FAILED if attempt >= 3 else JobStatus.PENDING
        job.last_error = f"{type(exc).__name__}: 事件处理失败，未确认或关闭任何任务"
        job.retry_at = datetime.now(timezone.utc) + timedelta(seconds=min(attempt * 10, 60))
        db.commit()
        if raise_errors:
            raise
        return {"event_id": job.event_id, "status": job.status, "error": job.last_error}


def run_once(db: Session) -> dict[str, object] | None:
    job = claim_next(db)
    if job is None:
        return None
    return run_job(db, job.id)
