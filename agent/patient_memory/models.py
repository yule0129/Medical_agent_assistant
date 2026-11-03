from datetime import datetime
from typing import Any
from sqlalchemy import DateTime, ForeignKey, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from backend.db import Base


class MatterScan(Base):
    __tablename__ = 'patient_matter_scans'
    id: Mapped[int] = mapped_column(primary_key=True)
    scan_key: Mapped[str] = mapped_column(String(64), unique=True)
    encounter_id: Mapped[int] = mapped_column(ForeignKey('encounters.id'), index=True)
    doctor_id: Mapped[int] = mapped_column(ForeignKey('doctors.id'))
    source_kind: Mapped[str] = mapped_column(String(20))
    source_id: Mapped[int] = mapped_column()
    source_digest: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20))
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt: Mapped[int] = mapped_column(default=0, server_default='0')
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PatientMatter(Base):
    __tablename__ = 'patient_matters'
    id: Mapped[int] = mapped_column(primary_key=True)
    matter_key: Mapped[str] = mapped_column(String(64), unique=True)
    scan_id: Mapped[int] = mapped_column(ForeignKey('patient_matter_scans.id'))
    patient_id: Mapped[int] = mapped_column(ForeignKey('patients.id'), index=True)
    encounter_id: Mapped[int] = mapped_column(ForeignKey('encounters.id'), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    title: Mapped[str] = mapped_column(String(200))
    quote: Mapped[str] = mapped_column(Text)
    due_text: Mapped[str] = mapped_column(String(200), default='')
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    assigned_doctor_id: Mapped[int | None] = mapped_column(ForeignKey('doctors.id'), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default='CANDIDATE')
    version: Mapped[int] = mapped_column(default=1)
    source: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reviewed_by: Mapped[int | None] = mapped_column(ForeignKey('doctors.id'), nullable=True)
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)
