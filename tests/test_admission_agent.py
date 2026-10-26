from datetime import datetime, timezone
import pytest
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from test_rounding_workflow import client
from backend.models import Encounter, MedicalRecord, ClinicalEvent, LabReport
from agent.admission import service
from agent.admission.contracts import Decision, HEADINGS, DraftError
from agent.admission.loop import generate


class AdmissionTestModel:
    name, model = 'injected-test', 'admission-test'

    def decide(self, observations, timeout):
        tools = ('read_patient_profile', 'read_interview', 'read_examinations')
        if len(observations) < 3:
            return Decision(action='call_tool', tool_name=tools[len(observations)])
        interview = observations[1]['sources'][0]['id']
        exams = observations[2]['sources']
        sections = []
        for heading in HEADINGS:
            if heading == '主诉':
                sections.append(dict(heading=heading, text='患者自诉咳嗽3天', source_ids=[interview], missing=False))
            elif heading == '辅助检查' and exams:
                sections.append(dict(heading=heading, text=exams[0]['text'], source_ids=[exams[0]['id']], missing=False))
            else:
                sections.append(dict(heading=heading, text='未提供，待医生补充', source_ids=[], missing=True))
        return Decision(action='submit_draft', sections=sections)


@pytest.fixture
def model(monkeypatch):
    monkeypatch.setattr(service, 'configured', lambda: True)
    monkeypatch.setattr(service, 'get_provider', lambda: AdmissionTestModel())


def save_inputs(http, **changes):
    payload=dict(doctor_id=1, encounter_id=1, transcript='医生：哪里不舒服？\n患者：咳嗽3天。',
                 examination_text='血常规：白细胞12×10^9/L（合成测试）', expected_input_version=0)
    payload.update(changes)
    return http.put('/api/patients/1/admission-inputs', json=payload)


def run_generation(http, state):
    return http.post('/api/patients/1/admission-generate', json=dict(doctor_id=1, encounter_id=1,
        expected_revision=state['revision'], expected_source_digest=state['agent']['source_digest']))


def review(http, state, **changes):
    payload=dict(doctor_id=1, encounter_id=1, content=state['record']['content']+'\n医生核实补充',
                 expected_revision=state['revision'], expected_source_digest=state['agent']['source_digest'], confirmed=True)
    payload.update(changes)
    return http.post('/api/patients/1/admission-review', json=payload)


def test_agent_collects_sources_generates_draft_then_requires_review(client, model):
    http, engine=client
    state=save_inputs(http).json()
    response=run_generation(http,state)
    assert response.status_code==200,response.text
    state=response.json()
    assert state['written'] and not state['reviewed'] and state['reminder_needed']
    assert state['agent']['status']=='PENDING_REVIEW'
    assert len(state['agent']['trace'])==4
    assert '既往史' in state['agent']['draft']['missing_sections']
    assert '咳嗽3天' in state['record']['content']
    tasks=http.get('/api/admission-tasks').json()
    assert tasks[0]['waiting_for']=='审查入院记录' and tasks[0]['status']=='WAITING_EVENT'
    assert review(http,state,confirmed=False).status_code==422
    signed=review(http,state)
    assert signed.status_code==200,signed.text
    assert signed.json()['reviewed'] and not signed.json()['reminder_needed']
    assert http.get('/api/admission-tasks').json()[0]['status']=='RESOLVED'
    assert run_generation(http,signed.json()).status_code==409
    assert save_inputs(http,expected_input_version=state['agent']['input_version']).status_code==409
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(MedicalRecord))==1


def test_changed_interview_blocks_stale_review_and_regeneration_keeps_record_identity(client, model):
    http,_=client
    state=run_generation(http,save_inputs(http).json()).json()
    changed=save_inputs(http,transcript='医生更正：咳嗽2天。',expected_input_version=state['agent']['input_version']).json()
    assert changed['agent']['stale']
    assert review(http,state).status_code==409
    assert review(http,changed).status_code==409
    assert http.post(f"/api/medical-records/{state['record']['id']}/sign").status_code==409
    regenerated=run_generation(http,changed)
    assert regenerated.status_code==200,regenerated.text
    assert regenerated.json()['record']['id']==state['record']['id']
    assert not regenerated.json()['agent']['stale']


