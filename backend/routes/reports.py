from datetime import datetime, timezone
import hashlib

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from ..config import settings
from ..contracts.reports import LabImport, LabPayload, MatchingCriteria
from ..db import get_db
from ..models import (AgentRun, AgentStep, AuditLog, EventJob, EvidenceNode, Finding,
                      OpenLoop, ResultAcknowledgement, ReviewDecision)
from ..services.intent_service import require_scope
from ..services.report_ingestion import ingest_report
from ..services.report_service import backfill_loop, require_latest_evidence, utc
from ..worker.processor import run_job

router = APIRouter(prefix="/api", tags=["report-followup"])


def authorize(db, encounter_id, doctor_id):
    try:
        require_scope(db, encounter_id, doctor_id)
    except ValueError as exc:
        raise HTTPException(403, str(exc)) from exc


def get_loop(db, loop_id, doctor_id, lock=False):
    query = select(OpenLoop).where(OpenLoop.id == loop_id)
    row = db.scalar(query.with_for_update() if lock else query)
    if row is None:
        raise HTTPException(404, "task not found")
    authorize(db, row.encounter_id, doctor_id)
    return row


@router.post("/lab-reports")
def import_lab_report(payload: LabImport, db: Session = Depends(get_db)):
    if settings.environment != "development":
        raise HTTPException(403, "模拟报告接口仅供 development 环境使用")
    authorize(db, payload.encounter_id, payload.doctor_id)
    lab = LabPayload.model_validate(payload.model_dump(include=set(LabPayload.model_fields)))
    try:
        event, job, duplicate = ingest_report(db, event_key=payload.event_key, doctor_id=payload.doctor_id,
            patient_id=payload.patient_id, encounter_id=payload.encounter_id, reported_at=payload.reported_at, payload=lab)
        event_id, job_id = event.id, job.id
        # Persist the raw report before investigation; a worker error never loses it.
        db.commit()
    except IntegrityError:
        # A concurrent identical delivery may have committed while this
        # request was inserting. Re-read through the same conflict checks.
        db.rollback()
        try:
            event, job, duplicate = ingest_report(db, event_key=payload.event_key, doctor_id=payload.doctor_id,
                patient_id=payload.patient_id, encounter_id=payload.encounter_id, reported_at=payload.reported_at, payload=lab)
            event_id, job_id = event.id, job.id
            db.commit()
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    processing = run_job(db, job_id)
    return {"event_id": event_id, "job_id": job_id, "duplicate": duplicate, "saved": True, "processing": processing}


class MatchAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    doctor_id: int = Field(gt=0)
    expected_version: int = Field(ge=1)
    matching: MatchingCriteria


@router.post("/loops/{loop_id}/matching")
def bind_matching(loop_id: int, payload: MatchAction, db: Session = Depends(get_db)):
    from ..models import ClinicalIntent
    from .tasks import task_out
    loop = get_loop(db, loop_id, payload.doctor_id, lock=True)
    intent = db.get(ClinicalIntent, loop.intent_id)
    if loop.version != payload.expected_version or loop.status != "WAITING_EVENT" or loop.current_evidence_id:
        raise HTTPException(409, "任务已更新或已有结果，请刷新；新目标请建立后续任务")
    if intent.intent_type not in {"FOLLOW_RESULT", "RECHECK"}:
        raise HTTPException(409, "仅检验随访任务需要匹配报告")
    loop.matching_criteria = payload.matching.model_dump(exclude_none=True)
    loop.waiting_for, loop.version = payload.matching.test_name, loop.version + 1
    loop.updated_at = datetime.now(timezone.utc)
    db.add(AuditLog(actor_type="DOCTOR", actor_id=str(payload.doctor_id), action="BIND_REPORT_CRITERIA", resource_type="OpenLoop", resource_id=str(loop.id), new_state={"criteria": loop.matching_criteria}, created_at=loop.updated_at))
    backfill_loop(db, loop)
    db.commit()
    return task_out(loop, db)


def finding_out(row, db):
    evidence = db.get(EvidenceNode, row.evidence_id)
    ack = db.scalar(select(ResultAcknowledgement).where(ResultAcknowledgement.loop_id == row.loop_id, ResultAcknowledgement.evidence_id == row.evidence_id))
    return {"id": row.id, "loop_id": row.loop_id, "evidence_id": row.evidence_id, "claim": row.claim, "status": row.status,
        "version": row.version, "searched_sources": row.searched_sources, "evidence": evidence.content,
        "created_at": row.created_at.isoformat(), "acknowledgement_found": bool(ack),
        "coverage": "仅限本系统结构化确认记录，不判断医院全部来源或临床处置是否正确"}


