from datetime import datetime,timezone
from fastapi import APIRouter,Depends,HTTPException
from pydantic import BaseModel,ConfigDict,Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from backend.db import get_db
from backend.models import Patient,AuditLog
from agent.patient_memory.models import PatientMatter
from agent.patient_memory.api import scope
from .models import MatterProgressJob,MatterProgress
from .runtime import enqueue,progress_out,fresh

router=APIRouter()


def matter_scope(db,matter_id,doctor_id,lock=False):
    row=db.get(PatientMatter,matter_id)
    if not row:raise HTTPException(404,'事项不存在')
    scope(db,row.encounter_id,doctor_id,lock)
    return db.scalar(select(PatientMatter).where(PatientMatter.id==matter_id).with_for_update().execution_options(populate_existing=True)) if lock else row


class Body(BaseModel):
    model_config=ConfigDict(extra='forbid')
    doctor_id:int=Field(gt=0)
    expected_version:int=Field(ge=1)


@router.post('/matters/{matter_id}/progress/check')
def check(matter_id:int,payload:Body,db:Session=Depends(get_db)):
    row=matter_scope(db,matter_id,payload.doctor_id,True)
    if row.version!=payload.expected_version or row.status!='OPEN':raise HTTPException(409,'事项已更新或结束，请刷新')
    job=enqueue(db,row)
    if not job:raise HTTPException(409,'当前住院或负责人范围不支持继续调查')
    db.commit();return progress_out(db,row)


class Acknowledge(Body):
    result_id:int=Field(gt=0)


@router.post('/matters/{matter_id}/progress/acknowledge')
def acknowledge(matter_id:int,payload:Acknowledge,db:Session=Depends(get_db)):
    row=matter_scope(db,matter_id,payload.doctor_id,True)
    if row.version!=payload.expected_version or row.status!='OPEN':raise HTTPException(409,'事项已变化，请重新核对')
    current=progress_out(db,row)
    observation=db.get(MatterProgress,payload.result_id)
    if not observation or observation.matter_id!=row.id:raise HTTPException(404,'进展反馈不存在')
    if current.get('result_id')!=observation.id or current.get('stale'):raise HTTPException(409,'这条进展依据已过期，请读取最新反馈')
    if not observation.acknowledged_at:
        observation.acknowledged_at,observation.acknowledged_by=datetime.now(timezone.utc),payload.doctor_id
        db.add(AuditLog(actor_type='DOCTOR',actor_id=str(payload.doctor_id),action='ACK_MATTER_PROGRESS',
            resource_type='MatterProgress',resource_id=str(observation.id),new_state={'matter_id':row.id,'matter_status':row.status},created_at=observation.acknowledged_at))
    db.commit();return progress_out(db,row)


class Retry(Body):
    job_id:int=Field(gt=0)
    expected_attempt:int=Field(ge=1)


@router.post('/matters/{matter_id}/progress/retry')
def retry(matter_id:int,payload:Retry,db:Session=Depends(get_db)):
    row=matter_scope(db,matter_id,payload.doctor_id,True)
    job=db.scalar(select(MatterProgressJob).where(MatterProgressJob.id==payload.job_id).with_for_update())
    if not job or job.matter_id!=row.id:raise HTTPException(404,'跟进作业不存在')
    if row.version!=payload.expected_version or job.attempt!=payload.expected_attempt or job.status!='FAILED' or not fresh(db,job,row):
        raise HTTPException(409,'作业已变化或依据已过期，请重新检查当前资料')
    job.status,job.attempt,job.retry_at,job.error_code='PENDING',0,None,None
    db.add(AuditLog(actor_type='DOCTOR',actor_id=str(payload.doctor_id),action='RETRY_MATTER_PROGRESS',
        resource_type='MatterProgressJob',resource_id=str(job.id),new_state={'expected_attempt':payload.expected_attempt},created_at=datetime.now(timezone.utc)))
    db.commit();return progress_out(db,row)


@router.get('/matters/{matter_id}/progress')
def detail(matter_id:int,doctor_id:int=1,db:Session=Depends(get_db)):
    row=matter_scope(db,matter_id,doctor_id)
    history=db.scalars(select(MatterProgress).where(MatterProgress.matter_id==row.id).order_by(MatterProgress.id.desc()).limit(20)).all()
    return {'latest':progress_out(db,row),'history':[{'id':r.id,'state':r.state,'summary':r.summary,
        'evidence':r.evidence,'acknowledged':bool(r.acknowledged_at)} for r in history]}


@router.get('/matter-progress-feedback')
def feedback(doctor_id:int=1,db:Session=Depends(get_db)):
    items=[]
    for row in db.scalars(select(PatientMatter).where(PatientMatter.status=='OPEN').order_by(PatientMatter.id.desc())):
        try:scope(db,row.encounter_id,doctor_id)
        except HTTPException:continue
        patient=db.get(Patient,row.patient_id)
        items.append({'matter_id':row.id,'matter_version':row.version,'patient_id':row.patient_id,
            'encounter_id':row.encounter_id,'patient':patient.name,'title':row.title,'progress':progress_out(db,row)})
    return {'items':items,'attention_count':sum(bool(i['progress']['needs_attention']) for i in items)}
