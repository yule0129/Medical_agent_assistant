from datetime import datetime
from typing import Any
from sqlalchemy import DateTime,ForeignKey,JSON,String,Text
from sqlalchemy.orm import Mapped,mapped_column
from backend.db import Base


class MatterAction(Base):
    __tablename__='matter_actions'
    id:Mapped[int]=mapped_column(primary_key=True)
    action_key:Mapped[str]=mapped_column(String(64),unique=True)
    matter_id:Mapped[int]=mapped_column(ForeignKey('patient_matters.id'),index=True)
    progress_id:Mapped[int]=mapped_column(ForeignKey('matter_progress.id'),index=True)
    expected_matter_version:Mapped[int]=mapped_column()
    content:Mapped[dict[str,Any]]=mapped_column(JSON)
    status:Mapped[str]=mapped_column(String(20),default='PROPOSED')
    version:Mapped[int]=mapped_column(default=1)
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True))
    reviewed_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True),nullable=True)
    reviewed_by:Mapped[int|None]=mapped_column(ForeignKey('doctors.id'),nullable=True)
    note:Mapped[str|None]=mapped_column(Text,nullable=True)
    request_key:Mapped[str|None]=mapped_column(String(100),nullable=True,unique=True)
    request_digest:Mapped[str|None]=mapped_column(String(64),nullable=True)
    outcome:Mapped[dict[str,Any]]=mapped_column(JSON,default=dict)


class MatterLink(Base):
    __tablename__='matter_links'
    id:Mapped[int]=mapped_column(primary_key=True)
    parent_id:Mapped[int]=mapped_column(ForeignKey('patient_matters.id'),index=True)
    child_id:Mapped[int]=mapped_column(ForeignKey('patient_matters.id'),unique=True)
    action_id:Mapped[int]=mapped_column(ForeignKey('matter_actions.id'),unique=True)
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True))
