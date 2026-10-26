from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base
from agent.admission.models import AdmissionWorkspace  # register shared metadata


class LoopStatus(str, enum.Enum):
    PROPOSED = "PROPOSED"
    WAITING_EVENT = "WAITING_EVENT"
    RESULT_AVAILABLE = "RESULT_AVAILABLE"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class JobStatus(str, enum.Enum):
    PENDING = "PENDING"
    LEASED = "LEASED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class Doctor(Base):
    __tablename__ = "doctors"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(80))
    department: Mapped[str] = mapped_column(String(120))
    patients: Mapped[list["Patient"]] = relationship(back_populates="doctor")


class Patient(Base):
    __tablename__ = "patients"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    admission_number: Mapped[str] = mapped_column(String(40), index=True)
    bed_number: Mapped[str] = mapped_column(String(20))
    gender: Mapped[str] = mapped_column(String(10), default="未记录")
    age: Mapped[int] = mapped_column(default=0)
    ethnicity: Mapped[str | None] = mapped_column(String(40), nullable=True)
    marital_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    birthplace: Mapped[str | None] = mapped_column(String(160), nullable=True)
    occupation: Mapped[str | None] = mapped_column(String(120), nullable=True)
    diagnosis: Mapped[str] = mapped_column(String(255))
    doctor_id: Mapped[int | None] = mapped_column(ForeignKey("doctors.id"), nullable=True)
    doctor: Mapped["Doctor | None"] = relationship(back_populates="patients")
    records: Mapped[list["MedicalRecord"]] = relationship(back_populates="patient", cascade="all, delete-orphan")
    encounters: Mapped[list["Encounter"]] = relationship(back_populates="patient", cascade="all, delete-orphan")

class Encounter(Base):
    __tablename__ = "encounters"

    id: Mapped[int] = mapped_column(primary_key=True)
    encounter_number: Mapped[str] = mapped_column(String(60), unique=True, index=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    attending_doctor_id: Mapped[int | None] = mapped_column(ForeignKey("doctors.id"), nullable=True)
    admitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    history_reporter: Mapped[str | None] = mapped_column(String(120), nullable=True)
    discharged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="INPATIENT")
    diagnosis_snapshot: Mapped[str] = mapped_column(String(255), default="")
    patient: Mapped["Patient"] = relationship(back_populates="encounters")
    assignments: Mapped[list["DoctorEncounterAssignment"]] = relationship(back_populates="encounter", cascade="all, delete-orphan")


class DoctorEncounterAssignment(Base):
    __tablename__ = "doctor_encounter_assignments"

    id: Mapped[int] = mapped_column(primary_key=True)
    doctor_id: Mapped[int] = mapped_column(ForeignKey("doctors.id"), index=True)
    encounter_id: Mapped[int] = mapped_column(ForeignKey("encounters.id"), index=True)
    role: Mapped[str] = mapped_column(String(40), default="ATTENDING")
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    doctor: Mapped["Doctor"] = relationship()
    encounter: Mapped["Encounter"] = relationship(back_populates="assignments")
    __table_args__ = (UniqueConstraint("doctor_id", "encounter_id", name="uq_doctor_encounter"),)


class MedicalRecord(Base):
    __tablename__ = "medical_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    record_number: Mapped[str] = mapped_column(String(60), unique=True, index=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    doctor_id: Mapped[int | None] = mapped_column(ForeignKey("doctors.id"), nullable=True)
    encounter_id: Mapped[int | None] = mapped_column(ForeignKey("encounters.id"), nullable=True, index=True)
    source_rounding_session_id: Mapped[int | None] = mapped_column(ForeignKey("rounding_sessions.id"), nullable=True)
    record_type: Mapped[str] = mapped_column(String(80), default="日常病程记录")
    content: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="待审核")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    patient: Mapped["Patient"] = relationship(back_populates="records")
    doctor: Mapped["Doctor | None"] = relationship()


class RoundingSession(Base):
    __tablename__ = "rounding_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    request_key: Mapped[str | None] = mapped_column(String(100), nullable=True, unique=True)
    encounter_id: Mapped[int] = mapped_column(ForeignKey("encounters.id"), index=True)
    doctor_id: Mapped[int] = mapped_column(ForeignKey("doctors.id"), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    transcript: Mapped[str] = mapped_column(Text, default="")
    generated_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="DRAFT")
    audio_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    transcript_source: Mapped[str] = mapped_column(String(30), default="manual")


