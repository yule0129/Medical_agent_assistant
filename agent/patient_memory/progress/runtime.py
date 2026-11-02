from datetime import datetime,timedelta,timezone
from uuid import uuid4
import re
from sqlalchemy import select,or_,and_
from backend.models import AuditLog,Encounter,Patient
from backend.services.intent_service import require_scope
from backend.services.report_service import utc
from agent.admission.contracts import DraftError
from agent.llm.deepseek import configured
from agent.patient_memory.models import PatientMatter
from agent.patient_memory.service import digest
from .models import MatterProgressJob,MatterProgress
from .sources import build_snapshot
from .contracts import Observation
from .provider import get_provider


def enqueue(db,matter,now=None):
    if matter.status!='OPEN':return None
    encounter=db.get(Encounter,matter.encounter_id)
    if encounter.status!='INPATIENT':return None
    doctor_id=matter.assigned_doctor_id or matter.reviewed_by
    try:require_scope(db,encounter.id,doctor_id)
    except ValueError:return None
    snapshot,key=build_snapshot(db,matter,now)
    job_key=digest((matter.id,matter.version,key))
    row=db.scalar(select(MatterProgressJob).where(MatterProgressJob.job_key==job_key))
    if row:return row
    row=MatterProgressJob(job_key=job_key,matter_id=matter.id,matter_version=matter.version,
        doctor_id=doctor_id,snapshot_digest=key,snapshot=snapshot,status='PENDING',created_at=now or datetime.now(timezone.utc))
    db.add(row);db.flush();return row


def schedule(db,now=None):
    # A source/version sweep catches bedside saves and report imports, even if an API process restarts.
    count=0
    for matter in db.scalars(select(PatientMatter).where(PatientMatter.status=='OPEN').order_by(PatientMatter.id)
                              .with_for_update(skip_locked=True)):
        job=enqueue(db,matter,now)
        if job and job.status=='PENDING':count+=1
    db.commit();return count


def validate(snapshot,observation):
    sources={s['id']:s for s in snapshot['sources']}
    evidence=[]
    for reference in observation.references:
        source=sources.get(reference.source_id)
        if not source or reference.quote not in source['text']:raise DraftError('UNGROUNDED_PROGRESS_REFERENCE')
        evidence.append({**source,'quote':reference.quote})
    if observation.state in {'RESPONSE_RECORDED','RESULT_AVAILABLE','CONFLICT','PLAN_CHANGED'} and not evidence:
        raise DraftError('PROGRESS_EVIDENCE_REQUIRED')
    if observation.state=='RESULT_AVAILABLE' and not any(e['kind']=='LAB' for e in evidence):
        raise DraftError('LAB_EVIDENCE_REQUIRED')
    if observation.state=='RESPONSE_RECORDED' and not any(e['kind']=='NOTE' and not e['is_origin'] for e in evidence):
        raise DraftError('ORIGINAL_PLAN_IS_NOT_RESPONSE')
    if observation.state in {'PLAN_CHANGED','CONFLICT'} and not any(not e['is_origin'] for e in evidence):
        raise DraftError('NEW_EVIDENCE_REQUIRED')
    if observation.state=='RESULT_AVAILABLE':
        plan=snapshot['matter'];start=plan.get('original_source',{}).get('at')
        compact=lambda value:re.sub(r'[\W_]+','',value).replace('检查','')
        target=compact(plan['title']+plan['quote'])
        eligible=[s for s in snapshot['sources'] if s['kind']=='LAB' and not s['is_origin'] and compact(s['label']) and compact(s['label']) in target
                  and start and datetime.fromisoformat(s['at'])>=datetime.fromisoformat(start)]
        cited=[e for e in evidence if e['kind']=='LAB']
        if plan['kind'] not in {'RECHECK','OTHER'} or len(eligible)!=1 or not all(e['id']==eligible[0]['id'] for e in cited):
            observation.state='UNCLEAR'
            observation.summary='发现检验资料，但无法按明确检查名称、时间及唯一来源对应原计划，请医生核对。'
    if observation.state=='WAITING' and not snapshot['coverage']['complete']:
        raise DraftError('INCOMPLETE_COVERAGE_REQUIRES_UNCLEAR')
    return evidence


def fresh(db,job,matter):
    if not matter or matter.status!='OPEN' or matter.version!=job.matter_version:return False
    encounter=db.get(Encounter,matter.encounter_id)
    if encounter.status!='INPATIENT':return False
    try:require_scope(db,encounter.id,job.doctor_id)
    except ValueError:return False
    return build_snapshot(db,matter)[1]==job.snapshot_digest


