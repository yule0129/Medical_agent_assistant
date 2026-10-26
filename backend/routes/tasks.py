"""Doctor-confirmed scheduling and manual task follow-through.

Manual result entry is explicitly labelled; this is not LIS synchronization.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..contracts.loops import LoopTransitionRequest
from ..domain.loop_state import transition_loop
from ..models import (AuditLog, ClinicalEvent, ClinicalIntent, EvidenceNode, HandoffReport,
                      MedicalRecord, OpenLoop, Patient, ResultAcknowledgement)
from ..services.intent_service import require_scope
from ..services.report_service import current_evidence, require_latest_evidence

router = APIRouter(prefix="/api", tags=["tasks"])


@router.get("/admission-tasks")
def list_admission_tasks(doctor_id: int = 1, db: Session = Depends(get_db)):
    from ..services.admission_service import admission_tasks
    return admission_tasks(db, doctor_id)


def task_out(row, db):
    intent = db.get(ClinicalIntent, row.intent_id)
    event = db.get(ClinicalEvent, intent.source_event_id) if intent.source_event_id else None
    source_id = event.payload.get("rounding_session_id") if event else None
    record = db.scalar(select(MedicalRecord).where(MedicalRecord.encounter_id == row.encounter_id,
        (MedicalRecord.source_rounding_session_id == source_id) | (MedicalRecord.record_number == f"ROUND-{source_id:06d}"))) if isinstance(source_id, int) else None
    patient = db.get(Patient, row.patient_id)
    return {"id": row.id, "patient": patient.name, "patient_id": row.patient_id,
            "encounter_id": row.encounter_id, "intent_type": intent.intent_type,
            "raw_text": intent.raw_text, "waiting_for": row.waiting_for, "status": row.status,
            "version": row.version, "due_at": row.due_at.isoformat() if row.due_at else None,
            "state_reason": row.state_reason, "record_id": record.id if record else None,
            "source_rounding_session_id": source_id,
            "completed_at": row.updated_at.isoformat() if row.status == "RESOLVED" else None,
            "matching_criteria": row.matching_criteria, "workflow_memory": row.workflow_memory,
            "current_evidence_id": row.current_evidence_id, "parent_loop_id": row.parent_loop_id}


@router.get("/tasks")
def list_tasks(doctor_id: int = 1, encounter_id: int | None = None, db: Session = Depends(get_db)):
    query = select(OpenLoop).order_by(OpenLoop.due_at.asc().nulls_last(), OpenLoop.id.desc())
    if encounter_id is not None:
        query = query.where(OpenLoop.encounter_id == encounter_id)
    result = []
    for row in db.scalars(query):
        try:
            require_scope(db, row.encounter_id, doctor_id)
        except ValueError:
            continue
        result.append(task_out(row, db))
    return result


class TaskAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    doctor_id: int = Field(gt=0)
    expected_version: int = Field(ge=1)
    note: str = Field(default="", max_length=10000)
    due_at: datetime | None = None
    evidence_id: int | None = Field(default=None, gt=0)

    @field_validator("due_at")
    @classmethod
    def timezone_required(cls, value):
        if value is not None and value.utcoffset() is None:
            raise ValueError("时间必须带时区")
        return value


@router.post("/loops/{loop_id}/{action}")
def update_task(loop_id: int, action: str, payload: TaskAction, db: Session = Depends(get_db)):
    if action not in {"schedule", "record-result", "acknowledge", "resolve", "cancel", "complete"}:
        raise HTTPException(404, "unknown task action")
    row = db.scalar(select(OpenLoop).where(OpenLoop.id == loop_id).with_for_update())
    if row is None:
        raise HTTPException(404, "task not found")
    try:
        require_scope(db, row.encounter_id, payload.doctor_id)
    except ValueError as exc:
        raise HTTPException(403, str(exc)) from exc
    if row.version != payload.expected_version:
        raise HTTPException(409, "任务已更新，请刷新后重试")
    now = datetime.now(timezone.utc)
    before = {"status": row.status, "version": row.version}
    note = payload.note.strip()
    intent = db.get(ClinicalIntent, row.intent_id)
    if action == "complete" and intent.intent_type not in {"ACTION", "MONITOR"}:
        raise HTTPException(409, "检验类任务请先登记结果、确认查看，再填写处理结论完成")
    if row.status in {"RESOLVED", "CANCELLED", "REJECTED"}:
        raise HTTPException(409, "任务已结束")
    if action == "schedule":
        row.due_at = payload.due_at
    else:
        target = {"record-result": "RESULT_AVAILABLE", "acknowledge": "ACKNOWLEDGED", "resolve": "RESOLVED", "complete": "RESOLVED", "cancel": "CANCELLED"}[action]
        evidence = current_evidence(db, row)
        if action == "record-result":
            if row.status != "WAITING_EVENT" or not note:
                raise HTTPException(409, "仅等待中的任务可登记结果，且必须填写结果或执行记录")
            evidence = EvidenceNode(evidence_key=f"TASK-{row.id}-RESULT", patient_id=row.patient_id,
                                    encounter_id=row.encounter_id, source_type="DOCTOR_MANUAL_ENTRY",
                                    source_ref=f"loop:{row.id}", captured_at=now,
                                    content={"note": note, "doctor_id": payload.doctor_id}, verification_status="DOCTOR_ENTERED")
            db.add(evidence)
            db.flush()
            row.current_evidence_id = evidence.id
        if action == "acknowledge" and evidence is None:
            raise HTTPException(409, "没有该任务的结果记录")
        if action in {"acknowledge", "resolve"}:
            try:
                require_latest_evidence(db, evidence)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
        if action == "acknowledge" and evidence and evidence.source_type == "STRUCTURED_LAB_REPORT" and payload.evidence_id != evidence.id:
            raise HTTPException(409, "请明确确认当前报告版本的证据 ID，旧报告不能确认新结果")
        if row.status == target:
            raise HTTPException(409, "该操作已完成，请刷新")
        try:
            transition_loop(LoopTransitionRequest(current=row.status, target=target,
                intent_type=intent.intent_type if action == "complete" else "FOLLOW_RESULT",
                has_evidence=evidence is not None, clinician_confirmed=True,
                resolution_note=note, occurred_at=now))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        if action == "complete":
            db.add(EvidenceNode(evidence_key=f"TASK-{row.id}-EXECUTION", patient_id=row.patient_id,
                encounter_id=row.encounter_id, source_type="DOCTOR_EXECUTION_RECORD", source_ref=f"loop:{row.id}",
                captured_at=now, content={"note": note, "doctor_id": payload.doctor_id}, verification_status="DOCTOR_ENTERED"))
        if action == "acknowledge":
            db.add(ResultAcknowledgement(loop_id=row.id, evidence_id=evidence.id,
                doctor_id=payload.doctor_id, note=note, created_at=now, idempotency_key=f"TASK-ACK-{row.id}-{evidence.id}"))
            from ..models import Finding
            for finding in db.scalars(select(Finding).where(Finding.loop_id == row.id, Finding.evidence_id == evidence.id, Finding.status.in_(["PENDING_REVIEW", "ACCEPTED"]))):
                finding.status, finding.version = "ACKNOWLEDGED", finding.version + 1
        row.status = target
    row.version += 1
    row.updated_at = now
    row.state_reason = note or {"schedule": "医生调整任务时间", "acknowledge": "医生已确认查看结果"}.get(action, action)
    db.add(AuditLog(actor_type="DOCTOR", actor_id=str(payload.doctor_id), action=action.upper(),
        resource_type="OpenLoop", resource_id=str(row.id), old_state=before,
        new_state={"status": row.status, "version": row.version, "note": note}, created_at=now))
    db.commit()
    return task_out(row, db)


@router.get("/loops/{loop_id}/history")
def task_history(loop_id: int, doctor_id: int = 1, db: Session = Depends(get_db)):
    row = db.get(OpenLoop, loop_id)
    if row is None:
        raise HTTPException(404, "task not found")
    try:
        require_scope(db, row.encounter_id, doctor_id)
    except ValueError as exc:
        raise HTTPException(403, str(exc)) from exc
    history = db.scalars(select(AuditLog).where(AuditLog.resource_type == "OpenLoop", AuditLog.resource_id == str(loop_id)).order_by(AuditLog.id)).all()
    evidence = current_evidence(db, row)
    if evidence is None:
        evidence = db.scalar(select(EvidenceNode).where(EvidenceNode.evidence_key == f"TASK-{loop_id}-EXECUTION"))
    from .reports import list_findings
    return {"task": task_out(row, db), "result": evidence.content if evidence else None,
            "evidence": {"id": evidence.id, "source_type": evidence.source_type, "source_ref": evidence.source_ref, "verification_status": evidence.verification_status} if evidence else None,
            "findings": list_findings(doctor_id=doctor_id, loop_id=loop_id, db=db),
            "history": [{"action": h.action, "doctor_id": h.actor_id, "at": h.created_at.isoformat(), "detail": h.new_state} for h in history]}


class HandoffCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    doctor_id: int = Field(gt=0)


@router.post("/encounters/{encounter_id}/handoffs")
def create_handoff(encounter_id: int, payload: HandoffCreate, db: Session = Depends(get_db)):
    try:
        require_scope(db, encounter_id, payload.doctor_id)
    except ValueError as exc:
        raise HTTPException(403, str(exc)) from exc
    tasks = list_tasks(payload.doctor_id, encounter_id, db)
    pending = [t for t in tasks if t["status"] not in {"RESOLVED", "REJECTED", "CANCELLED"}]
    candidates = db.scalars(select(ClinicalIntent).where(ClinicalIntent.encounter_id == encounter_id, ClinicalIntent.status == "CANDIDATE")).all()
    lines = []
    for t in pending:
        lines.append(f"任务 #{t['id']}：{t['waiting_for']} · {t['status']} · 负责人 医生 #{payload.doctor_id} · 计划时间 {t['due_at'] or '待安排'}")
        evidence = db.get(EvidenceNode, t['current_evidence_id']) if t['current_evidence_id'] else None
        if evidence:
            ack = db.scalar(select(ResultAcknowledgement.id).where(ResultAcknowledgement.loop_id == t['id'], ResultAcknowledgement.evidence_id == evidence.id))
            lines.append(f"  证据 #{evidence.id}：{evidence.source_ref} · {'医生已确认查看' if ack else '待医生确认查看'} · 报告原文摘录：{evidence.content.get('note', '')[:240]}")
        if t['parent_loop_id']:
            parent = db.get(OpenLoop, t['parent_loop_id'])
            lines.append(f"  前序任务 #{parent.id}：{parent.waiting_for} · {parent.status}；本次仍需跟进 {t['waiting_for']}")
    content = "未完成任务交班草稿\n" + "\n".join(lines)
    content += "\n" + "\n".join(f"待确认意图 #{c.id}：{c.raw_text}" for c in candidates)
    if not pending and not candidates:
        content += "\n当前没有未完成任务或待确认计划。"
    report = HandoffReport(encounter_id=encounter_id, doctor_id=payload.doctor_id, content=content,
        created_at=datetime.now(timezone.utc), references=[{"loop_id": t["id"], "version": t["version"], **({"evidence_id": t["current_evidence_id"]} if t["current_evidence_id"] else {}),
            **({"parent_loop_id": t["parent_loop_id"], "parent_version": db.get(OpenLoop, t["parent_loop_id"]).version,
                "parent_evidence_id": db.get(OpenLoop, t["parent_loop_id"]).current_evidence_id} if t["parent_loop_id"] else {})} for t in pending] + [{"intent_id": c.id} for c in candidates])
    db.add(report)
    db.commit()
    return {"id": report.id, "content": report.content, "status": report.status, "references": report.references}