class ClinicalEvent(Base):
    __tablename__ = "clinical_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_key: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    event_type: Mapped[str] = mapped_column(String(60), index=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    encounter_id: Mapped[int] = mapped_column(ForeignKey("encounters.id"), index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(80))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class EventJob(Base):
    __tablename__ = "event_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("clinical_events.id"), unique=True, index=True)
    status: Mapped[JobStatus] = mapped_column(String(20), default=JobStatus.PENDING)
    attempt: Mapped[int] = mapped_column(default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class ClinicalIntent(Base):
    __tablename__ = "clinical_intents"

    id: Mapped[int] = mapped_column(primary_key=True)
    intent_key: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    encounter_id: Mapped[int] = mapped_column(ForeignKey("encounters.id"), index=True)
    source_event_id: Mapped[int | None] = mapped_column(ForeignKey("clinical_events.id"), nullable=True)
    raw_text: Mapped[str] = mapped_column(Text)
    target: Mapped[str | None] = mapped_column(String(120), nullable=True)
    intent_type: Mapped[str] = mapped_column(String(60))
    status: Mapped[str] = mapped_column(String(30), default="CANDIDATE")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OpenLoop(Base):
    __tablename__ = "open_loops"

    id: Mapped[int] = mapped_column(primary_key=True)
    loop_key: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    intent_id: Mapped[int] = mapped_column(ForeignKey("clinical_intents.id"), index=True)
    parent_loop_id: Mapped[int | None] = mapped_column(ForeignKey("open_loops.id"), nullable=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    encounter_id: Mapped[int] = mapped_column(ForeignKey("encounters.id"), index=True)
    waiting_for: Mapped[str | None] = mapped_column(String(120), nullable=True)
    status: Mapped[LoopStatus] = mapped_column(String(30), default=LoopStatus.PROPOSED)
    version: Mapped[int] = mapped_column(default=1)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    state_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    matching_criteria: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    workflow_memory: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    current_evidence_id: Mapped[int | None] = mapped_column(ForeignKey("evidence_nodes.id"), nullable=True)


class EvidenceNode(Base):
    __tablename__ = "evidence_nodes"

    id: Mapped[int] = mapped_column(primary_key=True)
    evidence_key: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    event_id: Mapped[int | None] = mapped_column(ForeignKey("clinical_events.id"), nullable=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    encounter_id: Mapped[int] = mapped_column(ForeignKey("encounters.id"), index=True)
    source_type: Mapped[str] = mapped_column(String(60))
    source_ref: Mapped[str] = mapped_column(String(120))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    content: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    verification_status: Mapped[str] = mapped_column(String(30), default="UNVERIFIED")


class Finding(Base):
    __tablename__ = "findings"

    id: Mapped[int] = mapped_column(primary_key=True)
    loop_id: Mapped[int] = mapped_column(ForeignKey("open_loops.id"), index=True)
    evidence_id: Mapped[int] = mapped_column(ForeignKey("evidence_nodes.id"), index=True)
    claim: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(default=0.0)
    status: Mapped[str] = mapped_column(String(30), default="PENDING_REVIEW")
    searched_sources: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(default=1)
    __table_args__ = (UniqueConstraint("loop_id", "evidence_id", name="uq_finding_loop_evidence"),)


class LabReport(Base):
    __tablename__ = "lab_reports"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("clinical_events.id"), unique=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    encounter_id: Mapped[int] = mapped_column(ForeignKey("encounters.id"), index=True)
    report_id: Mapped[str] = mapped_column(String(100))
    report_version: Mapped[int] = mapped_column()
    test_name: Mapped[str] = mapped_column(String(120))
    specimen_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    order_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    result_text: Mapped[str] = mapped_column(Text)
    reported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("patient_id", "encounter_id", "report_id", "report_version", name="uq_lab_report_version"),)






class ReviewDecision(Base):
    __tablename__ = "review_decisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    finding_id: Mapped[int] = mapped_column(ForeignKey("findings.id"), index=True)
    doctor_id: Mapped[int] = mapped_column(ForeignKey("doctors.id"), index=True)
    decision: Mapped[str] = mapped_column(String(30))
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    idempotency_key: Mapped[str] = mapped_column(String(100), unique=True)


class ResultAcknowledgement(Base):
    __tablename__ = "result_acknowledgements"

    id: Mapped[int] = mapped_column(primary_key=True)
    loop_id: Mapped[int] = mapped_column(ForeignKey("open_loops.id"), index=True)
    evidence_id: Mapped[int] = mapped_column(ForeignKey("evidence_nodes.id"), index=True)
    doctor_id: Mapped[int] = mapped_column(ForeignKey("doctors.id"), index=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    idempotency_key: Mapped[str] = mapped_column(String(100), unique=True)


class HandoffReport(Base):
    __tablename__ = "handoff_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    encounter_id: Mapped[int] = mapped_column(ForeignKey("encounters.id"), index=True)
    doctor_id: Mapped[int] = mapped_column(ForeignKey("doctors.id"), index=True)
    content: Mapped[str] = mapped_column(Text)
    snapshot_version: Mapped[int] = mapped_column(default=1)
    status: Mapped[str] = mapped_column(String(30), default="DRAFT")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    references: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    actor_type: Mapped[str] = mapped_column(String(30))
    actor_id: Mapped[str] = mapped_column(String(80))
    action: Mapped[str] = mapped_column(String(80))
    resource_type: Mapped[str] = mapped_column(String(60))
    resource_id: Mapped[str] = mapped_column(String(80))
    old_state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    new_state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("agent_runs.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

# Compatibility exports for existing migrations and rule-workflow services.
# Actual Agent ORM mappings live in the Agent component directory.
from agent.persistence.models import AgentJob, AgentProposal, AgentRun, AgentStep  # noqa: E402,F401
