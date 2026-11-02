from datetime import datetime,timedelta,timezone
import pytest
from sqlalchemy import select,func
from sqlalchemy.orm import Session
from test_patient_matters import client,model,add_source,list_data,scan,action
from backend.models import RoundingSession,Encounter,ClinicalEvent,LabReport
from agent.patient_memory.models import PatientMatter
from agent.patient_memory.progress.models import MatterProgressJob,MatterProgress
from agent.patient_memory.progress import runtime
from agent.patient_memory.progress.contracts import Observation
from agent.admission.contracts import DraftError


class ProgressTestModel:
    model='injected-progress-test'
    calls=0
    def observe(self,snapshot):
        self.calls+=1
        notes=[s for s in snapshot['sources'] if s['kind']=='NOTE' and not s['is_origin']]
        labs=[s for s in snapshot['sources'] if s['kind']=='LAB' and s['label']=='血常规']
        changed=next((s for s in notes if '改为' in s['text']),None)
        response=next((s for s in notes if '已向女儿核实' in s['text']),None)
        state,source='WAITING',None
        if changed:state,source='PLAN_CHANGED',changed
        elif snapshot['matter']['kind']=='VERIFY_INFO' and response:state,source='RESPONSE_RECORDED',response
        elif snapshot['matter']['kind']=='RECHECK' and labs:state,source='RESULT_AVAILABLE',labs[0]
        return Observation(state=state,summary='仅在本次读取资料中：'+state,
            references=[dict(source_id=source['id'],quote=source['text'])] if source else [])


def setup_matter(http,engine,kind='VERIFY_INFO'):
    add_source(engine);scan(http,list_data(http)['sources'][0])
    item=next(i for i in list_data(http)['items'] if i['kind']==kind)
    response=action(http,item,'CONFIRM');assert response.status_code==200,response.text
    return response.json()


def add_note(engine,text,encounter_id=1):
    with Session(engine) as db:
        db.add(RoundingSession(encounter_id=encounter_id,doctor_id=1,started_at=datetime.now(timezone.utc),
            transcript=text,record_mode='TRANSCRIPT',status='DRAFT'));db.commit()


def run(engine,provider):
    with Session(engine,autoflush=False) as db:
        runtime.schedule(db);return runtime.process_once(db,provider)


def progress(http,item):
    return http.get(f"/api/agent/matters/{item['id']}/progress").json()['latest']


def test_new_rounding_auto_resumes_and_acknowledge_does_not_complete(client,model):
    http,engine=client;item=setup_matter(http,engine);provider=ProgressTestModel()
    assert run(engine,provider)['status']=='SUCCEEDED'
    assert progress(http,item)['state']=='WAITING'
    assert run(engine,provider) is None and provider.calls==1
    add_note(engine,'医生：已向女儿核实药物过敏史，记录具体情况。')
    assert run(engine,provider)['status']=='SUCCEEDED'
    latest=progress(http,item)
    assert latest['state']=='RESPONSE_RECORDED' and latest['needs_attention']
    assert latest['evidence'][0]['quote'].startswith('医生：已向女儿核实')
    response=http.post(f"/api/agent/matters/{item['id']}/progress/acknowledge",json={
        'doctor_id':1,'expected_version':item['version'],'result_id':latest['result_id']})
    assert response.status_code==200,response.text
    assert response.json()['acknowledged'] and not response.json()['needs_attention']
    assert next(i for i in list_data(http)['items'] if i['id']==item['id'])['status']=='OPEN'
    assert http.get('/api/agent/matter-progress-feedback?doctor_id=2').status_code==200
    assert http.get('/api/agent/matter-progress-feedback?doctor_id=2').json()['items']==[]
    assert http.get(f"/api/agent/matters/{item['id']}/progress?doctor_id=2").status_code==403


