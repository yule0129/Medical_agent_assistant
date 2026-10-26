"""Shared doctor scope checks and request contracts for Agent endpoints."""
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from backend.models import OpenLoop
from backend.services.intent_service import require_scope


def authorize(db, encounter_id, doctor):
    try:
        require_scope(db, encounter_id, doctor)
    except ValueError as exc:
        raise HTTPException(403, str(exc)) from exc



def task(db, loop_id, doctor, lock=False):
    row = db.scalar(select(OpenLoop).where(OpenLoop.id == loop_id).with_for_update()) if lock else db.get(OpenLoop, loop_id)
    if not row:
        raise HTTPException(404, "任务不存在")
    authorize(db, row.encounter_id, doctor)
    return row



class Body(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    doctor_id: int = Field(default=1, gt=0)



class Start(Body):
    expected_version: int = Field(ge=1)
    request_key: str = Field(min_length=1, max_length=100)



def start_readiness(loop):
    if loop.status in {"CANCELLED", "REJECTED"}:
        return False, "该任务已取消，不能再启动调查。"
    from backend.contracts.reports import MatchingCriteria
    try:
        MatchingCriteria.model_validate(loop.matching_criteria or {})
    except ValidationError:
        if loop.current_evidence_id:
            return False, "该任务已有手工结果，但未设置报告匹配编号。请在下方建立带标本号或医嘱号的后续随访任务，再启用 Agent 跟进；现有记录保留。"
        return False, "请先在下方设置报告匹配条件，填写检验项目及标本号、医嘱号或报告号，再启用 Agent。"
    return True, ""
