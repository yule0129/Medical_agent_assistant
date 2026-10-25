from enum import StrEnum

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class EventType(StrEnum):
    NOTE_CREATED = "NOTE_CREATED"
    LAB_RESULT_CREATED = "LAB_RESULT_CREATED"
    PROGRESS_NOTE_CREATED = "PROGRESS_NOTE_CREATED"
    HANDOFF_STARTED = "HANDOFF_STARTED"


class ClinicalEventInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_key: str = Field(min_length=1)
    event_type: EventType
    patient_id: int = Field(gt=0)
    encounter_id: int = Field(gt=0)
    occurred_at: datetime
    source: str = Field(min_length=1)
    payload: dict = Field(default_factory=dict)

    @field_validator("occurred_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at 必须带时区")
        return value
