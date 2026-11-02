from datetime import datetime, timezone
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session
from backend.db import get_db
from backend.models import AuditLog, Doctor, Encounter, Patient
from backend.services.intent_service import require_scope
from backend.services.report_service import utc
from .models import PatientMatter, MatterScan
from . import service

router=APIRouter()


def scope(db,encounter_id,doctor_id,lock=False):
    query=select(Encounter).where(Encounter.id==encounter_id)
    row=db.scalar(query.with_for_update() if lock else query)
    if not row:raise HTTPException(404,'住院记录不存在')
    try:require_scope(db,encounter_id,doctor_id)
    except ValueError as exc:raise HTTPException(403,'无权操作该次住院事项') from exc
    return row


@router.get('/encounters/{encounter_id}/matters')
def list_matters(encounter_id:int,doctor_id:int=1,db:Session=Depends(get_db)):
    encounter=scope(db,encounter_id,doctor_id)
    rows=db.scalars(select(PatientMatter).where(PatientMatter.encounter_id==encounter_id).order_by(PatientMatter.id.desc())).all()
    return {'items':[service.item_out(db,r,encounter) for r in rows],
        'sources':service.source_list(db,encounter),'model_configured':service.configured(),
        'owners':[{'id':d.id,'name':d.name} for d in db.scalars(select(Doctor))
                  if d.id in {db.get(Patient,encounter.patient_id).doctor_id,encounter.attending_doctor_id}],
        'scans':[{'id':s.id,'source_kind':s.source_kind,'source_id':s.source_id,'status':s.status,'error_code':s.error_code,
                  'created_at':utc(s.created_at).isoformat()} for s in db.scalars(select(MatterScan).where(
                      MatterScan.encounter_id==encounter_id).order_by(MatterScan.id.desc()).limit(20))]}


class ScanRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    doctor_id:int=Field(gt=0)
    source_kind:Literal['ADMISSION','ROUNDING']
    source_id:int=Field(gt=0)
    expected_digest:str=Field(min_length=64,max_length=64)


@router.post('/encounters/{encounter_id}/matters/scan')
def scan_matters(encounter_id:int,payload:ScanRequest,db:Session=Depends(get_db)):
    row=scope(db,encounter_id,payload.doctor_id,True)
    return service.extract_matters(db,row,payload.doctor_id,payload.source_kind,payload.source_id,payload.expected_digest)


class ReviewRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    doctor_id:int=Field(gt=0)
    expected_version:int=Field(ge=1)
    action:Literal['CONFIRM','DISMISS','UPDATE','COMPLETE','CANCEL']
    title:str|None=Field(default=None,min_length=1,max_length=200)
    assigned_doctor_id:int|None=Field(default=None,gt=0)
    due_at:datetime|None=None
    note:str=Field(default='',max_length=5000)

    @field_validator('due_at')
    @classmethod
    def timezone_required(cls,value):
        if value is not None and value.utcoffset() is None:raise ValueError('时间必须带时区')
        return value


@router.post('/matters/{matter_id}/review')
def review_matter(matter_id:int,payload:ReviewRequest,db:Session=Depends(get_db)):
    initial=db.get(PatientMatter,matter_id)
    if not initial:raise HTTPException(404,'事项不存在')
    encounter=scope(db,initial.encounter_id,payload.doctor_id,True)
    row=db.scalar(select(PatientMatter).where(PatientMatter.id==matter_id).with_for_update().execution_options(populate_existing=True))
    if row.version!=payload.expected_version:raise HTTPException(409,'事项已更新，请刷新后核对')
    allowed={'CONFIRM':('CANDIDATE','OPEN'),'DISMISS':('CANDIDATE','DISMISSED'),
             'UPDATE':('OPEN','OPEN'),'COMPLETE':('OPEN','COMPLETED'),'CANCEL':('OPEN','CANCELLED')}
    before,target=allowed[payload.action]
    if row.status!=before:raise HTTPException(409,'当前事项状态不支持此操作')
    old={'status':row.status,'version':row.version,'title':row.title,'assigned_doctor_id':row.assigned_doctor_id,'due_at':utc(row.due_at).isoformat() if row.due_at else None}
    if payload.action in {'CONFIRM','UPDATE'}:
        if payload.action=='CONFIRM' and service.item_out(db,row,encounter)['source_changed']:
            raise HTTPException(409,'候选依据已变化，请重新识别当前原文')
        owner=payload.assigned_doctor_id or (row.assigned_doctor_id if payload.action=='UPDATE' else payload.doctor_id)
        if not db.get(Doctor,owner):raise HTTPException(422,'负责人医生不存在')
        try:require_scope(db,encounter.id,owner)
        except ValueError as exc:raise HTTPException(403,'负责人不在本次住院医生范围内') from exc
        if payload.title is not None:
            if not payload.title.strip():raise HTTPException(422,'事项标题不能为空')
            row.title=payload.title.strip()
        row.assigned_doctor_id,row.due_at=owner,payload.due_at.astimezone(timezone.utc) if payload.due_at else None
    if payload.action in {'DISMISS','COMPLETE','CANCEL'} and not payload.note.strip():
        raise HTTPException(422,'请填写忽略原因、实际完成情况或取消原因')
    row.status,row.version,row.reviewed_by,row.updated_at=target,row.version+1,payload.doctor_id,datetime.now(timezone.utc)
    if payload.note.strip():row.resolution=payload.note.strip()
    if payload.action in {'CONFIRM','UPDATE'}:
        from .progress.runtime import enqueue
        db.flush();enqueue(db,row)
    db.add(AuditLog(actor_type='DOCTOR',actor_id=str(payload.doctor_id),action='MATTER_'+payload.action,
        resource_type='PatientMatter',resource_id=str(row.id),old_state=old,
        new_state={'status':row.status,'version':row.version,'title':row.title,'note':payload.note,
                   'assigned_doctor_id':row.assigned_doctor_id,'due_at':row.due_at.isoformat() if row.due_at else None},created_at=row.updated_at))
    db.commit()
    return service.item_out(db,row,encounter)


@router.get('/matter-tasks')
def matter_tasks(doctor_id:int=1,db:Session=Depends(get_db)):
    result=[]
    for row in db.scalars(select(PatientMatter).where(PatientMatter.status.in_(['OPEN','COMPLETED','CANCELLED']))):
        try:scope(db,row.encounter_id,doctor_id)
        except HTTPException:continue
        patient=db.get(Patient,row.patient_id)
        result.append({'id':f'matter-{row.id}','matter_id':row.id,'patient_id':row.patient_id,'patient':patient.name,
            'encounter_id':row.encounter_id,'intent_type':'PATIENT_MATTER','waiting_for':row.title,
            'status':{'OPEN':'WAITING_EVENT','COMPLETED':'RESOLVED','CANCELLED':'CANCELLED'}[row.status],
            'due_at':utc(row.due_at).isoformat() if row.due_at else None,
            'overdue':row.status=='OPEN' and row.due_at is not None and utc(row.due_at)<datetime.now(timezone.utc),
            'state_reason':row.resolution or '医生已确认，持续保存在本次住院事项中'})
    return result
