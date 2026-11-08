from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class Proposal(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    kind: Literal['VERIFY_INFO', 'RECHECK', 'CONTACT', 'OTHER']
    title: str = Field(min_length=1, max_length=200)
    quote: str = Field(min_length=2, max_length=3000)
    due_text: str = Field(default='', max_length=200)
    classification: Literal['ACTIONABLE','NEEDS_CLARIFICATION','BACKGROUND','ALREADY_DONE','NEGATED','PATIENT_REQUEST','CONDITIONAL']
    action_text: str = Field(max_length=80)
    target_text: str = Field(max_length=200)
    evidence_kind: Literal['LAB_REPORT','VERIFICATION_RECORD','CONTACT_RECORD','CLINICIAN_NOTE','UNSPECIFIED']
    clarification: str = Field(default='', max_length=500)


class Extraction(BaseModel):
    model_config = ConfigDict(extra='forbid')
    items: list[Proposal] = Field(max_length=20)


class Assessment(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    index: int = Field(ge=0)
    decision: Literal['KEEP','CLARIFY','SKIP']
    reason: str = Field(min_length=1,max_length=500)
    duplicate_ids: list[int] = Field(default_factory=list,max_length=10)


class ExtractionReview(BaseModel):
    model_config = ConfigDict(extra='forbid')
    assessments: list[Assessment] = Field(max_length=20)
