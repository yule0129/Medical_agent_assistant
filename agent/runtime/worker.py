"""Claim a durable lease, investigate outside locks, verify and persist outcomes."""
import uuid
from datetime import timedelta
from sqlalchemy import select
from agent.persistence.models import AgentJob, AgentProposal, AgentRun, AgentStep
from backend.models import ClinicalEvent, ClinicalIntent, Encounter, OpenLoop
from backend.services.intent_service import require_scope
from agent.llm import deepseek as providers
from agent.contracts import HumanReviewAction
from agent.core.loop import investigate
from agent.core.memory import build_context, remember_outcome
from agent.guard.evidence import validate_candidate
from agent.tools.reader import execute_read
from .clock import now


def process_agent_once(db, provider=None, *, job_id=None):
    timestamp = now()
    query = select(AgentJob).where(
        ((AgentJob.status == "PENDING") & ((AgentJob.lease_until.is_(None)) | (AgentJob.lease_until <= timestamp))) |
        ((AgentJob.status == "RUNNING") & (AgentJob.lease_until <= timestamp))
    ).order_by(AgentJob.id).with_for_update(skip_locked=True)
    if job_id is not None:
        query = query.where(AgentJob.id == job_id)
    job = db.scalar(query)
    if job is None:
        db.rollback()
        return None
    retry_base = (job.result or {}).get("retry_base_attempt", 0)
    if job.attempt - retry_base >= 3:
        job.status, job.result = "FAILED", {"stop_reason": "RETRY_EXHAUSTED"}
        db.commit()
        return {"job_id": job.id, "status": "FAILED"}
    if job.run_id:
        abandoned = db.get(AgentRun, job.run_id)
        if abandoned and abandoned.status == "RUNNING":
            abandoned.status, abandoned.stop_reason, abandoned.finished_at = "FAILED", "LEASE_EXPIRED", timestamp
    loop = db.get(OpenLoop, job.loop_id)
    event = db.get(ClinicalEvent, job.event_id)
    intent = db.get(ClinicalIntent, loop.intent_id)
    encounter = db.get(Encounter, loop.encounter_id)
    require_scope(db, loop.encounter_id, job.doctor_id)
    previous = db.scalar(select(AgentRun).where(AgentRun.intent_id == loop.intent_id).order_by(AgentRun.id.desc()))
    token = uuid.uuid4().hex
    job.attempt += 1
    job.status, job.lease_token, job.lease_until = "RUNNING", token, timestamp + timedelta(minutes=3)
    provider = provider or providers.get_provider()
    run = AgentRun(run_key=f"{job.job_key}-{job.attempt}", intent_id=loop.intent_id, loop_id=loop.id,
        prior_run_id=previous.id if previous else None, trigger_event_id=event.id,
        plan_version=(loop.workflow_memory or {}).get("agent_plan_version", 1), provider=provider.name, status="RUNNING", started_at=timestamp)
    db.add(run)
    db.flush()
    job.run_id = run.id
    run_input, extra = build_context(loop, intent, encounter, job, run, event, timestamp)
    bind, job_id, run_id = db.get_bind(), job.id, run.id
    db.commit()  # Durable lease and frozen input BEFORE the first network request.
    result, steps, observations = investigate(run_input, provider,
        lambda action, remaining: execute_read(bind, run_input, action, remaining), context_extra=extra)
    steps.insert(0, {"action": "OBSERVE", "input": run_input.model_dump(mode="json"),
        "result": {"provider": provider.name, "model": provider.model, "prompt_version": providers.PROMPT_VERSION}, "duration_ms": 0})
    db.expire_all()
    job = db.scalar(select(AgentJob).where(AgentJob.id == job_id).with_for_update())
    if job.lease_token != token or job.status != "RUNNING":
        db.rollback()
        return {"job_id": job_id, "status": "LEASE_LOST"}
    run = db.get(AgentRun, run_id)
    loop = db.scalar(select(OpenLoop).where(OpenLoop.id == run_input.scope.loop_id).with_for_update())
    try:
        require_scope(db, loop.encounter_id, run_input.scope.actor_id)
        if loop.version != run_input.scope.expected_loop_version or loop.status in {"CANCELLED", "REJECTED"}:
            raise ValueError("STALE_CONTEXT")
        if result.final_action.action == "submit_candidate":
            snapshots = validate_candidate(db, run_input, loop, result.final_action.candidate, observations, now())
            for older in db.scalars(select(AgentProposal).where(AgentProposal.loop_id == loop.id, AgentProposal.status == "PENDING_REVIEW")):
                older.status, older.version = "SUPERSEDED", older.version + 1
            db.add(AgentProposal(run_id=run.id, loop_id=loop.id, expected_loop_version=loop.version, evidence_id=loop.current_evidence_id,
                content={"candidate": result.final_action.candidate.model_dump(mode="json"), "snapshots": snapshots},
                status="PENDING_REVIEW", version=1, created_at=now()))
    except ValueError as exc:
        reason = "STALE_CONTEXT" if str(exc) == "STALE_CONTEXT" else "NEEDS_REVIEW"
        steps.append({"action": "GUARD_REJECTED", "input": {}, "result": {"reason": str(exc)}, "duration_ms": 0})
        result = result.model_copy(update={"stop_reason": reason, "final_action": HumanReviewAction(action="request_review", reason=str(exc))})
    for index, step in enumerate(steps, 1):
        db.add(AgentStep(run_id=run.id, step_number=index, action=step["action"], tool_name=step.get("tool_name"),
            input_summary=step["input"], result_summary=step["result"], duration_ms=step["duration_ms"]))
    failed = result.stop_reason in {"MODEL_ERROR", "TOOL_ERROR", "BUDGET_EXHAUSTED"}
    run.status, run.stop_reason, run.finished_at = ("FAILED" if failed else "SUSPENDED"), result.stop_reason, now()
    job.status, job.lease_until, job.result = ("FAILED" if failed else "SUCCEEDED"), None, {**result.model_dump(mode="json"), "retry_base_attempt": retry_base}
    remember_outcome(loop, run_input, result, run.id)
    db.commit()
    return {"job_id": job.id, "run_id": run.id, "status": job.status, "stop_reason": result.stop_reason}
