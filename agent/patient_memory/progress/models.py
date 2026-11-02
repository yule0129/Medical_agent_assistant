from datetime import datetime
from typing import Any
from sqlalchemy import DateTime, ForeignKey, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from backend.db import Base


class MatterProgressJob(Base):
    __tablename__='matter_progress_jobs'
    id: Mapped[int]=mapped_column(primary_key=True)
    job_key: Mapped[str]=mapped_column(String(64),unique=True)
    matter_id: Mapped[int]=mapped_column(ForeignKey('patient_matters.id'),index=True)
    matter_version: Mapped[int]=mapped_column()
    doctor_id: Mapped[int]=mapped_column(ForeignKey('doctors.id'))
    snapshot_digest: Mapped[str]=mapped_column(String(64))
    snapshot: Mapped[dict[str,Any]]=mapped_column(JSON)
    status: Mapped[str]=mapped_column(String(20),default='PENDING')
    attempt: Mapped[int]=mapped_column(default=0)
    lease_token: Mapped[str|None]=mapped_column(String(40),nullable=True)
    lease_until: Mapped[datetime|None]=mapped_column(DateTime(timezone=True),nullable=True)
    retry_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True),nullable=True)
    error_code: Mapped[str|None]=mapped_column(String(100),nullable=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True),nullable=True)


class MatterProgress(Base):
    __tablename__='matter_progress'
    id: Mapped[int]=mapped_column(primary_key=True)
    job_id: Mapped[int]=mapped_column(ForeignKey('matter_progress_jobs.id'),unique=True)
    matter_id: Mapped[int]=mapped_column(ForeignKey('patient_matters.id'),index=True)
    state: Mapped[str]=mapped_column(String(40))
    summary: Mapped[str]=mapped_column(Text)
    evidence: Mapped[list[dict[str,Any]]]=mapped_column(JSON,default=list)
    model: Mapped[str]=mapped_column(String(100))
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True))
    acknowledged_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True),nullable=True)
    acknowledged_by: Mapped[int|None]=mapped_column(ForeignKey('doctors.id'),nullable=True)