def process_once(db,provider=None):
    now=datetime.now(timezone.utc)
    # A crashed final attempt must leave an explicit failure, not a permanent RUNNING badge.
    for expired in db.scalars(select(MatterProgressJob).where(MatterProgressJob.status=='RUNNING',
        MatterProgressJob.lease_until<=now,MatterProgressJob.attempt>=3).with_for_update(skip_locked=True)):
        expired.status,expired.error_code,expired.lease_token,expired.lease_until='FAILED','LEASE_EXPIRED',None,None
    for skipped in range(50):
        job=db.scalar(select(MatterProgressJob).where(or_(MatterProgressJob.status=='PENDING',
            and_(MatterProgressJob.status=='FAILED',MatterProgressJob.attempt<3,MatterProgressJob.retry_at<=now),
            and_(MatterProgressJob.status=='RUNNING',MatterProgressJob.attempt<3,MatterProgressJob.lease_until<=now)))
            .order_by(MatterProgressJob.id).with_for_update(skip_locked=True).limit(1))
        if not job:db.commit();return None
        matter=db.get(PatientMatter,job.matter_id)
        if fresh(db,job,matter):break
        job.status='STALE';db.commit()
    else:return None
    token=uuid4().hex
    job.status,job.attempt,job.lease_token,job.lease_until='RUNNING',job.attempt+1,token,now+timedelta(seconds=100)
    job.retry_at,job.error_code=None,None
    job_id,snapshot=job.id,job.snapshot
    db.commit()  # External request runs without holding clinical database locks.
    try:
        if provider is None and not configured():raise DraftError('DEEPSEEK_NOT_CONFIGURED')
        model=provider or get_provider()
        observation=Observation.model_validate(model.observe(snapshot))
        evidence=validate(snapshot,observation)
        error=None
    except Exception as exc:
        error=str(exc)[:100] if isinstance(exc,DraftError) else 'MODEL_FAILED'
    db.expire_all()
    job=db.get(MatterProgressJob,job_id)
    matter=db.scalar(select(PatientMatter).where(PatientMatter.id==job.matter_id).with_for_update())
    job=db.scalar(select(MatterProgressJob).where(MatterProgressJob.id==job_id).with_for_update().execution_options(populate_existing=True))
    if job.lease_token!=token:return {'job_id':job_id,'status':'LEASE_LOST'}
    job.lease_token,job.lease_until=None,None
    if not fresh(db,job,matter):
        job.status,job.error_code='STALE','SOURCE_OR_MATTER_CHANGED'
    elif error:
        job.status,job.error_code='FAILED',error
        job.retry_at=datetime.now(timezone.utc)+timedelta(seconds=30*job.attempt) if job.attempt<3 else None
    else:
        progress=MatterProgress(job_id=job.id,matter_id=matter.id,state=observation.state,summary=observation.summary,
            evidence=evidence,model=model.model,created_at=datetime.now(timezone.utc))
        db.add(progress);db.flush()
        from agent.patient_memory.actions.service import persist_actions
        persist_actions(db,matter,progress,observation,snapshot)
        job.status,job.finished_at='SUCCEEDED',datetime.now(timezone.utc)
        db.add(AuditLog(actor_type='AGENT',actor_id='matter-progress',action='OBSERVE_MATTER_PROGRESS',
            resource_type='PatientMatter',resource_id=str(matter.id),new_state={'job_id':job.id,
            'state':observation.state,'matter_version':matter.version,'snapshot_digest':job.snapshot_digest},created_at=job.finished_at))
    db.commit();return {'job_id':job_id,'status':job.status,'error_code':job.error_code}


def progress_out(db,matter):
    job=db.scalar(select(MatterProgressJob).where(MatterProgressJob.matter_id==matter.id).order_by(MatterProgressJob.id.desc()).limit(1))
    if not job:return {'status':'NOT_SCHEDULED','needs_attention':False}
    observation=db.scalar(select(MatterProgress).where(MatterProgress.job_id==job.id))
    stale=not fresh(db,job,matter)
    status='STALE' if stale else job.status
    if status=='RUNNING' and job.lease_until and utc(job.lease_until)<datetime.now(timezone.utc):status='RECOVERING'
    from agent.patient_memory.actions.models import MatterAction
    from agent.patient_memory.actions.service import action_out
    all_actions=[action_out(db,a,matter,job.id) for a in db.scalars(select(MatterAction).where(MatterAction.matter_id==matter.id).order_by(MatterAction.id.desc()).limit(20))]
    pending_action=any(a['status']=='PROPOSED' and not a['stale'] for a in all_actions)
    attention=bool(pending_action or not stale and ((observation and observation.state!='WAITING' and observation.acknowledged_at is None) or job.status=='FAILED'))
    return {'job_id':job.id,'attempt':job.attempt,'status':status,'error_code':job.error_code,
        'retry_at':utc(job.retry_at).isoformat() if job.retry_at else None,
        'result_id':observation.id if observation else None,'state':observation.state if observation else None,
        'summary':observation.summary if observation else '',
        'evidence':observation.evidence if observation else [],'stale':stale,'needs_attention':attention,
        'acknowledged':bool(observation and observation.acknowledged_at),'actions':all_actions,
        'coverage':job.snapshot['coverage'],'created_at':utc(job.created_at).isoformat()}
