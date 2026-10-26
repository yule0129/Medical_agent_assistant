"""Provider-independent schemas. Parsing is not authorization or evidence verification."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from backend.contracts.loops import LoopStatus
from backend.contracts.reports import MatchingCriteria


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TimeWindow(Contract):
    from_time: datetime
    to_time: datetime

    @field_validator("from_time", "to_time")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("time must include timezone")
        return value

    @model_validator(mode="after")
    def ordered(self):
        if self.to_time < self.from_time:
            raise ValueError("time window is reversed")
        return self


class RunScope(Contract):
    """Trusted server input, never a model-supplied action argument."""
    actor_id: int = Field(strict=True, gt=0)
    patient_id: int = Field(strict=True, gt=0)
    encounter_id: int = Field(strict=True, gt=0)
    intent_id: int = Field(strict=True, gt=0)
    loop_id: int = Field(strict=True, gt=0)
    expected_loop_version: int = Field(strict=True, ge=1)


class RunBudget(Contract):
    max_tool_calls: int = Field(default=8, strict=True, ge=1, le=20)
    max_model_requests: int = Field(default=8, strict=True, ge=1, le=20)
    timeout_seconds: float = Field(default=30, gt=0, le=120, allow_inf_nan=False)


class AgentRunInput(Contract):
    scope: RunScope
    trigger_event_id: int = Field(strict=True, gt=0)
    goal: str = Field(min_length=1, max_length=2000)
    loop_status: LoopStatus
    matching: MatchingCriteria
    window: TimeWindow
    prior_run_id: int | None = Field(default=None, strict=True, gt=0)
    plan_version: int = Field(default=1, strict=True, ge=1)
    budget: RunBudget = Field(default_factory=RunBudget)


# Describes the MVP vocabulary, not which tools have been implemented or granted.
# The executor must intersect these names with its actual runtime registry.
ToolName = Literal[
    "get_patient_snapshot", "get_lab_reports", "get_progress_notes",
    "get_loop_state", "get_result_acknowledgements", "get_evidence",
]


class ReadArguments(Contract):
    window: TimeWindow | None = None
    limit: int = Field(default=20, strict=True, ge=1, le=100)
    report_id: str | None = Field(default=None, min_length=1, max_length=100)
    report_version: int | None = Field(default=None, strict=True, ge=1)
    evidence_id: int | None = Field(default=None, strict=True, gt=0)


class EvidenceReference(Contract):
    source_type: Literal["lab_report", "medical_record", "result_acknowledgement", "clinical_event"]
    source_id: int = Field(strict=True, gt=0)
    source_version: int = Field(strict=True, ge=1)
    quote: str = Field(min_length=1, max_length=4000)


class SearchCoverage(Contract):
    tool_name: ToolName
    window: TimeWindow
    query_succeeded: bool = Field(strict=True)
    complete: bool = Field(strict=True)


class Candidate(Contract):
    kind: Literal["RESPONSE_FOUND", "RESPONSE_NOT_FOUND", "FOLLOW_UP", "AMBIGUOUS"]
    summary: str = Field(min_length=1, max_length=2000)
    evidence: list[EvidenceReference] = Field(min_length=1, max_length=20)
    searched_sources: list[SearchCoverage] = Field(min_length=1, max_length=10)
    requires_clinician_review: Literal[True] = True
    follow_up: MatchingCriteria | None = None

    @model_validator(mode="after")
    def candidate_requirements(self):
        if (self.kind == "FOLLOW_UP") != (self.follow_up is not None):
            raise ValueError("only FOLLOW_UP requires follow_up matching criteria")
        if self.kind == "RESPONSE_NOT_FOUND":
            if not all(s.query_succeeded and s.complete for s in self.searched_sources):
                raise ValueError("missing response cannot be inferred from failed or partial queries")
            response_tools = {"get_result_acknowledgements", "get_progress_notes"}
            if not response_tools.issubset({s.tool_name for s in self.searched_sources}):
                raise ValueError("missing response requires acknowledgement and progress note coverage")
        return self


class ToolCallAction(Contract):
    action: Literal["call_tool"]
    reason: str = Field(min_length=1, max_length=1000)
    tool_name: ToolName
    arguments: ReadArguments = Field(default_factory=ReadArguments)


class SubmitCandidateAction(Contract):
    action: Literal["submit_candidate"]
    reason: str = Field(min_length=1, max_length=1000)
    candidate: Candidate


class WaitAction(Contract):
    action: Literal["wait"]
    reason: str = Field(min_length=1, max_length=1000)
    waiting_for: list[Literal["LAB_RESULT_CREATED", "PROGRESS_NOTE_CREATED", "DUE_RECHECK"]] = Field(min_length=1, max_length=3)


class HumanReviewAction(Contract):
    action: Literal["request_review"]
    reason: str = Field(min_length=1, max_length=1000)


AgentAction = Annotated[
    ToolCallAction | SubmitCandidateAction | WaitAction | HumanReviewAction,
    Field(discriminator="action"),
]
TerminalAction = Annotated[
    SubmitCandidateAction | WaitAction | HumanReviewAction,
    Field(discriminator="action"),
]
_action_adapter = TypeAdapter(AgentAction)


def parse_action(payload: object) -> AgentAction:
    """Parse untrusted output; the future guard must verify actual sources and scope."""
    return _action_adapter.validate_python(payload)


def action_json_schema() -> dict:
    """Provider-neutral schema; provider-specific adaptation belongs to its adapter."""
    return _action_adapter.json_schema()


class AgentRunResult(Contract):
    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)
    stop_reason: Literal["CANDIDATE_READY", "WAITING_EVENT", "NEEDS_REVIEW", "TOOL_ERROR", "MODEL_ERROR", "BUDGET_EXHAUSTED", "STALE_CONTEXT"]
    final_action: TerminalAction
    model_requests: int = Field(strict=True, ge=0)
    tool_calls: int = Field(strict=True, ge=0)

    @model_validator(mode="after")
    def compatible_stop(self):
        expected = {"CANDIDATE_READY": "submit_candidate", "WAITING_EVENT": "wait"}
        action = expected.get(self.stop_reason, "request_review")
        if self.final_action.action != action:
            raise ValueError("stop reason does not match final action")
        return self
