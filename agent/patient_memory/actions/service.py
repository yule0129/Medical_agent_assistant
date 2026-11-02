from datetime import datetime,timezone
from sqlalchemy import select
from backend.models import LabReport,ClinicalEvent
from agent.patient_memory.models import PatientMatter
from agent.patient_memory.service import digest
from backend.services.report_service import utc
from .models import MatterAction,MatterLink


def validation_error(candidate,observation,snapshot):
    refs={e.source_id:e.quote for e in observation.references}
    sources={s['id']:s for s in snapshot['sources']}
    selected=[sources[s] for s in candidate.source_ids if s in sources]
    if not set(candidate.source_ids)<=set(refs):return 'ACTION_REFERENCE_NOT_IN_OBSERVATION'
    if candidate.due_text and not any(candidate.due_text in refs[s] for s in candidate.source_ids):return 'ACTION_TIME_NOT_GROUNDED'
    if not candidate.source_ids and not(candidate.action=='RESCHEDULE' and observation.state=='WAITING' and snapshot['past_due']):return 'ACTION_EVIDENCE_REQUIRED'
    if candidate.action!='CREATE_FOLLOWUP' and candidate.purpose is not None:return 'UNEXPECTED_ACTION_PURPOSE'
    fresh_note=any(s['kind']=='NOTE' and not s['is_origin'] for s in selected)
    if candidate.action=='CREATE_FOLLOWUP':
        if snapshot['matter'].get('parent_id'):return 'FOLLOWUP_DEPTH_LIMIT'
        if candidate.purpose in snapshot['matter'].get('active_child_purposes',[]):return 'FOLLOWUP_ALREADY_OPEN'
        allowed={'RESULT_AVAILABLE':{'REVIEW_RESULT'},'CONFLICT':{'VERIFY_INFORMATION'},
                 'UNCLEAR':{'VERIFY_INFORMATION'},'RESPONSE_RECORDED':{'RECORD_RESPONSE'}}
        if candidate.purpose not in allowed.get(observation.state,set()):return 'FOLLOWUP_PURPOSE_NOT_SUPPORTED'
        if candidate.purpose=='REVIEW_RESULT' and not any(s['kind']=='LAB' for s in selected):return 'FOLLOWUP_RESULT_REQUIRED'
    elif candidate.action=='RESCHEDULE':
        if not(observation.state=='WAITING' and snapshot['past_due']) and not(observation.state=='PLAN_CHANGED' and fresh_note):return 'RESCHEDULE_NOT_GROUNDED'
    elif candidate.action=='REVISE_PLAN':
        if observation.state!='PLAN_CHANGED' or not fresh_note:return 'PLAN_CHANGE_EVIDENCE_REQUIRED'
    elif candidate.action=='COMPLETE_MATTER':
        if observation.state!='RESPONSE_RECORDED' or not fresh_note:return 'COMPLETION_RESPONSE_REQUIRED'
    elif candidate.action=='CANCEL_PLAN':
        if observation.state!='PLAN_CHANGED' or not fresh_note or not any(word in refs[s] for s in candidate.source_ids for word in ('取消','暂不','停止')):return 'CANCELLATION_EVIDENCE_REQUIRED'
    return None


def persist_actions(db,matter,progress,observation,snapshot):
    seen=set()
    for candidate in observation.actions:
        key=digest((progress.id,candidate.action,candidate.purpose,candidate.title,candidate.source_ids))
        if key in seen:continue
        seen.add(key)
        content=candidate.model_dump()
        reason=validation_error(candidate,observation,snapshot)
        if reason:content['blocked_reason']=reason
        db.add(MatterAction(action_key=key,matter_id=matter.id,progress_id=progress.id,
            expected_matter_version=matter.version,content=content,status='BLOCKED' if reason else 'PROPOSED',
            created_at=datetime.now(timezone.utc)))


def action_out(db,row,matter=None,current_job=None):
    from agent.patient_memory.progress.models import MatterProgress,MatterProgressJob
    from agent.patient_memory.progress.runtime import fresh
    matter=matter or db.get(PatientMatter,row.matter_id)
    observation=db.get(MatterProgress,row.progress_id)
    job=db.get(MatterProgressJob,observation.job_id)
    latest=db.scalar(select(MatterProgressJob.id).where(MatterProgressJob.matter_id==matter.id).order_by(MatterProgressJob.id.desc()).limit(1)) if current_job is None else current_job
    stale=row.status=='PROPOSED' and (job.id!=latest or not fresh(db,job,matter))
    return {'id':row.id,'matter_id':row.matter_id,'version':row.version,'expected_matter_version':row.expected_matter_version,
        'progress_id':row.progress_id,'status':row.status,'stale':stale,'content':row.content,
        'note':row.note,'outcome':row.outcome,'reviewed_by':row.reviewed_by,
        'evidence':[e for e in observation.evidence if e['id'] in row.content['source_ids']]}


def source_from_evidence(db,encounter,evidence):
    from agent.patient_memory.service import current_source
    parts=evidence['id'].split(':')
    kind={'PROGRESS':'PROGRESS_NOTE'}.get(parts[0],parts[0])
    return current_source(db,encounter,kind,int(parts[1]))


def extra_source(db,encounter,kind,source_id):
    """Canonical first-layer provenance for an approved follow-up based on a report/event."""
    from fastapi import HTTPException
    if kind=='LAB':
        row=db.get(LabReport,source_id)
        if not row or row.encounter_id!=encounter.id or row.patient_id!=encounter.patient_id:raise HTTPException(404,'报告来源不属于本次住院')
        row=db.scalar(select(LabReport).where(LabReport.encounter_id==encounter.id,LabReport.patient_id==encounter.patient_id,
            LabReport.report_id==row.report_id).order_by(LabReport.report_version.desc()).limit(1))
        source={'kind':'LAB','id':row.id,'encounter_id':encounter.id,'version':row.report_version,
                'label':row.test_name,'at':utc(row.reported_at).isoformat(),'text':row.result_text,'doctor_id':None}
    else:
        row=db.get(ClinicalEvent,source_id)
        if not row or row.encounter_id!=encounter.id or row.patient_id!=encounter.patient_id or row.event_type!='PROGRESS_NOTE_CREATED':raise HTTPException(404,'病程来源不属于本次住院')
        source={'kind':'PROGRESS_NOTE','id':row.id,'encounter_id':encounter.id,'version':1,
                'label':f'病程更新 #{row.id}','at':utc(row.occurred_at).isoformat(),'text':str(row.payload.get('text','')),
                'doctor_id':row.payload.get('doctor_id')}
    source['digest']=digest(source)
    return source
