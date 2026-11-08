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
from .quality import for_item,compact,VAGUE_TARGETS
from .workflow import completion_contract,revise_plan_metadata

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
    from .workflow import work_summary,feedback_summary
    items=[service.item_out(db,r,encounter) for r in rows]
    return {'items':items,'work_summary':work_summary(items),'quality_feedback':feedback_summary(db,rows),
        'sources':service.source_list(db,encounter),'model_configured':service.configured(),
        'owners':[{'id':d.id,'name':d.name} for d in db.scalars(select(Doctor))
                  if d.id in {db.get(Patient,encounter.patient_id).doctor_id,encounter.attending_doctor_id}],
        'scans':[{'id':s.id,'source_kind':s.source_kind,'source_id':s.source_id,'status':s.status,'error_code':s.error_code,
                  'quality_report':s.quality_report,'created_at':utc(s.created_at).isoformat()} for s in db.scalars(select(MatterScan).where(
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
    action_text:str|None=Field(default=None,max_length=80)
    target_text:str|None=Field(default=None,max_length=200)
    evidence_kind:Literal['LAB_REPORT','VERIFICATION_RECORD','CONTACT_RECORD','CLINICIAN_NOTE']|None=None
    clarification_note:str=Field(default='',max_length=5000)
    duplicate_decision:Literal['SEPARATE','LINK']|None=None
    duplicate_matter_id:int|None=Field(default=None,gt=0)
    duplicate_expected_version:int|None=Field(default=None,ge=1)
    reviewed_result_id:int|None=Field(default=None,gt=0)

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
    quality=for_item(row)
    completion_evidence=None
    if payload.action=='COMPLETE':
        from .progress.runtime import progress_out
        from .progress.models import MatterProgress
        current=progress_out(db,row)
        if payload.reviewed_result_id is not None:
            if current.get('result_id')!=payload.reviewed_result_id or current.get('stale') or current.get('status')!='SUCCEEDED':
                raise HTTPException(409,'报告或进展已更新，请重新核对当前资料')
        if current.get('state')=='RESULT_AVAILABLE' and not current.get('stale'):
            if not current.get('acknowledged') and payload.reviewed_result_id!=current.get('result_id'):
                raise HTTPException(422,'请先审阅当前报告，并确认已审阅后记录处理结论')
            observation=db.get(MatterProgress,current['result_id'])
            # Completing records the explicit review, without equating review alone to completion.
            if payload.note.strip() and not observation.acknowledged_at:
                observation.acknowledged_at,observation.acknowledged_by=datetime.now(timezone.utc),payload.doctor_id
        completion_evidence={
            'result_id':current.get('result_id') if not current.get('stale') else None,
            'evidence':current.get('evidence',[]) if not current.get('stale') else [],
            'basis':'CURRENT_PROGRESS' if not current.get('stale') and current.get('status')=='SUCCEEDED' and current.get('state') in {'RESPONSE_RECORDED','RESULT_AVAILABLE'} else 'DOCTOR_RECORDED',
            'contract':completion_contract(row)}
    if payload.action=='CONFIRM':
        if service.item_out(db,row,encounter)['source_changed']:
            raise HTTPException(409,'候选依据已变化，请重新识别当前原文')
        duplicates=service.duplicate_choices(db,row)
        if duplicates and not payload.duplicate_decision:
            raise HTTPException(422,'此事项可能重复，请核对后关联已有事项，或说明为何是独立安排')
        if payload.duplicate_decision:
            if not payload.note.strip():raise HTTPException(422,'请填写重复核对的理由')
            quality={**quality,'duplicate_decision':payload.duplicate_decision,'duplicate_note':payload.note.strip()}
        if payload.duplicate_decision=='LINK':
            choice=next((d for d in duplicates if d['id']==payload.duplicate_matter_id),None)
            if not choice:raise HTTPException(409,'关联对象已变化或不属于本次住院，请刷新核对')
            if choice['version']!=payload.duplicate_expected_version:raise HTTPException(409,'关联对象版本已变化，请重新核对')
            row.quality={**quality,'linked_matter_id':choice['id'],'linked_matter_version':choice['version']}
            row.status,row.version,row.reviewed_by,row.updated_at='DISMISSED',row.version+1,payload.doctor_id,datetime.now(timezone.utc)
            row.resolution=payload.note.strip()
            db.add(AuditLog(actor_type='DOCTOR',actor_id=str(payload.doctor_id),action='MATTER_LINK_EXISTING',
                resource_type='PatientMatter',resource_id=str(row.id),old_state=old,
                new_state={'linked_matter_id':choice['id'],'linked_version':choice['version'],'note':row.resolution},created_at=row.updated_at))
            db.add(AuditLog(actor_type='DOCTOR',actor_id=str(payload.doctor_id),action='MATTER_QUALITY_FEEDBACK',
                resource_type='PatientMatter',resource_id=str(row.id),
                new_state={'reason':'DUPLICATE','note':row.resolution,'reason_origin':'operation_default',
                           'linked_matter_id':choice['id'],'policy_version':quality.get('policy_version','legacy')},created_at=row.updated_at))
            db.commit();return service.item_out(db,row,encounter)
        if quality.get('admission')=='SKIP':raise HTTPException(422,'该候选不符合提取准入要求，请核对后忽略；明确的新计划请记录到问诊原文')
        if quality.get('admission')=='CLARIFY':
            action_text=(payload.action_text or '').strip();target_text=(payload.target_text or '').strip()
            if not action_text or not target_text or not payload.evidence_kind or not payload.clarification_note.strip():
                raise HTTPException(422,'请补充具体动作、对象、核对资料类型及实际安排说明，再确认')
            if compact(target_text) in VAGUE_TARGETS:raise HTTPException(422,'对象仍过于笼统，请明确具体检查项目或执行对象')
            if not payload.title or compact(action_text) not in compact(payload.title) or compact(target_text) not in compact(payload.title):
                raise HTTPException(422,'请同步修改事项标题，明确补充后的动作和对象')
            quality={**quality,'admission':'KEEP','action_text':action_text,'target_text':target_text,
                'evidence_kind':payload.evidence_kind,'clarified_by':payload.doctor_id,
                'clarification_note':payload.clarification_note.strip(),'original_admission':'CLARIFY'}
        row.quality=quality
    if payload.action in {'CONFIRM','UPDATE'}:
        if payload.action=='CONFIRM' and service.item_out(db,row,encounter)['source_changed']:
            raise HTTPException(409,'候选依据已变化，请重新识别当前原文')
        owner=payload.assigned_doctor_id or (row.assigned_doctor_id if payload.action=='UPDATE' else payload.doctor_id)
        if not db.get(Doctor,owner):raise HTTPException(422,'负责人医生不存在')
        try:require_scope(db,encounter.id,owner)
        except ValueError as exc:raise HTTPException(403,'负责人不在本次住院医生范围内') from exc
        if payload.title is not None:
            if not payload.title.strip():raise HTTPException(422,'事项标题不能为空')
            if payload.action=='UPDATE':revise_plan_metadata(row,payload.title.strip(),payload.note)
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
        new_state={'status':row.status,'version':row.version,'title':row.title,'note':payload.note,'quality':row.quality,
                   'assigned_doctor_id':row.assigned_doctor_id,'due_at':row.due_at.isoformat() if row.due_at else None,
                   'completion_evidence':completion_evidence},created_at=row.updated_at))
    if payload.action in {'DISMISS','UPDATE'} or (payload.action=='CONFIRM' and (quality.get('clarified_by') or payload.title and payload.title.strip()!=old['title'])):
        reason='WRONG_EXTRACTION' if payload.action=='DISMISS' else 'PLAN_CHANGED' if payload.action=='UPDATE' else 'MISSING_CONTEXT'
        db.add(AuditLog(actor_type='DOCTOR',actor_id=str(payload.doctor_id),action='MATTER_QUALITY_FEEDBACK',
            resource_type='PatientMatter',resource_id=str(row.id),old_state=old,
            new_state={'reason':reason,'reason_origin':'operation_default','note':payload.note or payload.clarification_note,
                'before_title':old['title'],'after_title':row.title,'policy_version':quality.get('policy_version','legacy')},created_at=row.updated_at))
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


class QualityFeedbackRequest(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
    doctor_id:int=Field(gt=0)
    expected_version:int=Field(ge=1)
    reason:Literal['WRONG_EXTRACTION','MISSING_CONTEXT','DUPLICATE','WRONG_EVIDENCE','PLAN_CHANGED','OTHER']
    note:str=Field(min_length=1,max_length=5000)


@router.post('/matters/{matter_id}/quality-feedback')
def quality_feedback(matter_id:int,payload:QualityFeedbackRequest,db:Session=Depends(get_db)):
    row=db.get(PatientMatter,matter_id)
    if not row:raise HTTPException(404,'事项不存在')
    scope(db,row.encounter_id,payload.doctor_id,True)
    db.refresh(row)
    if row.version!=payload.expected_version:raise HTTPException(409,'事项已变化，请刷新后反馈')
    state={'reason':payload.reason,'note':payload.note,'matter_version':row.version,
           'title':row.title,'quote':row.quote,'quality':row.quality,
           'policy_version':(row.quality or {}).get('policy_version','legacy')}
    previous=db.scalar(select(AuditLog).where(AuditLog.resource_type=='PatientMatter',
        AuditLog.resource_id==str(row.id),AuditLog.action=='MATTER_QUALITY_FEEDBACK',
        AuditLog.actor_id==str(payload.doctor_id)).order_by(AuditLog.id.desc()).limit(1))
    if not previous or previous.new_state!=state:
        db.add(AuditLog(actor_type='DOCTOR',actor_id=str(payload.doctor_id),
            action='MATTER_QUALITY_FEEDBACK',resource_type='PatientMatter',resource_id=str(row.id),
            new_state=state,created_at=datetime.now(timezone.utc)))
        db.commit()
    return {'saved':True,'matter_status':row.status}


@router.get('/matters/{matter_id}/timeline')
def matter_timeline(matter_id:int,doctor_id:int=1,db:Session=Depends(get_db)):
    row=db.get(PatientMatter,matter_id)
    if not row:raise HTTPException(404,'事项不存在')
    scope(db,row.encounter_id,doctor_id)
    from .workflow import timeline
    return {'matter_id':row.id,'items':timeline(db,row)}
