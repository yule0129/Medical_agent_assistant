from datetime import datetime, timezone
import pytest
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from test_rounding_workflow import client
from backend.models import AdmissionWorkspace, RoundingSession
from agent.patient_memory import service
from agent.patient_memory.models import PatientMatter, MatterScan
from agent.patient_memory.contracts import Extraction,ExtractionReview
from agent.admission.contracts import DraftError

TEXT='医生：药物过敏史还需向女儿核实。明天复查血常规。请联系心内科会诊。'


class TestMatterModel:
    __test__=False
    model='injected-memory-test'
    calls=0
    def extract(self,source,known):
        self.calls+=1
        return Extraction(items=[
            dict(kind='VERIFY_INFO',title='向女儿核实药物过敏史',quote='药物过敏史还需向女儿核实。',due_text='',classification='ACTIONABLE',action_text='核实',target_text='药物过敏史',evidence_kind='VERIFICATION_RECORD'),
            dict(kind='RECHECK',title='复查血常规',quote='明天复查血常规。',due_text='明天',classification='ACTIONABLE',action_text='复查',target_text='血常规',evidence_kind='LAB_REPORT'),
            dict(kind='CONTACT',title='联系心内科会诊',quote='请联系心内科会诊。',due_text='',classification='ACTIONABLE',action_text='联系',target_text='心内科',evidence_kind='CONTACT_RECORD')])

    def review(self,source,proposals,known):
        return ExtractionReview(assessments=[dict(index=i,decision='KEEP',reason='合成明确计划') for i in range(len(proposals))])


@pytest.fixture
def model(monkeypatch):
    provider=TestMatterModel()
    monkeypatch.setattr(service,'configured',lambda:True)
    monkeypatch.setattr(service,'get_provider',lambda:provider)
    return provider


def add_source(engine,kind='ROUNDING'):
    with Session(engine) as db:
        if kind=='ROUNDING':
            row=RoundingSession(encounter_id=1,doctor_id=1,started_at=datetime.now(timezone.utc),
                transcript=TEXT,record_mode='TRANSCRIPT',status='DRAFT')
        else:
            row=AdmissionWorkspace(encounter_id=1,transcript=TEXT,updated_at=datetime.now(timezone.utc))
        db.add(row);db.commit();return row.id


def list_data(http):return http.get('/api/agent/encounters/1/matters').json()
def scan(http,source):
    return http.post('/api/agent/encounters/1/matters/scan',json=dict(doctor_id=1,
        source_kind=source['kind'],source_id=source['id'],expected_digest=source['digest']))
def action(http,item,act,**extra):
    return http.post(f"/api/agent/matters/{item['id']}/review",json=dict(
        doctor_id=1,expected_version=item['version'],action=act,**extra))


@pytest.mark.parametrize('kind',['ADMISSION','ROUNDING'])
def test_source_grounded_proposals_persist_and_require_doctor_confirmation(client,model,kind):
    http,engine=client;add_source(engine,kind)
    source=list_data(http)['sources'][0]
    result=scan(http,source)
    assert result.status_code==200,result.text
    items=list_data(http)['items']
    assert len(items)==3 and all(i['status']=='CANDIDATE' for i in items)
    assert http.get('/api/agent/matter-tasks').json()==[]
    for item in items:
        assert item['quote'] in TEXT and item['source']['text']==TEXT
        assert item['due_at'] is None and item['assigned_doctor_id'] is None
    dated=next(i for i in items if i['kind']=='RECHECK')
    due='2026-11-01T09:00:00+08:00'
    accepted=action(http,dated,'CONFIRM',title='医生核对：复查血常规',due_at=due,assigned_doctor_id=1)
    assert accepted.status_code==200,accepted.text
    accepted=accepted.json()
    assert accepted['status']=='OPEN' and accepted['assigned_doctor_name']=='合成医生'
    assert datetime.fromisoformat(accepted['due_at'])==datetime.fromisoformat(due)
    assert len(http.get('/api/agent/matter-tasks').json())==1
    assert scan(http,source).json()['cached'] and model.calls==1
    assert len(list_data(http)['items'])==3


