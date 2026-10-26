"""Versioned clinical handoff reads, edits and explicit confirmation."""
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from backend.db import get_db
from backend.models import AuditLog, ClinicalIntent, HandoffReport, OpenLoop
from .common import authorize, task, Body, Start

router = APIRouter()


@router.get("/encounters/{encounter_id}/handoffs")
def handoffs(encounter_id: int, doctor_id: int = 1, db: Session = Depends(get_db)):
    authorize(db, encounter_id, doctor_id)
    return [{"id": r.id, "status": r.status, "version": r.snapshot_version, "content": r.content, "references": r.references,
        "confirmed_at": r.confirmed_at.isoformat() if r.confirmed_at else None} for r in db.scalars(select(HandoffReport).where(HandoffReport.encounter_id == encounter_id).order_by(HandoffReport.id.desc()))]



class HandoffEdit(Body):
    expected_version: int = Field(ge=1)
    content: str = Field(min_length=1, max_length=20000)



@router.post("/handoffs/{handoff_id}/revisions")
def revise_handoff(handoff_id: int, payload: HandoffEdit, db: Session = Depends(get_db)):
    row = db.scalar(select(HandoffReport).where(HandoffReport.id == handoff_id).with_for_update())
    if not row:
        raise HTTPException(404, "交接不存在")
    authorize(db, row.encounter_id, payload.doctor_id)
    if row.snapshot_version != payload.expected_version:
        raise HTTPException(409, "交接版本不符")
    # Keep an immutable copy of prior content, including confirmed versions.
    revision = HandoffReport(encounter_id=row.encounter_id, doctor_id=payload.doctor_id, content=payload.content,
        snapshot_version=row.snapshot_version + 1, status="DRAFT", references=row.references, created_at=datetime.now(timezone.utc))
    db.add(revision)
    db.commit()
    return {"id": revision.id, "version": revision.snapshot_version, "status": revision.status}



@router.post("/handoffs/{handoff_id}/confirm")
def confirm_handoff(handoff_id: int, payload: Start, db: Session = Depends(get_db)):
    row = db.scalar(select(HandoffReport).where(HandoffReport.id == handoff_id).with_for_update())
    if not row:
        raise HTTPException(404, "交接不存在")
    authorize(db, row.encounter_id, payload.doctor_id)
    if row.snapshot_version != payload.expected_version:
        raise HTTPException(409, "交接版本不符")
    if row.status == "CONFIRMED":
        return {"id": row.id, "status": row.status}
    expected_ids = set()
    for ref in row.references:
        if "loop_id" in ref:
            loop = task(db, ref["loop_id"], payload.doctor_id)
            if loop.version != ref["version"] or loop.current_evidence_id != ref.get("evidence_id"):
                raise HTTPException(409, "任务或证据已更新，请重新生成交接草稿")
            expected_ids.add(loop.id)
            if ref.get("parent_loop_id"):
                parent = task(db, ref["parent_loop_id"], payload.doctor_id)
                if parent.version != ref["parent_version"] or parent.current_evidence_id != ref.get("parent_evidence_id"):
                    raise HTTPException(409, "前序任务或证据已变化，请重新生成交接")
    candidate_ids = set(db.scalars(select(ClinicalIntent.id).where(ClinicalIntent.encounter_id == row.encounter_id, ClinicalIntent.status == "CANDIDATE")))
    if candidate_ids != {ref["intent_id"] for ref in row.references if "intent_id" in ref}:
        raise HTTPException(409, "待确认计划已变化，请重新生成交接")
    current_ids = set(db.scalars(select(OpenLoop.id).where(OpenLoop.encounter_id == row.encounter_id, OpenLoop.status.notin_(["RESOLVED", "CANCELLED", "REJECTED"]))))
    if expected_ids != current_ids:
        raise HTTPException(409, "未完成任务集合已变化，请重新生成交接")
    # Preserve mandatory task identities even when the clinician edits the prose.
    if any(f"#{loop_id}" not in row.content for loop_id in current_ids):
        raise HTTPException(409, "交接内容缺少必需任务标识，请保留全部待办任务")
    row.status, row.confirmed_at = "CONFIRMED", datetime.now(timezone.utc)
    db.add(AuditLog(actor_type="DOCTOR", actor_id=str(payload.doctor_id), action="CONFIRM_HANDOFF", resource_type="HandoffReport", resource_id=str(row.id),
        new_state={"version": row.snapshot_version, "references": row.references}, created_at=row.confirmed_at))
    db.commit()
    return {"id": row.id, "status": row.status}
