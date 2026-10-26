"""Clinician approval of proposals and dependent follow-up plans."""
from datetime import datetime, timezone
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from backend.db import get_db
from backend.models import AuditLog, MedicalRecord, OpenLoop, ResultAcknowledgement
from backend.services.report_service import backfill_loop
from agent.persistence.models import AgentProposal
from agent.guard.evidence import recheck_snapshots
from .common import task, Body

router = APIRouter()


class Review(Body):
    expected_version: int = Field(ge=1)
    expected_loop_version: int = Field(ge=1)
    decision: Literal["ACCEPT", "REJECT"]
    effect: Literal["REVIEW_ONLY", "CONFIRM_RESPONSE", "CREATE_FOLLOW_UP"] = "REVIEW_ONLY"
    evidence_id: int | None = None
    note: str = Field(default="", max_length=2000)



@router.post("/proposals/{proposal_id}/review")
def review(proposal_id: int, payload: Review, db: Session = Depends(get_db)):
    proposal = db.scalar(select(AgentProposal).where(AgentProposal.id == proposal_id).with_for_update())
    if not proposal:
        raise HTTPException(404, "候选不存在")
    loop = task(db, proposal.loop_id, payload.doctor_id, True)
    if proposal.status != "PENDING_REVIEW" or proposal.version != payload.expected_version:
        raise HTTPException(409, "候选已审核，请刷新")
    if loop.version != payload.expected_loop_version or loop.version != proposal.expected_loop_version or loop.current_evidence_id != proposal.evidence_id:
        raise HTTPException(409, "任务或报告版本已变化，请重新调查")
    if payload.decision == "REJECT" and (not payload.note or payload.effect != "REVIEW_ONLY"):
        raise HTTPException(422, "驳回需填写理由，且不能同时提交业务操作")
    item = proposal.content["candidate"]
    now = datetime.now(timezone.utc)
    if payload.decision == "ACCEPT":
        try:
            recheck_snapshots(db, loop, proposal.content)
            if item["kind"] == "RESPONSE_NOT_FOUND":
                through = max(s["window"]["to_time"] for s in item["searched_sources"])
                if db.scalar(select(MedicalRecord.id).where(MedicalRecord.patient_id == loop.patient_id, MedicalRecord.encounter_id == loop.encounter_id,
                    MedicalRecord.created_at > datetime.fromisoformat(through))):
                    raise ValueError("病程已有更新，请重新调查")
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        if payload.effect != "REVIEW_ONLY":
            if payload.evidence_id != loop.current_evidence_id:
                raise HTTPException(409, "请明确核对当前报告证据版本")
            if payload.effect == "CONFIRM_RESPONSE" and item["kind"] != "RESPONSE_FOUND":
                raise HTTPException(422, "仅明确响应候选可确认响应")
            if payload.effect == "CREATE_FOLLOW_UP" and item["kind"] != "FOLLOW_UP":
                raise HTTPException(422, "仅后续计划候选可批准依赖任务")
            if loop.status not in {"RESULT_AVAILABLE", "ACKNOWLEDGED", "RESOLVED"}:
                raise HTTPException(409, "任务没有可确认的当前结果")
            if loop.status == "RESULT_AVAILABLE":
                db.add(ResultAcknowledgement(loop_id=loop.id, evidence_id=loop.current_evidence_id, doctor_id=payload.doctor_id,
                    note=payload.note or item["summary"], created_at=now, idempotency_key=f"AGENT-ACK-{loop.id}-{loop.current_evidence_id}"))
                loop.status = "ACKNOWLEDGED"
            if payload.effect == "CREATE_FOLLOW_UP":
                matching = item["follow_up"]
                next_plan = (loop.workflow_memory or {}).get("agent_plan_version", 1) + 1
                child = OpenLoop(loop_key=f"AGENT-FOLLOW-{proposal.id}", intent_id=loop.intent_id, parent_loop_id=loop.id,
                    patient_id=loop.patient_id, encounter_id=loop.encounter_id, waiting_for=matching["test_name"], matching_criteria={k:v for k,v in matching.items() if v is not None},
                    status="WAITING_EVENT", version=1, created_at=now, updated_at=now, state_reason=item["summary"],
                    workflow_memory={"agent_enabled": True, "agent_actor_id": payload.doctor_id, "agent_plan_version": next_plan, "dependencies": [loop.id]})
                db.add(child)
                db.flush()
                proposal.child_loop_id = child.id
                loop.workflow_memory = {**(loop.workflow_memory or {}), "agent_plan_version": next_plan, "agent_child_loop_id": child.id}
                backfill_loop(db, child)
            loop.version += 1
            # Do not rewrite an already-resolved parent's clinical completion timestamp.
            if loop.status != "RESOLVED":
                loop.updated_at = now
    proposal.status, proposal.version = ("ACCEPTED" if payload.decision == "ACCEPT" else "REJECTED"), proposal.version + 1
    proposal.doctor_id, proposal.reviewed_at, proposal.review_note = payload.doctor_id, now, payload.note
    db.add(AuditLog(actor_type="DOCTOR", actor_id=str(payload.doctor_id), action="REVIEW_AGENT_PROPOSAL", resource_type="AgentProposal", resource_id=str(proposal.id),
        new_state={"decision": payload.decision, "effect": payload.effect, "child_loop_id": proposal.child_loop_id, "note": payload.note}, run_id=proposal.run_id, created_at=now))
    db.commit()
    return {"status": proposal.status, "child_loop_id": proposal.child_loop_id, "loop_status": loop.status}
