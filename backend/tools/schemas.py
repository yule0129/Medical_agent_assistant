from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ToolStatus(StrEnum):
    FOUND = "found"
    EMPTY = "empty"
    PARTIAL = "partial"
    ERROR = "error"


class ToolContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor_id: int = Field(gt=0)
    patient_id: int = Field(gt=0)
    encounter_id: int = Field(gt=0)
    limit: int = Field(default=50, ge=1, le=100)
    from_time: datetime | None = None
    to_time: datetime | None = None
    report_id: str | None = None
    specimen_id: str | None = None
    order_id: str | None = None

    @field_validator("from_time", "to_time")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("时间范围必须带时区")
        return value


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ToolStatus
    tool_name: str
    items: list[dict[str, Any]] = Field(default_factory=list)
    sources: list[dict[str, Any]] = Field(default_factory=list)
    query: dict[str, Any] = Field(default_factory=dict)
    complete: bool = True
    error_code: str | None = None
    error_message: str | None = None
