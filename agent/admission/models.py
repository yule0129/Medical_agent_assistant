from datetime import datetime
from typing import Any
from sqlalchemy import DateTime, ForeignKey, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from backend.db import Base


class AdmissionWorkspace(Base):
    __tablename__ = "admission_workspaces"
    id: Mapped[int] = mapped_column(primary_key=True)
    encounter_id: Mapped[int] = mapped_column(ForeignKey("encounters.id"), unique=True)
    transcript: Mapped[str] = mapped_column(Text, default="")
    examination_text: Mapped[str] = mapped_column(Text, default="")
    input_version: Mapped[int] = mapped_column(default=1)
    status: Mapped[str] = mapped_column(String(40), default="NEEDS_INPUT")
    generation_token: Mapped[str | None] = mapped_column(String(40), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    draft: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    trace: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_by: Mapped[int | None] = mapped_column(ForeignKey("doctors.id"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
