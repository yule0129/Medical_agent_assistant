"""Investigation commands, doctor feedback and trace/source queries."""
from datetime import datetime, timezone
import hashlib
from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from backend.db import get_db
from backend.models import AuditLog, ClinicalEvent, EventJob, MedicalRecord
from agent.persistence.models import AgentJob, AgentProposal, AgentRun, AgentStep
from agent.llm import deepseek as provider
from agent.runtime.queue import enqueue
from .common import authorize, task, Body, Start, start_readiness

router = APIRouter()


@router.get("/feedback")
def feedback(doctor_id: int = 1, encounter_id: int | None = None, db: Session = Depends(get_db)):
    from agent.api.feedback_service import doctor_feedback
    try:
        return doctor_feedback(db, doctor_id, encounter_id)
    except ValueError as exc:
        raise HTTPException(403, str(exc)) from exc



@router.get("/status")
def status():
    return {"provider": "deepseek", "model": provider.DeepSeekProvider().model, "configured": provider.configured(),
        "message": "已配置，可启用目标后等待 Worker 调查" if provider.configured() else "未配置：在本地 .env 设置 DEEPSEEK_API_KEY 后重启 API 和 Worker"}



@router.post("/loops/{loop_id}/runs")
def start(loop_id: int, payload: Start, db: Session = Depends(get_db)):
    loop = task(db, loop_id, payload.doctor_id, True)
    if not provider.configured():
        raise HTTPException(503, "DeepSeek 尚未配置，请在本地 .env 设置 DEEPSEEK_API_KEY 并重启服务")
    # Replays may arrive after a rule report transition; never enable a different goal silently.
    key = "AGENT-" + hashlib.sha256(f"{loop.id}|MANUAL-{payload.request_key}".encode()).hexdigest()[:48]
    existing = db.scalar(select(AgentJob).where(AgentJob.job_key == key))
    if existing:
        if existing.doctor_id != payload.doctor_id:
            raise HTTPException(409, "提交标识冲突")
        return {"job_id": existing.id, "status": existing.status}
    if loop.version != payload.expected_version:
        raise HTTPException(409, "任务已更新，请刷新")
    ready, reason = start_readiness(loop)
    if not ready:
        raise HTTPException(409, reason)
    event = db.scalar(select(ClinicalEvent).where(ClinicalEvent.patient_id == loop.patient_id, ClinicalEvent.encounter_id == loop.encounter_id).order_by(ClinicalEvent.id.desc()))
    if not event:
        raise HTTPException(409, "没有可用于启动的事件")
    loop.workflow_memory = {**(loop.workflow_memory or {}), "agent_enabled": True, "agent_actor_id": payload.doctor_id}
    try:
        job = enqueue(db, loop, event, payload.doctor_id, "MANUAL-" + payload.request_key)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    db.add(AuditLog(actor_type="DOCTOR", actor_id=str(payload.doctor_id), action="ENABLE_AGENT", resource_type="OpenLoop",
        resource_id=str(loop.id), new_state={"job_id": job.id}, created_at=datetime.now(timezone.utc)))
    db.commit()
    return {"job_id": job.id, "status": job.status}