def lab(engine,version=1,encounter_id=1,text='合成血常规结果'):
    now=datetime.now(timezone.utc)
    with Session(engine) as db:
        event=ClinicalEvent(event_key=f'progress-lab-{encounter_id}-{version}',event_type='LAB_RESULT_CREATED',
            patient_id=1,encounter_id=encounter_id,occurred_at=now,received_at=now,source='synthetic',payload={})
        db.add(event);db.flush()
        db.add(LabReport(event_id=event.id,patient_id=1,encounter_id=encounter_id,report_id='TEST-CBC',report_version=version,
            test_name='血常规',result_text=text,reported_at=now));db.commit()


def test_latest_report_version_invalidates_old_feedback_and_other_admission_is_excluded(client,model):
    http,engine=client;item=setup_matter(http,engine,'RECHECK');provider=ProgressTestModel()
    with Session(engine) as db:
        db.add(Encounter(id=2,encounter_number='OTHER',patient_id=1,attending_doctor_id=1,admitted_at=datetime.now(timezone.utc)));db.commit()
    lab(engine,encounter_id=2)
    run(engine,provider);assert progress(http,item)['state']=='WAITING'
    lab(engine,1);run(engine,provider);old=progress(http,item)
    assert old['state']=='RESULT_AVAILABLE'
    lab(engine,2,text='修订版本')
    assert progress(http,item)['stale']
    ack=http.post(f"/api/agent/matters/{item['id']}/progress/acknowledge",json={
        'doctor_id':1,'expected_version':item['version'],'result_id':old['result_id']})
    assert ack.status_code==409
    run(engine,provider);latest=progress(http,item)
    assert latest['evidence'][0]['version']==2 and latest['evidence'][0]['quote']=='修订版本'


def test_plan_change_is_feedback_only_and_manual_change_invalidates_job(client,model):
    http,engine=client;item=setup_matter(http,engine)
    add_note(engine,'医生：原计划改为明天下午联系家属。')
    run(engine,ProgressTestModel());latest=progress(http,item)
    assert latest['state']=='PLAN_CHANGED'
    current=next(i for i in list_data(http)['items'] if i['id']==item['id'])
    assert current['status']=='OPEN' and current['title']==item['title']
    changed=action(http,current,'UPDATE',title='医生确认后的新计划').json()
    assert progress(http,changed)['status']=='PENDING'
    finished=action(http,changed,'COMPLETE',note='医生明确完成').json()
    result=run(engine,ProgressTestModel())
    assert result is None or result['status']=='STALE'
    assert finished['status']=='COMPLETED'


def test_source_edit_during_model_call_discards_old_result(client,model):
    http,engine=client;item=setup_matter(http,engine)
    class Concurrent(ProgressTestModel):
        def observe(self,snapshot):
            add_note(engine,'医生：已向女儿核实，补充记录。')
            return super().observe(snapshot)
    assert run(engine,Concurrent())['status']=='STALE'
    with Session(engine) as db:assert db.scalar(select(func.count()).select_from(MatterProgress))==0
    assert run(engine,ProgressTestModel())['status']=='SUCCEEDED'


def test_failure_retries_have_limits_and_explicit_retry(client,model):
    http,engine=client;item=setup_matter(http,engine)
    class Failure:
        def observe(self,*_):raise DraftError('MODEL_TIMEOUT')
    for attempt in range(1,4):
        assert run(engine,Failure())['status']=='FAILED'
        with Session(engine) as db:
            job=db.scalar(select(MatterProgressJob).order_by(MatterProgressJob.id.desc()))
            assert job.attempt==attempt
            if job.retry_at:job.retry_at=datetime.now(timezone.utc)-timedelta(seconds=1);db.commit()
    assert run(engine,Failure()) is None
    latest=progress(http,item);assert latest['needs_attention']
    retry=http.post(f"/api/agent/matters/{item['id']}/progress/retry",json={
        'doctor_id':1,'expected_version':item['version'],'job_id':latest['job_id'],'expected_attempt':3})
    assert retry.status_code==200,retry.text
    assert run(engine,ProgressTestModel())['status']=='SUCCEEDED'


