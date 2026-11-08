from datetime import datetime,timezone
from sqlalchemy import select
from backend.models import Encounter,LabReport,ClinicalEvent
from backend.services.report_service import utc
from agent.patient_memory.service import source_list,digest


def build_snapshot(db,matter,now=None):
    now=now or datetime.now(timezone.utc)
    encounter=db.get(Encounter,matter.encounter_id)
    notes=[s for s in source_list(db,encounter) if utc(encounter.admitted_at)<=datetime.fromisoformat(s['at'])<=now]
    notes.sort(key=lambda s:(datetime.fromisoformat(s['at']).astimezone(timezone.utc),s['kind'],s['id']),reverse=True)
    sources=[]
    for s in notes[:30]:
        sources.append({'id':f"{s['kind']}:{s['id']}:{s['digest']}",'kind':'NOTE','label':s['label'],
            'at':s['at'],'text':s['text'],'version':s['version'],
            'is_origin':s['kind']==matter.source['kind'] and s['id']==matter.source['id'] and s['digest']==matter.source['digest']})
    events=db.scalars(select(ClinicalEvent).where(ClinicalEvent.encounter_id==encounter.id,
        ClinicalEvent.patient_id==matter.patient_id,ClinicalEvent.event_type=='PROGRESS_NOTE_CREATED')
        .order_by(ClinicalEvent.occurred_at.desc(),ClinicalEvent.id.desc()).limit(21)).all()
    for e in events[:20]:
        if str(e.payload.get('text','')).strip():
            sources.append({'id':f'PROGRESS:{e.id}','kind':'NOTE','label':f'病程更新 #{e.id}',
                'at':utc(e.occurred_at).isoformat(),'text':str(e.payload['text']),'version':1,'is_origin':matter.source['kind']=='PROGRESS_NOTE' and matter.source['id']==e.id and matter.source['text']==str(e.payload['text'])})
    reports=db.scalars(select(LabReport).where(LabReport.encounter_id==encounter.id,
        LabReport.patient_id==matter.patient_id,LabReport.reported_at>=encounter.admitted_at,LabReport.reported_at<=now).order_by(LabReport.report_version.desc(),LabReport.id.desc())).all()
    latest={}
    for r in reports:latest.setdefault(r.report_id,r)
    ordered=sorted(latest.values(),key=lambda r:(utc(r.reported_at),r.id),reverse=True)
    for r in ordered[:30]:
        sources.append({'id':f'LAB:{r.id}:v{r.report_version}','kind':'LAB','label':r.test_name,
            'at':utc(r.reported_at).isoformat(),'text':r.result_text,'version':r.report_version,
            'report_id':r.report_id,'specimen_id':r.specimen_id,'order_id':r.order_id,'is_origin':matter.source['kind']=='LAB' and matter.source['id']==r.id and matter.source['version']==r.report_version})
    # Bound the model context, and explicitly forbid absence conclusions when truncated.
    clipped=False
    for source in sources:
        if len(source['text'])>8000:source['text']=source['text'][:8000];clipped=True
    sources.sort(key=lambda s:(datetime.fromisoformat(s['at']).astimezone(timezone.utc),s['id']),reverse=True)
    budget=48000;bounded=[]
    for source in sources:
        if budget<=0:clipped=True;break
        if len(source['text'])>budget:source['text']=source['text'][:budget];clipped=True
        budget-=len(source['text']);bounded.append(source)
    sources=bounded
    from agent.patient_memory.actions.models import MatterLink,MatterAction
    from agent.patient_memory.models import PatientMatter
    parent=db.scalar(select(MatterLink).where(MatterLink.child_id==matter.id))
    children=[];purposes=[]
    for link in db.scalars(select(MatterLink).where(MatterLink.parent_id==matter.id).order_by(MatterLink.id)):
        child=db.get(PatientMatter,link.child_id);action=db.get(MatterAction,link.action_id)
        children.append({'id':child.id,'status':child.status,'version':child.version,'title':child.title})
        if child.status=='OPEN':purposes.append(action.content.get('purpose'))
    from agent.patient_memory.workflow import completion_contract
    snapshot={'policy_version':'matter-workflow-v2','matter':{'id':matter.id,'patient_id':matter.patient_id,'encounter_id':encounter.id,
        'parent_id':parent.parent_id if parent else None,'active_child_purposes':purposes,'children':children,'version':matter.version,'kind':matter.kind,'title':matter.title,'quote':matter.quote,
        'due_text':matter.due_text,'due_at':utc(matter.due_at).isoformat() if matter.due_at else None,
        'completion_contract':completion_contract(matter),
        'plan_started_at':(matter.quality or {}).get('plan_revision_at') or matter.source.get('at'),
        'clarified_plan':{k:matter.quality.get(k) for k in ('action_text','target_text','evidence_kind','clarification_note','clarified_by')} if matter.quality else {},
        'assigned_doctor_id':matter.assigned_doctor_id,'original_source':{k:matter.source.get(k) for k in ('kind','id','label','digest','at','version')}},
        'past_due':bool(matter.due_at and utc(matter.due_at)<=now),
        'coverage':{'complete':not(clipped or len(notes)>30 or len(events)>20 or len(ordered)>30),
                    'scope':'本次患者住院：最近30段问诊、20条病程更新、30份最新版本检验报告'},
        'sources':sources}
    return snapshot,digest(snapshot)
