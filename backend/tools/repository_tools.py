from __future__ import annotations

from datetime import timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ClinicalEvent, Encounter, LabReport, MedicalRecord, OpenLoop, Patient
from .registry import ToolRegistry
from .schemas import ToolContext, ToolResult, ToolStatus


def _scope(db: Session, context: ToolContext) -> tuple[Patient | None, Encounter | None, str | None]:
    patient = db.get(Patient, context.patient_id)
    encounter = db.get(Encounter, context.encounter_id)
    if patient is None or encounter is None:
        return patient, encounter, "SCOPE_NOT_FOUND"
    if encounter.patient_id != patient.id:
        return patient, encounter, "PATIENT_ENCOUNTER_MISMATCH"
    if patient.doctor_id != context.actor_id and encounter.attending_doctor_id != context.actor_id:
        return patient, encounter, "ACTOR_OUT_OF_SCOPE"
    return patient, encounter, None


def _error(name: str, context: ToolContext, code: str) -> ToolResult:
    return ToolResult(status=ToolStatus.ERROR, tool_name=name, query=context.model_dump(mode="json"), complete=False, error_code=code, error_message="读取范围校验失败")


def get_patient_snapshot(db: Session, context: ToolContext) -> ToolResult:
    patient, encounter, error = _scope(db, context)
    if error:
        return _error("get_patient_snapshot", context, error)
    item = {"patient_id": patient.id, "name": patient.name, "gender": patient.gender, "age": patient.age, "diagnosis": encounter.diagnosis_snapshot, "encounter_number": encounter.encounter_number, "status": encounter.status}
    return ToolResult(status=ToolStatus.FOUND, tool_name="get_patient_snapshot", items=[item], sources=[{"type": "encounter", "id": encounter.id}], query=context.model_dump(mode="json"))


def get_recent_events(db: Session, context: ToolContext) -> ToolResult:
    _, _, error = _scope(db, context)
    if error:
        return _error("get_recent_events", context, error)
    query = select(ClinicalEvent).where(ClinicalEvent.patient_id == context.patient_id, ClinicalEvent.encounter_id == context.encounter_id).order_by(ClinicalEvent.occurred_at.desc()).limit(context.limit)
    if context.from_time:
        query = query.where(ClinicalEvent.occurred_at >= context.from_time)
    if context.to_time:
        query = query.where(ClinicalEvent.occurred_at <= context.to_time)
    rows = db.scalars(query).all()
    items = [{"id": row.id, "event_key": row.event_key, "event_type": row.event_type, "occurred_at": row.occurred_at.isoformat(), "source": row.source, "payload": row.payload} for row in rows]
    return ToolResult(status=ToolStatus.FOUND if items else ToolStatus.EMPTY, tool_name="get_recent_events", items=items, sources=[{"type": "clinical_event", "id": row.id} for row in rows], query=context.model_dump(mode="json"))


def get_labs(db: Session, context: ToolContext) -> ToolResult:
    result = get_recent_events(db, context)
    result.tool_name = "get_labs"
    if result.status != ToolStatus.ERROR:
        result.items = [item for item in result.items if item["event_type"] == "LAB_RESULT_CREATED"]
        result.sources = [{"type": "clinical_event", "id": item["id"]} for item in result.items]
        result.status = ToolStatus.FOUND if result.items else ToolStatus.EMPTY
    return result


def get_progress_notes(db: Session, context: ToolContext) -> ToolResult:
    _, _, error = _scope(db, context)
    if error:
        return _error("get_progress_notes", context, error)
    rows = db.scalars(select(MedicalRecord).where(MedicalRecord.patient_id == context.patient_id, MedicalRecord.encounter_id == context.encounter_id).order_by(MedicalRecord.created_at.desc()).limit(context.limit)).all()
    items = [{"id": row.id, "record_number": row.record_number, "record_type": row.record_type, "created_at": row.created_at.astimezone(timezone.utc).isoformat(), "status": row.status, "content": row.content} for row in rows]
    return ToolResult(status=ToolStatus.FOUND if items else ToolStatus.EMPTY, tool_name="get_progress_notes", items=items, sources=[{"type": "medical_record", "id": row.id} for row in rows], query=context.model_dump(mode="json"))


def get_loop_state(db: Session, context: ToolContext) -> ToolResult:
    _, _, error = _scope(db, context)
    if error:
        return _error("get_loop_state", context, error)
    rows = db.scalars(select(OpenLoop).where(OpenLoop.patient_id == context.patient_id, OpenLoop.encounter_id == context.encounter_id).order_by(OpenLoop.updated_at.desc()).limit(context.limit)).all()
    items = [{"id": row.id, "loop_key": row.loop_key, "status": row.status, "waiting_for": row.waiting_for, "version": row.version, "state_reason": row.state_reason} for row in rows]
    return ToolResult(status=ToolStatus.FOUND if items else ToolStatus.EMPTY, tool_name="get_loop_state", items=items, sources=[{"type": "open_loop", "id": row.id} for row in rows], query=context.model_dump(mode="json"))


def get_handoff(db: Session, context: ToolContext) -> ToolResult:
    from ..models import HandoffReport
    _, _, error = _scope(db, context)
    if error:
        return _error("get_handoff", context, error)
    rows = db.scalars(select(HandoffReport).where(HandoffReport.encounter_id == context.encounter_id).order_by(HandoffReport.id.desc()).limit(context.limit + 1)).all()
    complete = len(rows) <= context.limit
    rows = rows[:context.limit]
    return ToolResult(tool_name="get_handoff", status=ToolStatus.PARTIAL if not complete else ToolStatus.FOUND if rows else ToolStatus.EMPTY,
        complete=complete, items=[{"id": r.id, "content": r.content, "status": r.status, "version": r.snapshot_version} for r in rows],
        sources=[{"type": "handoff_report", "id": r.id, "version": r.snapshot_version} for r in rows], query=context.model_dump(mode="json"))


def build_default_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register("get_patient_snapshot", get_patient_snapshot)
    registry.register("get_recent_events", get_recent_events)
    registry.register("get_labs", get_labs)
    registry.register("get_progress_notes", get_progress_notes)
    registry.register("get_handoff", get_handoff)
    registry.register("get_loop_state", get_loop_state)
    registry.register("get_lab_reports", get_lab_reports)
    return registry


def get_lab_reports(db: Session, context: ToolContext) -> ToolResult:
    _, _, error = _scope(db, context)
    if error:
        return _error("get_lab_reports", context, error)
    query = select(LabReport).where(LabReport.patient_id == context.patient_id, LabReport.encounter_id == context.encounter_id)
    for key in ("report_id", "specimen_id", "order_id"):
        if getattr(context, key):
            query = query.where(getattr(LabReport, key) == getattr(context, key))
    if context.from_time:
        query = query.where(LabReport.reported_at >= context.from_time)
    if context.to_time:
        query = query.where(LabReport.reported_at <= context.to_time)
    rows = db.scalars(query.order_by(LabReport.report_version.desc(), LabReport.id.desc()).limit(context.limit + 1)).all()
    complete = len(rows) <= context.limit
    rows = rows[:context.limit]
    return ToolResult(tool_name="get_lab_reports", status=ToolStatus.PARTIAL if not complete else ToolStatus.FOUND if rows else ToolStatus.EMPTY,
        complete=complete, items=[{"id": r.id, "event_id": r.event_id, "report_id": r.report_id, "report_version": r.report_version} for r in rows],
        sources=[{"type": "lab_report", "id": r.id, "event_id": r.event_id, "version": r.report_version} for r in rows], query=context.model_dump(mode="json"))
