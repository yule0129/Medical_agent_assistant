from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class IntentType(StrEnum):
    FOLLOW_RESULT = "FOLLOW_RESULT"


class ClinicalIntentCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    patient_id: int = Field(gt=0)
    encounter_id: int = Field(gt=0)
    intent_type: IntentType
    raw_text: str = Field(min_length=1)
    target: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
