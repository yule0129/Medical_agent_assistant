import hashlib
import json
from datetime import datetime, timedelta, timezone
from fastapi import HTTPException
from sqlalchemy import select
from backend.models import AdmissionWorkspace, AuditLog, Doctor, Encounter, Patient, RoundingSession
from backend.services.intent_service import require_scope
from backend.services.report_service import utc
from agent.llm.deepseek import configured
from agent.admission.contracts import DraftError
from .models import MatterScan, PatientMatter
from .contracts import Extraction
from .provider import get_provider


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def source_out(row, kind, encounter):
    at = encounter.recorded_at or encounter.admitted_at if kind=='ADMISSION' else row.started_at
    source={'kind':kind,'id':row.id,'encounter_id':encounter.id,
            'version':row.input_version if kind=='ADMISSION' else row.version,
            'label':'入院问诊' if kind=='ADMISSION' else f'查房 #{row.id}',
            'at':utc(at).isoformat(), 'text':row.transcript,
            'doctor_id':encounter.attending_doctor_id if kind=='ADMISSION' else row.doctor_id}
    source['digest']=digest(source)
    return source


def source_list(db, encounter):
    sources=[]
    workspace=db.scalar(select(AdmissionWorkspace).where(AdmissionWorkspace.encounter_id==encounter.id))
    if workspace and workspace.transcript.strip():
        sources.append(source_out(workspace,'ADMISSION',encounter))
    for row in db.scalars(select(RoundingSession).where(RoundingSession.encounter_id==encounter.id).order_by(RoundingSession.started_at.desc())):
        if row.transcript.strip():sources.append(source_out(row,'ROUNDING',encounter))
    return sources


def current_source(db, encounter, kind, source_id):
    if kind in {'LAB','PROGRESS_NOTE'}:
        from .actions.service import extra_source
        return extra_source(db,encounter,kind,source_id)
    model=AdmissionWorkspace if kind=='ADMISSION' else RoundingSession
    row=db.get(model,source_id)
    if not row or row.encounter_id!=encounter.id or not row.transcript.strip():
        raise HTTPException(404,'该次住院的问诊来源不存在或内容为空')
    return source_out(row,kind,encounter)


def item_out(db, row, encounter=None):
    encounter=encounter or db.get(Encounter,row.encounter_id)
    try: changed=current_source(db,encounter,row.source['kind'],row.source['id'])['digest']!=row.source['digest']
    except HTTPException: changed=True
    owner=db.get(Doctor,row.assigned_doctor_id) if row.assigned_doctor_id else None
    from .progress.runtime import progress_out
    from .actions.models import MatterLink
    parent_link=db.scalar(select(MatterLink).where(MatterLink.child_id==row.id))
    children=[{'id':c.id,'title':c.title,'status':c.status} for link in db.scalars(select(MatterLink).where(MatterLink.parent_id==row.id)) if (c:=db.get(PatientMatter,link.child_id))]
    return {'id':row.id,'patient_id':row.patient_id,'encounter_id':row.encounter_id,
        'kind':row.kind,'title':row.title,'quote':row.quote,'due_text':row.due_text,
        'due_at':utc(row.due_at).isoformat() if row.due_at else None,
        'assigned_doctor_id':row.assigned_doctor_id,'assigned_doctor_name':owner.name if owner else None,
        'status':row.status,'version':row.version,'parent_id':parent_link.parent_id if parent_link else None,'children':children,'source':row.source,'source_changed':changed,'progress':progress_out(db,row),
        'resolution':row.resolution,'created_at':utc(row.created_at).isoformat(),
        'updated_at':utc(row.updated_at).isoformat()}