def test_matter_versions_reasons_completion_and_manual_arrangement(client,model):
    http,engine=client;add_source(engine);scan(http,list_data(http)['sources'][0])
    items=list_data(http)['items'];item=items[0]
    assert action(http,item,'COMPLETE',note='不能跳过确认').status_code==409
    opened=action(http,item,'CONFIRM').json()
    assert action(http,item,'CONFIRM').status_code==409
    assert action(http,opened,'COMPLETE').status_code==422
    changed=action(http,opened,'UPDATE',title='医生修改后的计划').json()
    finished=action(http,changed,'COMPLETE',note='已联系并记录实际处理情况')
    assert finished.status_code==200,finished.text
    assert finished.json()['status']=='COMPLETED'
    assert http.get('/api/agent/matter-tasks').json()[0]['status']=='RESOLVED'
    assert action(http,items[1],'DISMISS').status_code==422
    assert action(http,items[1],'DISMISS',note='与已有安排重复').json()['status']=='DISMISSED'


def test_scope_source_isolation_and_stale_quote_protection(client,model):
    http,engine=client;source_id=add_source(engine)
    source=list_data(http)['sources'][0];scan(http,source)
    assert http.get('/api/agent/encounters/1/matters?doctor_id=2').status_code==403
    assert http.get('/api/agent/matter-tasks?doctor_id=2').json()==[]
    item=list_data(http)['items'][0]
    assert action(http,item,'CONFIRM',assigned_doctor_id=999).status_code==422
    with Session(engine) as db:
        row=db.get(RoundingSession,source_id);row.transcript='医生已修改查房计划';row.version+=1;db.commit()
    assert scan(http,source).status_code==409
    assert list_data(http)['items'][0]['source_changed']
    assert action(http,item,'CONFIRM').status_code==409
    assert action(http,item,'DISMISS',note='原文已修改').status_code==200
    assert list_data(http)['items'][0]['source']['text']==TEXT


def test_ungrounded_proposal_and_model_failure_do_not_create_tasks(client,model,monkeypatch):
    http,engine=client;add_source(engine);source=list_data(http)['sources'][0]
    class Bad:
        model='test'
        def extract(self,*_):return Extraction(items=[dict(kind='RECHECK',title='编造检查',quote='并不存在的医嘱',due_text='',classification='ACTIONABLE',action_text='检查',target_text='编造',evidence_kind='LAB_REPORT')])
    monkeypatch.setattr(service,'get_provider',lambda:Bad())
    assert scan(http,source).status_code==502
    data=list_data(http)
    assert data['items']==[] and data['scans'][0]['error_code']=='UNGROUNDED_SOURCE_QUOTE'
    monkeypatch.setattr(service,'get_provider',lambda:model)
    assert scan(http,source).status_code==200
    assert len(list_data(http)['items'])==3


def test_empty_extraction_is_a_success_and_is_cached(client,model,monkeypatch):
    http,engine=client;add_source(engine)
    class Empty:
        model='test'
        def extract(self,*_):return Extraction(items=[])
    monkeypatch.setattr(service,'get_provider',lambda:Empty())
    source=list_data(http)['sources'][0]
    assert scan(http,source).json()['candidate_count']==0
    assert scan(http,source).json()['cached']
    assert not list_data(http)['items']


def test_changed_source_during_model_call_is_not_saved_as_current_candidate(client,model,monkeypatch):
    http,engine=client;source_id=add_source(engine);source=list_data(http)['sources'][0]
    class Concurrent(TestMatterModel):
        def extract(self,source,known):
            with Session(engine) as db:
                row=db.get(RoundingSession,source_id);row.transcript='生成期间修改';row.version+=1;db.commit()
            return super().extract(source,known)
    monkeypatch.setattr(service,'get_provider',lambda:Concurrent())
    assert scan(http,source).status_code==409
    assert list_data(http)['items']==[]