def test_generation_failure_persists_inputs_and_does_not_fake_a_draft(client, model, monkeypatch):
    http,_=client
    class Failure:
        def decide(self,*args): raise DraftError('MODEL_TIMEOUT')
    monkeypatch.setattr(service,'get_provider',lambda:Failure())
    state=save_inputs(http).json()
    assert run_generation(http,state).status_code==502
    after=http.get('/api/patients/1/admission-record?encounter_id=1').json()
    assert after['agent']['transcript']==state['agent']['transcript']
    assert after['agent']['status']=='FAILED' and not after['written']
    assert after['agent']['error_code']=='MODEL_TIMEOUT'


def test_generation_rejects_bad_scope_empty_interview_and_stale_input_version(client, model):
    http,_=client
    state=http.get('/api/patients/1/admission-record?encounter_id=1').json()
    assert run_generation(http,state).status_code==422
    assert save_inputs(http,doctor_id=2).status_code==403
    saved=save_inputs(http).json()
    assert save_inputs(http).status_code==409
    assert run_generation(http,state).status_code==409
    assert saved['agent']['transcript']


def test_model_must_read_scoped_tools_and_cite_known_sources():
    class BadModel:
        name,model='test','test'
        def decide(self,*_): return Decision(action='submit_draft')
    with pytest.raises(DraftError,match='REQUIRED_SOURCES_NOT_READ'):
        generate(BadModel(),dict.fromkeys(('read_patient_profile','read_interview','read_examinations'),[]))
    class BadCitation(AdmissionTestModel):
        def decide(self,observations,timeout):
            result=super().decide(observations,timeout)
            if result.action=='submit_draft':result.sections[0].source_ids=['another-patient-source']
            return result
    sources={'read_patient_profile':[], 'read_interview':[{'id':'interview:1:v1','text':'测试'}], 'read_examinations':[]}
    with pytest.raises(DraftError,match='UNKNOWN_SOURCE_REFERENCE'):
        generate(BadCitation(),sources)


def test_changed_sources_during_generation_never_overwrite_record(client, model, monkeypatch):
    http, engine=client
    state=save_inputs(http).json()
    class ConcurrentChange(AdmissionTestModel):
        changed=False
        def decide(self, observations, timeout):
            if not self.changed:
                self.changed=True
                from agent.admission.models import AdmissionWorkspace
                with Session(engine) as db:
                    row=db.scalar(select(AdmissionWorkspace))
                    row.transcript='生成期间医生更新问诊';row.input_version+=1
                    db.commit()
            return super().decide(observations, timeout)
    monkeypatch.setattr(service,'get_provider',lambda:ConcurrentChange())
    response=run_generation(http,state)
    assert response.status_code==409,response.text
    latest=http.get('/api/patients/1/admission-record?encounter_id=1').json()
    assert not latest['written'] and latest['agent']['transcript']=='生成期间医生更新问诊'


def test_agent_read_loop_has_a_finite_budget():
    class Repeating:
        name,model='test','test'
        def decide(self,*args):return Decision(action='call_tool',tool_name='read_interview')
    with pytest.raises(DraftError,match='BUDGET_EXHAUSTED'):
        generate(Repeating(),{'read_interview':[]})


def test_report_reader_uses_only_latest_same_admission_and_invalidates_draft(client, model):
    http,engine=client
    now=datetime.now(timezone.utc)
    with Session(engine) as db:
        db.add(Encounter(id=2,patient_id=1,encounter_number='OTHER',admitted_at=now,status='DISCHARGED'))
        db.flush()
        for i,(enc,version,text) in enumerate(((1,1,'旧版本'),(1,2,'新版本'),(2,9,'其他住院资料')),1):
            event=ClinicalEvent(event_key=f'admission-lab-{i}',event_type='LAB',patient_id=1,encounter_id=enc,
                occurred_at=now,received_at=now,source='synthetic',payload={})
            db.add(event);db.flush()
            db.add(LabReport(event_id=event.id,patient_id=1,encounter_id=enc,report_id='A',report_version=version,
                             test_name='血常规',result_text=text,reported_at=now))
        db.commit()
    saved=save_inputs(http,examination_text='').json()
    labs=saved['agent']['sources']['read_examinations']
    assert len(labs)==1 and labs[0]['text']=='新版本'
    state=run_generation(http,saved).json()
    with Session(engine) as db:
        report=db.scalar(select(LabReport).where(LabReport.encounter_id==1,LabReport.report_version==2))
        report.result_text='已更正的结果';db.commit()
    assert review(http,state).status_code==409