@router.get("/findings")
def list_findings(doctor_id: int = 1, encounter_id: int | None = None, loop_id: int | None = None, db: Session = Depends(get_db)):
    query = select(Finding).join(OpenLoop, Finding.loop_id == OpenLoop.id).order_by(Finding.id.desc())
    if encounter_id is not None:
        authorize(db, encounter_id, doctor_id)
        query = query.where(OpenLoop.encounter_id == encounter_id)
    if loop_id is not None:
        get_loop(db, loop_id, doctor_id)
        query = query.where(Finding.loop_id == loop_id)
    result = []
    for row in db.scalars(query):
        loop = db.get(OpenLoop, row.loop_id)
        try:
            require_scope(db, loop.encounter_id, doctor_id)
        except ValueError:
            continue
        result.append(finding_out(row, db))
    return result


class FindingReview(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    doctor_id: int = Field(gt=0)
    expected_version: int = Field(ge=1)
    decision: str = Field(pattern="^(ACCEPT|REJECT)$")
    reason: str = Field(default="", max_length=1000)
    idempotency_key: str = Field(min_length=1, max_length=100)


@router.post("/findings/{finding_id}/reviews")
def review_finding(finding_id: int, payload: FindingReview, db: Session = Depends(get_db)):
    row = db.scalar(select(Finding).where(Finding.id == finding_id).with_for_update())
    if row is None:
        raise HTTPException(404, "finding not found")
    loop = get_loop(db, row.loop_id, payload.doctor_id)
    existing = db.scalar(select(ReviewDecision).where(ReviewDecision.idempotency_key == payload.idempotency_key))
    if existing:
        if (existing.finding_id, existing.doctor_id, existing.decision, existing.reason) != (row.id, payload.doctor_id, payload.decision, payload.reason):
            raise HTTPException(409, "幂等标识已用于不同审核")
        return finding_out(row, db)
    if row.version != payload.expected_version or row.status != "PENDING_REVIEW" or loop.current_evidence_id != row.evidence_id:
        raise HTTPException(409, "提醒或报告版本已更新，请刷新后审核")
    try:
        require_latest_evidence(db, db.get(EvidenceNode, row.evidence_id))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if payload.decision == "REJECT" and not payload.reason:
        raise HTTPException(422, "驳回提醒必须填写理由")
    now = datetime.now(timezone.utc)
    db.add(ReviewDecision(finding_id=row.id, doctor_id=payload.doctor_id, decision=payload.decision,
        reason=payload.reason, created_at=now, idempotency_key=payload.idempotency_key))
    row.status, row.version = ("ACCEPTED" if payload.decision == "ACCEPT" else "DISMISSED"), row.version + 1
    db.add(AuditLog(actor_type="DOCTOR", actor_id=str(payload.doctor_id), action="REVIEW_FINDING", resource_type="Finding", resource_id=str(row.id), new_state={"status": row.status, "reason": payload.reason, "evidence_id": row.evidence_id}, created_at=now))
    db.commit()
    return finding_out(row, db)


@router.get("/evidence/{evidence_id}")
def get_evidence(evidence_id: int, doctor_id: int = 1, db: Session = Depends(get_db)):
    evidence = db.get(EvidenceNode, evidence_id)
    if evidence is None:
        raise HTTPException(404, "evidence not found")
    authorize(db, evidence.encounter_id, doctor_id)
    return {"id": evidence.id, "event_id": evidence.event_id, "patient_id": evidence.patient_id, "encounter_id": evidence.encounter_id,
        "source_type": evidence.source_type, "source_ref": evidence.source_ref, "verification_status": evidence.verification_status,
        "content": evidence.content, "boundary": "核验患者、住院、结构化编号和版本，不代替临床正确性判断"}


@router.get("/loops/{loop_id}/runs")
def list_runs(loop_id: int, doctor_id: int = 1, db: Session = Depends(get_db)):
    loop = get_loop(db, loop_id, doctor_id)
    runs = db.scalars(select(AgentRun).where(AgentRun.intent_id == loop.intent_id).order_by(AgentRun.id)).all()
    return {"workflow_memory": loop.workflow_memory, "runs": [{"id": run.id, "loop_id": run.loop_id, "prior_run_id": run.prior_run_id,
        "trigger_event_id": run.trigger_event_id, "plan_version": run.plan_version, "provider": run.provider, "stop_reason": run.stop_reason,
        "steps": [{"number": step.step_number, "action": step.action, "tool_name": step.tool_name, "result": step.result_summary} for step in db.scalars(select(AgentStep).where(AgentStep.run_id == run.id).order_by(AgentStep.step_number))]} for run in runs]}


class FollowUp(MatchAction):
    note: str = Field(min_length=1, max_length=1000)
    request_key: str = Field(min_length=1, max_length=100)


@router.post("/loops/{loop_id}/follow-up")
def create_follow_up(loop_id: int, payload: FollowUp, db: Session = Depends(get_db)):
    from ..models import ClinicalIntent
    from .tasks import task_out
    parent = get_loop(db, loop_id, payload.doctor_id, lock=True)
    if db.get(ClinicalIntent, parent.intent_id).intent_type not in {"FOLLOW_RESULT", "RECHECK"}:
        raise HTTPException(409, "此接口仅建立检验随访的后续环节")
    key = "FOLLOW-" + hashlib.sha256(f"{loop_id}|{payload.request_key}".encode()).hexdigest()[:32]
    existing = db.scalar(select(OpenLoop).where(OpenLoop.loop_key == key))
    criteria = payload.matching.model_dump(exclude_none=True)
    if existing:
        if existing.matching_criteria != criteria:
            raise HTTPException(409, "提交标识已用于不同后续任务")
        return task_out(existing, db)
    if parent.version != payload.expected_version or parent.status not in {"ACKNOWLEDGED", "RESOLVED"}:
        raise HTTPException(409, "请先明确确认查看当前报告，再建立后续任务；旧版本需刷新")
    if not payload.note.strip():
        raise HTTPException(422, "请填写后续跟进说明")
    now = datetime.now(timezone.utc)
    child = OpenLoop(loop_key=key, intent_id=parent.intent_id, parent_loop_id=parent.id,
        patient_id=parent.patient_id, encounter_id=parent.encounter_id, waiting_for=criteria["test_name"], matching_criteria=criteria,
        workflow_memory={"dependencies": [parent.id], "waiting_for": criteria["test_name"]}, status="WAITING_EVENT",
        version=1, state_reason=payload.note.strip(), created_at=now, updated_at=now)
    db.add(child)
    db.flush()
    parent.version += 1
    db.add(AuditLog(actor_type="DOCTOR", actor_id=str(payload.doctor_id), action="CREATE_FOLLOW_UP", resource_type="OpenLoop", resource_id=str(parent.id), new_state={"child_loop_id": child.id, "note": payload.note.strip()}, created_at=now))
    backfill_loop(db, child)
    db.commit()
    return task_out(child, db)


@router.get("/event-jobs")
def list_jobs(doctor_id: int = 1, db: Session = Depends(get_db)):
    from ..models import ClinicalEvent
    result = []
    for job in db.scalars(select(EventJob).order_by(EventJob.id.desc()).limit(200)):
        event = db.get(ClinicalEvent, job.event_id)
        try:
            require_scope(db, event.encounter_id, doctor_id)
        except ValueError:
            continue
        result.append({"id": job.id, "event_id": event.id, "status": job.status, "attempt": job.attempt, "last_error": job.last_error, "retry_at": job.retry_at.isoformat() if job.retry_at else None})
    return result


class JobRetry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    doctor_id: int = Field(gt=0)
    expected_attempt: int = Field(ge=0)


@router.post("/event-jobs/{job_id}/retry")
def retry_job(job_id: int, payload: JobRetry, db: Session = Depends(get_db)):
    from ..models import ClinicalEvent
    if settings.environment != "development":
        raise HTTPException(403, "开发重试接口仅供 development 环境使用")
    job = db.scalar(select(EventJob).where(EventJob.id == job_id).with_for_update())
    if job is None:
        raise HTTPException(404, "job not found")
    event = db.get(ClinicalEvent, job.event_id)
    authorize(db, event.encounter_id, payload.doctor_id)
    if job.status != "FAILED" or job.attempt != payload.expected_attempt:
        raise HTTPException(409, "仅可重试当前失败的处理项，请刷新")
    db.add(AuditLog(actor_type="DOCTOR", actor_id=str(payload.doctor_id), action="RETRY_EVENT_JOB",
        resource_type="EventJob", resource_id=str(job.id), old_state={"attempt": job.attempt, "status": job.status},
        new_state={"status": "PENDING"}, created_at=datetime.now(timezone.utc)))
    job.status, job.attempt, job.retry_at, job.lease_until = "PENDING", 0, None, None
    db.commit()
    return run_job(db, job_id)
