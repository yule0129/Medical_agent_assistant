from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session
from sqlalchemy import select

from ..models import AgentRun, AgentStep, ClinicalEvent, OpenLoop


def start_run(db: Session, event: ClinicalEvent, loop: OpenLoop | None = None, prior_run_id: int | None = None, plan_version: int = 1, intent_id: int | None = None) -> AgentRun:
    effective_intent_id = loop.intent_id if loop else intent_id
    suffix = str(loop.id) if loop else f"intent-{effective_intent_id or 'event'}"
    run_key = f"RUN-{event.event_key}-{suffix}-{prior_run_id or 0}"
    existing = db.scalar(select(AgentRun).where(AgentRun.run_key == run_key))
    if existing is not None:
        return existing
    run = AgentRun(run_key=run_key, intent_id=effective_intent_id, loop_id=loop.id if loop else None, prior_run_id=prior_run_id, trigger_event_id=event.id, plan_version=plan_version, provider="rules", status="RUNNING", started_at=datetime.now(timezone.utc))
    db.add(run)
    db.flush()
    return run


def add_step(db: Session, run: AgentRun, number: int, action: str, tool_name: str | None = None, result: dict | None = None) -> None:
    db.add(AgentStep(run_id=run.id, step_number=number, action=action, tool_name=tool_name, result_summary=result or {}))
