from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class LoopStatus(StrEnum):
    PROPOSED = "PROPOSED"
    WAITING_EVENT = "WAITING_EVENT"
    RESULT_AVAILABLE = "RESULT_AVAILABLE"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class LoopTransitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current: LoopStatus
    target: LoopStatus
    intent_type: Literal["FOLLOW_RESULT", "RECHECK", "MONITOR", "ACTION"] = "FOLLOW_RESULT"
    has_evidence: bool = False
    clinician_confirmed: bool = False
    resolution_note: str | None = None
    occurred_at: datetime
    report_revision: bool = False

    @field_validator("occurred_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at 必须带时区")
        return value


class LoopState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    loop_id: int = Field(gt=0)
    patient_id: int = Field(gt=0)
    encounter_id: int = Field(gt=0)
    status: LoopStatus
    waiting_for: str | None = None
    version: int = Field(ge=1)