def test_bad_evidence_and_origin_only_response_rejected(client,model):
    http,engine=client;setup_matter(http,engine)
    class Origin:
        model='test'
        def observe(self,snapshot):
            s=snapshot['sources'][0]
            return Observation(state='RESPONSE_RECORDED',summary='错误地把计划当执行',references=[dict(source_id=s['id'],quote=s['text'])])
    assert run(engine,Origin())['error_code']=='ORIGINAL_PLAN_IS_NOT_RESPONSE'
    with pytest.raises(DraftError,match='UNGROUNDED_PROGRESS_REFERENCE'):
        runtime.validate({'sources':[],'coverage':{'complete':True}},Observation(state='RESULT_AVAILABLE',summary='错误引用',references=[dict(source_id='outside',quote='其他住院')]))


def test_due_boundary_and_crash_lease_recovery(client,model):
    http,engine=client;item=setup_matter(http,engine)
    with Session(engine) as db:
        matter=db.get(PatientMatter,item['id']);matter.due_at=datetime.now(timezone.utc)+timedelta(seconds=5);db.commit()
    provider=ProgressTestModel();run(engine,provider)
    with Session(engine) as db:
        matter=db.get(PatientMatter,item['id']);matter.due_at=datetime.now(timezone.utc)-timedelta(seconds=1);db.commit()
        runtime.schedule(db)
        job=db.scalar(select(MatterProgressJob).order_by(MatterProgressJob.id.desc()))
        job.status='RUNNING';job.attempt=1;job.lease_token='crashed';job.lease_until=datetime.now(timezone.utc)-timedelta(seconds=1);db.commit()
    assert run(engine,provider)['status']=='SUCCEEDED'


@pytest.mark.parametrize('case',['old','unrelated','ambiguous','valid'])
def test_lab_correspondence_is_conservative(case):
    source={'id':'lab','kind':'LAB','label':'血常规','at':'2026-10-31T03:00:00+00:00',
            'text':'合成结果','version':1,'is_origin':False}
    if case=='old':source['at']='2026-10-30T01:00:00+00:00'
    if case=='unrelated':source['label']='尿常规'
    sources=[source]
    if case=='ambiguous':sources.append({**source,'id':'second-lab'})
    snapshot={'matter':{'title':'复查血常规','quote':'明天复查血常规','kind':'RECHECK',
        'original_source':{'at':'2026-10-31T01:00:00+00:00'}},'coverage':{'complete':True},'sources':sources}
    result=Observation(state='RESULT_AVAILABLE',summary='模型声称已到',references=[dict(source_id='lab',quote='合成结果')])
    runtime.validate(snapshot,result)
    assert result.state==('RESULT_AVAILABLE' if case=='valid' else 'UNCLEAR')


def test_conflict_evidence_and_incomplete_coverage_guards():
    sources=[{'id':'a','kind':'NOTE','text':'记录一：患者否认过敏','is_origin':False},
             {'id':'b','kind':'NOTE','text':'记录二：家属明确青霉素过敏','is_origin':False}]
    observation=Observation(state='CONFLICT',summary='两条记录有矛盾，待核实',references=[
        dict(source_id='a',quote='患者否认过敏'),dict(source_id='b',quote='家属明确青霉素过敏')])
    assert len(runtime.validate({'sources':sources,'coverage':{'complete':True}},observation))==2
    with pytest.raises(DraftError,match='INCOMPLETE_COVERAGE_REQUIRES_UNCLEAR'):
        runtime.validate({'sources':[],'coverage':{'complete':False}},Observation(state='WAITING',summary='未查到',references=[]))
    runtime.validate({'sources':[],'coverage':{'complete':False}},Observation(state='UNCLEAR',summary='覆盖不足，需人工核对',references=[]))


def test_discharge_stops_followup_without_closing_matter(client,model):
    http,engine=client;item=setup_matter(http,engine)
    with Session(engine) as db:
        db.get(Encounter,1).status='DISCHARGED';db.commit()
    assert run(engine,ProgressTestModel()) is None
    assert next(i for i in list_data(http)['items'] if i['id']==item['id'])['status']=='OPEN'