@router.get("/loops/{loop_id}")
def dashboard(loop_id: int, doctor_id: int = 1, db: Session = Depends(get_db)):
    from agent.api.feedback_service import proposal_stale
    loop = task(db, loop_id, doctor_id)
    runs = db.scalars(select(AgentRun).where(AgentRun.loop_id == loop_id, AgentRun.provider != "rules").order_by(AgentRun.id.desc()).limit(20)).all()
    proposals = db.scalars(select(AgentProposal).where(AgentProposal.loop_id == loop_id).order_by(AgentProposal.id.desc())).all()
    jobs = db.scalars(select(AgentJob).where(AgentJob.loop_id == loop_id).order_by(AgentJob.id.desc()).limit(20)).all()
    ready, block_reason = start_readiness(loop)
    return {**status(), "start_ready": ready, "start_block_reason": block_reason, "loop_version": loop.version, "loop_status": loop.status, "memory": loop.workflow_memory,
        "jobs": [{"id": j.id, "status": j.status, "attempt": j.attempt, "result": j.result} for j in jobs],
        "proposals": [{"id": p.id, "run_id": p.run_id, "version": p.version, "status": p.status, "expected_loop_version": p.expected_loop_version,
            "stale": proposal_stale(db, loop, p), "evidence_id": p.evidence_id,
            "candidate": p.content["candidate"], "snapshots": p.content["snapshots"], "child_loop_id": p.child_loop_id, "review_note": p.review_note} for p in proposals],
        "runs": [{"id": r.id, "provider": r.provider, "status": r.status, "stop_reason": r.stop_reason, "prior_run_id": r.prior_run_id,
            "plan_version": r.plan_version, "trigger_event_id": r.trigger_event_id,
            "steps": [{"number": s.step_number, "action": s.action, "tool_name": s.tool_name, "input": s.input_summary,
                "result": s.result_summary, "duration_ms": s.duration_ms} for s in db.scalars(select(AgentStep).where(AgentStep.run_id == r.id).order_by(AgentStep.step_number))]} for r in runs]}



@router.post("/jobs/{job_id}/retry")
def retry(job_id: int, payload: Body, db: Session = Depends(get_db)):
    job = db.scalar(select(AgentJob).where(AgentJob.id == job_id).with_for_update())
    if not job:
        raise HTTPException(404, "调查不存在")
    task(db, job.loop_id, payload.doctor_id)
    if not provider.configured():
        raise HTTPException(503, "模型尚未配置")
    if job.status != "FAILED":
        raise HTTPException(409, "仅可重试失败调查")
    job.status, job.lease_until = "PENDING", None
    # Keep the request identity immutable and attempt numbers monotonic.
    job.result = {**(job.result or {}), "retry_base_attempt": job.attempt}
    db.add(AuditLog(actor_type="DOCTOR", actor_id=str(payload.doctor_id), action="RETRY_AGENT", resource_type="AgentJob", resource_id=str(job.id), new_state={}, created_at=datetime.now(timezone.utc)))
    db.commit()
    return {"job_id": job.id, "status": job.status}



class Progress(Start):
    text: str = Field(min_length=1, max_length=12000)



@router.post("/loops/{loop_id}/progress")
def progress(loop_id: int, payload: Progress, db: Session = Depends(get_db)):
    loop = task(db, loop_id, payload.doctor_id, True)
    key = "AGENT-NOTE-" + hashlib.sha256(f"{loop_id}|{payload.request_key}".encode()).hexdigest()[:40]
    existing = db.scalar(select(ClinicalEvent).where(ClinicalEvent.event_key == key))
    if existing:
        if existing.payload.get("text") != payload.text or existing.payload.get("doctor_id") != payload.doctor_id:
            raise HTTPException(409, "提交标识已用于不同病程")
        return {"event_id": existing.id, "duplicate": True}
    if loop.version != payload.expected_version:
        raise HTTPException(409, "任务已更新，请刷新")
    now = datetime.now(timezone.utc)
    note = MedicalRecord(record_number=key, patient_id=loop.patient_id, encounter_id=loop.encounter_id, record_type="病程记录",
        doctor_id=payload.doctor_id, content=payload.text, status="草稿", created_at=now)
    db.add(note)
    db.flush()
    event = ClinicalEvent(event_key=key, event_type="PROGRESS_NOTE_CREATED", patient_id=loop.patient_id, encounter_id=loop.encounter_id,
        occurred_at=now, received_at=now, source="doctor_progress", payload={"doctor_id": payload.doctor_id, "text": payload.text, "record_id": note.id, "loop_id": loop.id})
    db.add(event)
    db.flush()
    db.add(EventJob(event_id=event.id, status="PENDING", attempt=0))
    db.commit()
    return {"event_id": event.id, "record_id": note.id, "saved_as": "病程草稿，响应和后续任务仍需审核"}



@router.get("/sources/{kind}/{source_id}")
def source(kind: str, source_id: int, doctor_id: int = 1, db: Session = Depends(get_db)):
    from agent.tools.evidence import snapshot
    try:
        item = snapshot(db, kind, source_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    authorize(db, item["encounter_id"], doctor_id)
    return item
