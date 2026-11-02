from datetime import datetime,timezone
from typing import Literal
from fastapi import APIRouter,Depends,HTTPException
from pydantic import BaseModel,ConfigDict,Field,field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session
from backend.db import get_db
from backend.models import Encounter,AuditLog,Doctor
from backend.services.intent_service import require_scope
from agent.patient_memory.models import PatientMatter
from agent.patient_memory.service import digest
from agent.patient_memory.progress.api import matter_scope
from agent.patient_memory.progress.models import MatterProgress,MatterProgressJob
from agent.patient_memory.progress.runtime import fresh,enqueue
from .models import MatterAction,MatterLink
from .service import action_out,source_from_evidence

router=APIRouter()


@router.get('/matters/{matter_id}/actions')
def list_actions(matter_id:int,doctor_id:int=1,db:Session=Depends(get_db)):
    matter=matter_scope(db,matter_id,doctor_id)
    return [action_out(db,r,matter) for r in db.scalars(select(MatterAction).where(MatterAction.matter_id==matter.id).order_by(MatterAction.id.desc()))]


class ReviewAction(BaseModel):
    model_config=ConfigDict(extra='forbid')
    doctor_id:int=Field(gt=0)
    expected_version:int=Field(ge=1)
    expected_matter_version:int=Field(ge=1)
    decision:Literal['APPROVE','REJECT']
    request_key:str=Field(min_length=1,max_length=100)
    note:str=Field(min_length=1,max_length=5000)
    title:str|None=Field(default=None,min_length=1,max_length=200)
    assigned_doctor_id:int|None=Field(default=None,gt=0)
    due_at:datetime|None=None

    @field_validator('due_at')
    @classmethod
    def timezone_required(cls,value):
        if value is not None and value.utcoffset() is None:raise ValueError('时间必须带时区')
        return value


@router.post('/matter-actions/{action_id}/review')
def review_action(action_id:int,payload:ReviewAction,db:Session=Depends(get_db)):
    initial=db.get(MatterAction,action_id)
    if not initial:raise HTTPException(404,'行动建议不存在')
    matter=matter_scope(db,initial.matter_id,payload.doctor_id,True)
    row=db.scalar(select(MatterAction).where(MatterAction.id==action_id).with_for_update().execution_options(populate_existing=True))
    request_digest=digest(payload.model_dump(mode='json'))
    if row.status in {'APPLIED','REJECTED'}:
        if row.request_key==payload.request_key and row.request_digest==request_digest:return action_out(db,row,matter)
        raise HTTPException(409,'该建议已审核，请刷新查看结果')
    if row.status!='PROPOSED' or row.version!=payload.expected_version:raise HTTPException(409,'建议状态或版本已变化')
    existing=db.scalar(select(MatterAction).where(MatterAction.request_key==payload.request_key))
    if existing and existing.id!=row.id:raise HTTPException(409,'提交标识已用于其他行动')
    if not payload.note.strip():raise HTTPException(422,'请填写审核理由或实际处理结论')
    content=row.content;effect={};old={'status':matter.status,'version':matter.version,'title':matter.title}
    if payload.decision=='APPROVE':
        observation=db.get(MatterProgress,row.progress_id);job=db.get(MatterProgressJob,observation.job_id)
        latest=db.scalar(select(MatterProgressJob.id).where(MatterProgressJob.matter_id==matter.id).order_by(MatterProgressJob.id.desc()).limit(1))
        if matter.version!=payload.expected_matter_version or matter.version!=row.expected_matter_version or job.id!=latest or not fresh(db,job,matter):
            raise HTTPException(409,'事项或进展依据已更新，请核对当前建议，旧建议不能执行')
        owner=payload.assigned_doctor_id or matter.assigned_doctor_id or payload.doctor_id
        if not db.get(Doctor,owner):raise HTTPException(422,'负责人医生不存在')
        try:require_scope(db,matter.encounter_id,owner)
        except ValueError as exc:raise HTTPException(403,'负责人不在本次住院范围内') from exc
        title=(payload.title if payload.title is not None else content['title']).strip()
        if not title:raise HTTPException(422,'事项标题不能为空')
        due=payload.due_at.astimezone(timezone.utc) if payload.due_at else None
        if content['action']=='CREATE_FOLLOWUP':
            if db.scalar(select(MatterLink.id).where(MatterLink.child_id==matter.id)):raise HTTPException(409,'当前仅支持一层后续事项')
            for link in db.scalars(select(MatterLink).where(MatterLink.parent_id==matter.id)):
                child=db.get(PatientMatter,link.child_id);prior=db.get(MatterAction,link.action_id)
                if child.status=='OPEN' and prior.content.get('purpose')==content['purpose']:raise HTTPException(409,'已有同类后续事项，请继续处理已有任务')
            evidence=next((e for e in observation.evidence if e['id'] in content['source_ids']),None)
            if not evidence:raise HTTPException(409,'后续事项缺少当前依据')
            encounter=db.get(Encounter,matter.encounter_id);source=source_from_evidence(db,encounter,evidence)
            child=PatientMatter(matter_key=digest(('approved-followup',row.id)),scan_id=matter.scan_id,
                patient_id=matter.patient_id,encounter_id=matter.encounter_id,kind='VERIFY_INFO' if content['purpose']=='VERIFY_INFORMATION' else 'OTHER',
                title=title,quote=evidence['quote'],due_text=content.get('due_text',''),due_at=due,assigned_doctor_id=owner,
                source=source,status='OPEN',created_at=datetime.now(timezone.utc),updated_at=datetime.now(timezone.utc),reviewed_by=payload.doctor_id)
            db.add(child);db.flush()
            db.add(MatterLink(parent_id=matter.id,child_id=child.id,action_id=row.id,created_at=datetime.now(timezone.utc)))
            db.flush();enqueue(db,child)
            effect={'child_id':child.id,'parent_id':matter.id,'parent_status':'OPEN'}
        elif content['action']=='RESCHEDULE':
            if due is None:raise HTTPException(422,'请由医生选择新的计划时间')
            matter.due_at,matter.assigned_doctor_id=due,owner
            effect={'due_at':due.isoformat(),'assigned_doctor_id':owner}
        elif content['action']=='REVISE_PLAN':
            matter.title,matter.assigned_doctor_id=title,owner
            if due is not None:matter.due_at=due
            effect={'title':title,'assigned_doctor_id':owner}
        elif content['action']=='COMPLETE_MATTER':
            matter.status,matter.resolution='COMPLETED',payload.note.strip();effect={'status':'COMPLETED'}
        elif content['action']=='CANCEL_PLAN':
            matter.status,matter.resolution='CANCELLED',payload.note.strip();effect={'status':'CANCELLED'}
        matter.version+=1;matter.updated_at=datetime.now(timezone.utc);matter.reviewed_by=payload.doctor_id
        db.flush();enqueue(db,matter)
    row.status='APPLIED' if payload.decision=='APPROVE' else 'REJECTED'
    row.version+=1;row.reviewed_by=payload.doctor_id;row.reviewed_at=datetime.now(timezone.utc)
    row.note=payload.note.strip();row.request_key=payload.request_key;row.request_digest=request_digest;row.outcome=effect
    db.add(AuditLog(actor_type='DOCTOR',actor_id=str(payload.doctor_id),action='MATTER_ACTION_'+payload.decision,
        resource_type='MatterAction',resource_id=str(row.id),old_state=old,new_state={'effect':effect,
        'matter_version':matter.version,'progress_id':row.progress_id,'note':row.note},created_at=row.reviewed_at))
    db.commit();return action_out(db,row,matter)