def extract_matters(db, encounter, doctor_id, kind, source_id, expected_digest):
    source=current_source(db,encounter,kind,source_id)
    if source['digest']!=expected_digest:
        raise HTTPException(409,'原文已更新，请刷新后核对再识别')
    if len(source['text'])>100000:
        raise HTTPException(422,'问诊原文过长，请分次记录后识别')
    key=digest((encounter.id,kind,source_id,source['digest']))
    scan=db.scalar(select(MatterScan).where(MatterScan.scan_key==key))
    now=datetime.now(timezone.utc)
    if scan and scan.status=='READY':
        return {'scan_id':scan.id,'status':'READY','cached':True,
                'candidate_count':db.query(PatientMatter).filter(PatientMatter.scan_id==scan.id).count()}
    if scan and scan.status=='RUNNING' and utc(scan.created_at)+timedelta(seconds=90)>now:
        raise HTTPException(409,'本段原文正在识别，请稍候刷新')
    if not configured():raise HTTPException(503,'模型未配置或Agent未启用，原文已保留')
    if not scan:
        scan=MatterScan(scan_key=key,encounter_id=encounter.id,doctor_id=doctor_id,source_kind=kind,
                        source_id=source_id,source_digest=source['digest'],status='RUNNING',created_at=now)
        db.add(scan);db.flush()
    else:scan.status,scan.created_at,scan.error_code='RUNNING',now,None
    scan.attempt=(scan.attempt or 0)+1
    scan.retry_at=None
    known=[{'id':r.id,'title':r.title,'kind':r.kind,'status':r.status} for r in db.scalars(
        select(PatientMatter).where(PatientMatter.encounter_id==encounter.id,PatientMatter.status.in_(['OPEN','COMPLETED'])))]
    scan_id,encounter_id,patient_id=scan.id,encounter.id,encounter.patient_id
    db.commit()  # No DB locks during the bounded model request.
    try:
        provider=get_provider()
        extracted=Extraction.model_validate(provider.extract(source,known))
        for proposal in extracted.items:
            if proposal.quote not in source['text'] or (proposal.due_text and proposal.due_text not in proposal.quote):
                raise DraftError('UNGROUNDED_SOURCE_QUOTE')
    except Exception as exc:
        code=str(exc)[:100] if isinstance(exc,DraftError) else 'MODEL_FAILED'
        db.expire_all()
        db.scalar(select(Encounter).where(Encounter.id==encounter_id).with_for_update())
        scan=db.get(MatterScan,scan_id)
        if utc(scan.created_at)==now:
            scan.status,scan.error_code,scan.finished_at='FAILED',code,datetime.now(timezone.utc)
            scan.retry_at=scan.finished_at+timedelta(seconds=30*scan.attempt) if scan.attempt<3 else None
            db.commit()
        raise HTTPException(502,'Agent事项识别失败，原文已保留：'+code) from None
    db.expire_all()
    encounter=db.scalar(select(Encounter).where(Encounter.id==encounter_id).with_for_update())
    scan=db.get(MatterScan,scan_id)
    try: require_scope(db,encounter_id,doctor_id)
    except ValueError as exc:raise HTTPException(403,'医生范围已变化，请重新加载') from exc
    fresh=current_source(db,encounter,kind,source_id)
    if fresh['digest']!=source['digest'] or utc(scan.created_at)!=now or encounter.status!='INPATIENT':
        if utc(scan.created_at)==now:
            scan.status,scan.error_code='STALE','SOURCE_CHANGED';db.commit()
        raise HTTPException(409,'识别期间原文已变化，旧候选未纳入，请刷新后重试')
    added_keys=set()
    for proposal in extracted.items:
        matter_key=digest((key,proposal.kind,proposal.quote,proposal.due_text))
        if matter_key in added_keys:continue
        if db.scalar(select(PatientMatter.id).where(PatientMatter.matter_key==matter_key)):continue
        added_keys.add(matter_key)
        db.add(PatientMatter(matter_key=matter_key,scan_id=scan_id,patient_id=patient_id,encounter_id=encounter_id,
            kind=proposal.kind,title=proposal.title,quote=proposal.quote,due_text=proposal.due_text,
            source=source,status='CANDIDATE',created_at=now,updated_at=now))
    scan.status,scan.model,scan.finished_at='READY',provider.model,datetime.now(timezone.utc)
    db.add(AuditLog(actor_type='AGENT',actor_id='patient-memory',action='EXTRACT_PATIENT_MATTERS',
        resource_type='MatterScan',resource_id=str(scan_id),new_state={'source_digest':source['digest'],
        'candidate_count':len(added_keys),'model':provider.model},created_at=scan.finished_at))
    db.commit()
    return {'scan_id':scan_id,'status':'READY','cached':False,'candidate_count':len(added_keys)}
