"""Agent run/step, durable job and reviewed proposal ORM mappings.

These tables share backend.db.Base metadata and retain their original names.
"""
from datetime import datetime
from typing import Any
from sqlalchemy import DateTime, ForeignKey, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from backend.db import Base


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_key: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    intent_id: Mapped[int | None] = mapped_column(ForeignKey("clinical_intents.id"), nullable=True)
    loop_id: Mapped[int | None] = mapped_column(ForeignKey("open_loops.id"), nullable=True)
    prior_run_id: Mapped[int | None] = mapped_column(ForeignKey("agent_runs.id"), nullable=True)
    trigger_event_id: Mapped[int | None] = mapped_column(ForeignKey("clinical_events.id"), nullable=True)
    plan_version: Mapped[int] = mapped_column(default=1)
    provider: Mapped[str] = mapped_column(String(30), default="rules")
    status: Mapped[str] = mapped_column(String(30), default="RUNNING")
    stop_reason: Mapped[str | None] = mapped_column(String(80), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)



class AgentStep(Base):
    __tablename__ = "agent_steps"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    step_number: Mapped[int] = mapped_column()
    action: Mapped[str] = mapped_column(String(60))
    tool_name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    input_summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result_summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    duration_ms: Mapped[int | None] = mapped_column(nullable=True)



class AgentJob(Base):
    """Durable model investigation queue, separate from clinical event transactions."""
    __tablename__ = "agent_jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_key: Mapped[str] = mapped_column(String(100), unique=True)
    loop_id: Mapped[int] = mapped_column(ForeignKey("open_loops.id"), index=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("clinical_events.id"))
    doctor_id: Mapped[int] = mapped_column(ForeignKey("doctors.id"))
    status: Mapped[str] = mapped_column(String(30), default="PENDING")
    attempt: Mapped[int] = mapped_column(default=0)
    lease_token: Mapped[str | None] = mapped_column(String(40), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("agent_runs.id"), nullable=True)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))



class AgentProposal(Base):
    """Guard-verified candidate; acceptance never implicitly closes a clinical task."""
    __tablename__ = "agent_proposals"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("agent_runs.id"), unique=True)
    loop_id: Mapped[int] = mapped_column(ForeignKey("open_loops.id"), index=True)
    expected_loop_version: Mapped[int] = mapped_column()
    evidence_id: Mapped[int | None] = mapped_column(ForeignKey("evidence_nodes.id"), nullable=True)
    content: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(30), default="PENDING_REVIEW")
    version: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    doctor_id: Mapped[int | None] = mapped_column(ForeignKey("doctors.id"), nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    child_loop_id: Mapped[int | None] = mapped_column(ForeignKey("open_loops.id"), nullable=True)
