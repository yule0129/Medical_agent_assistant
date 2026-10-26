"""Real DB/tool/runtime integration with explicitly injected test model (no online claims)."""
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from test_rounding_workflow import client
from test_report_followup import propose, confirm, import_lab, detail, action
from agent.llm import deepseek as provider
from agent.contracts import parse_action, RunBudget
from agent.llm.deepseek import DeepSeekProvider, ProviderError
from agent.runtime.worker import process_agent_once
from agent.core.loop import investigate
from agent.runtime.scheduler import schedule_due
from agent.tools.reader import execute_read
from backend.models import AgentJob, AgentProposal, AgentRun, AgentStep, Encounter, MedicalRecord, OpenLoop
from backend.worker.processor import run_once


class TestModel:
    name = 'test-model'
    model = 'injected-only'
    __test__ = False

    def __init__(self, mode='missing'):
        self.mode = mode
        self.inputs = []

    def next_action(self, context, history, timeout):
        self.inputs.append((context, list(history)))
        names = [h['tool_name'] for h in history]
        for name in ('get_lab_reports', 'get_result_acknowledgements'):
            if name not in names:
                return parse_action({'action':'call_tool','reason':'取证','tool_name':name}), {}
        if self.mode == 'ambiguous':
            return parse_action({'action':'request_review','reason':'无法确定培养记录对应哪份报告'}), {}
        if self.mode == 'wait':
            return parse_action({'action':'wait','reason':'等待外部事件','waiting_for':['LAB_RESULT_CREATED']}), {}
        if self.mode != 'found' and 'get_progress_notes' not in names:
            return parse_action({'action':'call_tool','reason':'查病程','tool_name':'get_progress_notes'}), {}
        sources = [s for h in history for s in h['sources']]
        refs = [s for s in sources if s['source_type']=='lab_report'][:1]
        if self.mode in {'follow','note_found'}:
            refs += [s for s in sources if s['source_type']=='medical_record'][:1]
        if self.mode=='found':
            refs += [s for s in sources if s['source_type']=='result_acknowledgement'][:1]
        kind = 'FOLLOW_UP' if self.mode=='follow' else 'RESPONSE_FOUND' if self.mode in {'found','note_found'} else 'RESPONSE_NOT_FOUND'
        item={'kind':kind,'summary':'合成调查候选，等待医生审核',
            'evidence':[{'source_type':s['source_type'],'source_id':s['source_id'],'source_version':s['source_version'],'quote':s['text'][:80]} for s in refs],
            'searched_sources':[{k:h[k] for k in ('tool_name','window','query_succeeded','complete')} for h in history]}
        if kind=='FOLLOW_UP':item['follow_up']={'test_name':'药敏','specimen_id':'BC-S1'}
        if self.mode=='fabricated':item['evidence'][0]['source_id']=999999
        return parse_action({'action':'submit_candidate','reason':'证据已查询','candidate':item}), {'total_tokens':10}


def setup(http, engine, monkeypatch):
    monkeypatch.setattr(provider,'configured',lambda:True)
    with Session(engine) as db:
        db.get(Encounter,1).admitted_at=datetime.now(timezone.utc)-timedelta(days=1)
        db.commit()
    loop=confirm(http,propose(http),{'test_name':'血培养','specimen_id':'BC-S1'})
    import_lab(http, reported_at=(datetime.now(timezone.utc)-timedelta(minutes=10)).isoformat())
    return loop


def queue(http, loop, key='manual'):
    response=http.post(f'/api/agent/loops/{loop}/runs',json={'doctor_id':1,'expected_version':detail(http,loop)['task']['version'],'request_key':key})
    assert response.status_code==200,response.text
    return response.json()['job_id']


def run(engine, model):
    with Session(engine,autoflush=False) as db:
        return process_agent_once(db,model)


def panel(http,loop):
    response=http.get(f'/api/agent/loops/{loop}')
    assert response.status_code==200,response.text
    return response.json()


def test_dynamic_model_loop_actual_tools_and_missing_response_review(client,monkeypatch):
    http,engine=client
    loop=setup(http,engine,monkeypatch)
    job=queue(http,loop)
    assert queue(http,loop)==job
    result=run(engine,TestModel())
    assert result['stop_reason']=='CANDIDATE_READY'
    data=panel(http,loop)
    assert [s['tool_name'] for s in data['runs'][0]['steps'] if s['action']=='ACT']==['get_lab_reports','get_result_acknowledgements','get_progress_notes']
    candidate=data['proposals'][0]
    assert candidate['candidate']['kind']=='RESPONSE_NOT_FOUND'
    assert detail(http,loop)['task']['status']=='RESULT_AVAILABLE'
    reviewed=http.post(f"/api/agent/proposals/{candidate['id']}/review",json={'doctor_id':1,'expected_version':1,'expected_loop_version':data['loop_version'],'decision':'ACCEPT'})
    assert reviewed.status_code==200,reviewed.text
    assert detail(http,loop)['task']['status']=='RESULT_AVAILABLE'
    assert run(engine,TestModel()) is None


def test_existing_ack_changes_model_tool_path_and_preserves_closed_task(client,monkeypatch):
    http,engine=client
    loop=setup(http,engine,monkeypatch)
    evidence=detail(http,loop)['evidence']['id']
    action(http,loop,'acknowledge',evidence_id=evidence)
    action(http,loop,'resolve','合成处理结论')
    queue(http,loop)
    result=run(engine,TestModel('found'))
    assert result['stop_reason']=='CANDIDATE_READY'
    data=panel(http,loop)
    assert len([s for s in data['runs'][0]['steps'] if s['action']=='ACT'])==2
    assert data['proposals'][0]['candidate']['kind']=='RESPONSE_FOUND'
    assert detail(http,loop)['task']['status']=='RESOLVED'


def test_note_plan_creates_reviewed_dependency_then_second_report_resumes(client,monkeypatch):
    http,engine=client
    loop=setup(http,engine,monkeypatch)
    queue(http,loop)
    run(engine,TestModel('ambiguous'))
    assert not panel(http,loop)['proposals']
    version=detail(http,loop)['task']['version']
    note={'doctor_id':1,'expected_version':version,'request_key':'doctor-update','text':'已知晓本次血培养报告，继续等待药敏。'}
    response=http.post(f'/api/agent/loops/{loop}/progress',json=note)
    assert response.status_code==200,response.text
    assert http.post(f'/api/agent/loops/{loop}/progress',json=note).json()['duplicate']
    with Session(engine) as db:run_once(db)
    result=run(engine,TestModel('follow'))
    assert result['stop_reason']=='CANDIDATE_READY',panel(http,loop)
    data=panel(http,loop);p=data['proposals'][0]
    assert p['candidate']['kind']=='FOLLOW_UP'
    payload={'doctor_id':1,'expected_version':1,'expected_loop_version':data['loop_version'],'decision':'ACCEPT',
        'effect':'CREATE_FOLLOW_UP','evidence_id':p['evidence_id'],'note':'医生明确核对并批准'}
    reviewed=http.post(f"/api/agent/proposals/{p['id']}/review",json=payload)
    assert reviewed.status_code==200,reviewed.text
    child=reviewed.json()['child_loop_id']
    assert reviewed.json()['loop_status']=='ACKNOWLEDGED'
    assert http.post(f"/api/agent/proposals/{p['id']}/review",json=payload).status_code==409
    import_lab(http,event_key='SENS-EVENT',report_id='SENS-R1',test_name='药敏',result_text='合成药敏报告')
    result=run(engine,TestModel('ambiguous'))
    assert result['stop_reason']=='NEEDS_REVIEW'
    child_data=panel(http,child)
    assert child_data['runs'][0]['prior_run_id'] and child_data['runs'][0]['plan_version']==2
    assert detail(http,child)['task']['status']=='RESULT_AVAILABLE'
    handoff=http.post('/api/encounters/1/handoffs',json={'doctor_id':1}).json()
    assert any(ref.get('loop_id')==child for ref in handoff['references'])


def test_unobserved_evidence_never_becomes_proposal(client,monkeypatch):
    http,engine=client;loop=setup(http,engine,monkeypatch);queue(http,loop)
    assert run(engine,TestModel('fabricated'))['stop_reason']=='NEEDS_REVIEW'
    assert not panel(http,loop)['proposals']
    assert detail(http,loop)['task']['status']=='RESULT_AVAILABLE'


def test_report_revision_prevents_old_candidate_review(client,monkeypatch):
    http,engine=client;loop=setup(http,engine,monkeypatch);queue(http,loop);run(engine,TestModel())
    data=panel(http,loop);p=data['proposals'][0]
    import_lab(http,event_key='LAB-V2',report_version=2,result_text='合成更正报告')
    response=http.post(f"/api/agent/proposals/{p['id']}/review",json={'doctor_id':1,'expected_version':1,'expected_loop_version':data['loop_version'],'decision':'ACCEPT'})
    assert response.status_code==409
    assert panel(http,loop)['proposals'][0]['stale']


def test_note_edit_invalidates_original_snapshot(client,monkeypatch):
    http,engine=client;loop=setup(http,engine,monkeypatch)
    queue(http,loop)
    with Session(engine) as db:
        row=MedicalRecord(record_number='ACK-NOTE',patient_id=1,encounter_id=1,doctor_id=1,record_type='病程',content='已知晓本次血培养报告',created_at=datetime.now(timezone.utc))
        db.add(row);db.commit();note_id=row.id
    assert run(engine,TestModel('note_found'))['stop_reason']=='CANDIDATE_READY'
    data=panel(http,loop);p=data['proposals'][0]
    with Session(engine) as db:db.get(MedicalRecord,note_id).content='修改过的原文';db.commit()
    response=http.post(f"/api/agent/proposals/{p['id']}/review",json={'doctor_id':1,'expected_version':1,'expected_loop_version':data['loop_version'],'decision':'ACCEPT'})
    assert response.status_code==409


def test_scope_is_enforced_and_no_configuration_is_explicit(client,monkeypatch):
    http,engine=client;loop=setup(http,engine,monkeypatch)
    assert http.get(f'/api/agent/loops/{loop}?doctor_id=999').status_code==403
    monkeypatch.setattr(provider,'configured',lambda:False)
    assert http.post(f'/api/agent/loops/{loop}/runs',json={'doctor_id':1,'expected_version':2,'request_key':'no-key'}).status_code==503
    assert http.get('/api/agent/status').json()['configured'] is False


def test_model_failure_persisted_and_explicit_retry_recovers(client,monkeypatch):
    http,engine=client;loop=setup(http,engine,monkeypatch);job=queue(http,loop)
    class Broken(TestModel):
        def next_action(self,*args):raise ProviderError('private error must not be stored')
    assert run(engine,Broken())['stop_reason']=='MODEL_ERROR'
    data=panel(http,loop)
    assert data['jobs'][0]['status']=='FAILED'
    assert 'private error' not in json.dumps(data)
    assert http.post(f'/api/agent/jobs/{job}/retry',json={'doctor_id':1}).status_code==200
    assert queue(http,loop)==job  # retry preserves the original request identity
    assert run(engine,TestModel())['stop_reason']=='CANDIDATE_READY'


def test_durable_expired_lease_and_timer_do_not_duplicate_effects(client,monkeypatch):
    http,engine=client;loop=setup(http,engine,monkeypatch);job_id=queue(http,loop)
    with Session(engine) as db:
        job=db.get(AgentJob,job_id);job.status='RUNNING';job.lease_until=datetime.now(timezone.utc)-timedelta(seconds=1);job.attempt=1
        db.get(OpenLoop,loop).due_at=datetime.now(timezone.utc)-timedelta(seconds=1);db.commit()
    assert run(engine,TestModel())['stop_reason']=='CANDIDATE_READY'
    with Session(engine) as db:schedule_due(db);schedule_due(db)
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(AgentJob))==2
    assert run(engine,TestModel('ambiguous'))['stop_reason']=='NEEDS_REVIEW'
    assert run(engine,TestModel()) is None


def test_handoff_edit_confirm_and_stale_task_guard(client,monkeypatch):
    http,engine=client;loop=setup(http,engine,monkeypatch)
    draft=http.post('/api/encounters/1/handoffs',json={'doctor_id':1}).json()
    edit=http.post(f"/api/agent/handoffs/{draft['id']}/revisions",json={'doctor_id':1,'expected_version':1,'content':draft['content']+'\n医生补充交接说明'})
    assert edit.status_code==200,edit.text
    edited=edit.json()
    response=http.post(f"/api/agent/handoffs/{edited['id']}/confirm",json={'doctor_id':1,'expected_version':2,'request_key':'confirm'})
    assert response.status_code==200,response.text
    saved=http.get('/api/agent/encounters/1/handoffs').json()
    assert saved[0]['status']=='CONFIRMED' and saved[1]['content']==draft['content']
    assert detail(http,loop)['task']['status']=='RESULT_AVAILABLE'
    import_lab(http,event_key='REVISION',report_version=2,result_text='修订报告')
    assert http.post(f"/api/agent/handoffs/{draft['id']}/confirm",json={'doctor_id':1,'expected_version':1,'request_key':'stale'}).status_code==409


def test_provider_json_request_and_sanitized_failure(monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY','test-key')
    calls=[]
    def transport(request):
        data=json.loads(request.content);calls.append(data)
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'action':'request_review','reason':'需医生核对'})}}],'usage':{'total_tokens':4}})
    model=DeepSeekProvider(transport=httpx.MockTransport(transport))
    action,usage=model.next_action({'goal':'合成测试'},[],5)
    assert action.action=='request_review' and usage['total_tokens']==4
    assert calls[0]['response_format']=={'type':'json_object'}
    bad=DeepSeekProvider(transport=httpx.MockTransport(lambda request:httpx.Response(401,text='secret test-key')))
    with pytest.raises(ProviderError) as exc:bad.next_action({},[],5)
    assert 'test-key' not in str(exc.value)


def test_agent_tool_limits_scope_and_reports_partial(client,monkeypatch):
    http,engine=client;loop=setup(http,engine,monkeypatch);queue(http,loop)
    captured=[]
    class Capture(TestModel):
        def next_action(self,context,history,timeout):
            captured.append(context)
            return super().next_action(context,history,timeout)
    run(engine,Capture('wait'))
    from agent.contracts import AgentRunInput
    data=captured[0]
    with Session(engine) as db:
        row=db.get(OpenLoop,loop)
        request=AgentRunInput.model_validate({k:v for k,v in data.items() if k!='trusted_context'} | {'scope':{
            'actor_id':1,'patient_id':1,'encounter_id':1,'intent_id':row.intent_id,'loop_id':loop,'expected_loop_version':row.version}})
    result=execute_read(engine,request,parse_action({'action':'call_tool','reason':'检查截断','tool_name':'get_progress_notes','arguments':{'limit':1}}),5)
    assert result['query_succeeded']
    bad=request.model_copy(update={'scope':request.scope.model_copy(update={'actor_id':999})})
    with pytest.raises(ValueError):execute_read(engine,bad,parse_action({'action':'call_tool','reason':'越权','tool_name':'get_lab_reports'}),5)
    with pytest.raises(ValueError):execute_read(engine,request,parse_action({'action':'call_tool','reason':'错误参数','tool_name':'get_progress_notes','arguments':{'report_id':'not-applicable'}}),5)
    with Session(engine) as db:
        db.add(MedicalRecord(record_number='EXTRA',patient_id=1,encounter_id=1,content='另一份合成病程',created_at=datetime.now(timezone.utc)-timedelta(seconds=1)));db.commit()
    result=execute_read(engine,request,parse_action({'action':'call_tool','reason':'检查截断','tool_name':'get_progress_notes','arguments':{'limit':1}}),5)
    assert result['status']=='partial' and not result['complete']


def test_budget_is_enforced_and_changes_during_model_call_are_stale(client,monkeypatch):
    http,engine=client;loop=setup(http,engine,monkeypatch);queue(http,loop)
    monkeypatch.setenv('AGENT_MAX_TOOL_CALLS','1')
    assert run(engine,TestModel())['stop_reason']=='BUDGET_EXHAUSTED'
    monkeypatch.delenv('AGENT_MAX_TOOL_CALLS')
    queue(http,loop,'stale-call')
    class Concurrent(TestModel):
        def next_action(self,context,history,timeout):
            with Session(engine) as db:
                row=db.get(OpenLoop,loop);row.version+=1;db.commit()
            return parse_action({'action':'wait','reason':'等待报告','waiting_for':['LAB_RESULT_CREATED']}),{}
    assert run(engine,Concurrent())['stop_reason']=='STALE_CONTEXT'
    assert not panel(http,loop)['proposals']


def test_grace_period_and_invented_search_coverage_rejected(client,monkeypatch):
    http,engine=client;loop=setup(http,engine,monkeypatch)
    # Fresh corrected report cannot immediately become a missing-response claim.
    import_lab(http,event_key='FRESH-V2',report_version=2,result_text='刚返回的报告')
    queue(http,loop)
    assert run(engine,TestModel())['stop_reason']=='NEEDS_REVIEW'
    assert not panel(http,loop)['proposals']
    with Session(engine) as db:
        assert db.scalar(select(AgentStep).where(AgentStep.action=='GUARD_REJECTED')).result_summary['reason']=='RESPONSE_GRACE_PERIOD'


def test_reject_requires_reason_and_does_not_acknowledge(client,monkeypatch):
    http,engine=client;loop=setup(http,engine,monkeypatch);queue(http,loop);run(engine,TestModel())
    data=panel(http,loop);p=data['proposals'][0]
    payload={'doctor_id':1,'expected_version':1,'expected_loop_version':data['loop_version'],'decision':'REJECT'}
    assert http.post(f"/api/agent/proposals/{p['id']}/review",json=payload).status_code==422
    assert http.post(f"/api/agent/proposals/{p['id']}/review",json={**payload,'note':'已有其他来源，需核对'}).status_code==200
    assert detail(http,loop)['task']['status']=='RESULT_AVAILABLE'


def test_provider_wall_timeout_and_incomplete_output(monkeypatch):
    import asyncio
    monkeypatch.setenv('DEEPSEEK_API_KEY','test-key')
    async def slow(request):
        await asyncio.sleep(.1)
        return httpx.Response(200,json={})
    model=DeepSeekProvider(transport=httpx.MockTransport(slow))
    with pytest.raises(ProviderError):model.next_action({},[],.01)
    incomplete=DeepSeekProvider(transport=httpx.MockTransport(lambda request:httpx.Response(200,json={'choices':[{'finish_reason':'length','message':{'content':'{}'}}]})))
    with pytest.raises(ProviderError):incomplete.next_action({},[],5)


def test_agent_recovers_in_separate_worker_interpreters(client,monkeypatch,tmp_path):
    import sqlite3
    import subprocess
    import sys
    from pathlib import Path
    http,engine=client;loop=setup(http,engine,monkeypatch);job_id=queue(http,loop)
    with Session(engine) as db:
        job=db.get(AgentJob,job_id);job.status='RUNNING';job.attempt=1
        job.lease_until=datetime.now(timezone.utc)-timedelta(minutes=1);db.commit()
    path=tmp_path/'agent-restart.sqlite'
    raw=engine.raw_connection()
    try:
        with sqlite3.connect(path) as target:raw.driver_connection.backup(target)
    finally:raw.close()
    script='''
import json,sys
from sqlalchemy import create_engine,select,func
from sqlalchemy.orm import Session
sys.path.insert(0,'tests')
from test_agent_runtime import TestModel
from agent.runtime.worker import process_agent_once
from backend.models import AgentProposal,AgentJob
engine=create_engine('sqlite:///'+sys.argv[1])
with Session(engine,autoflush=False) as db:
    result=process_agent_once(db,TestModel(),job_id=int(sys.argv[2]))
    print(json.dumps({'result':result,'proposals':db.scalar(select(func.count()).select_from(AgentProposal)),'status':db.get(AgentJob,int(sys.argv[2])).status}))
'''
    outputs=[]
    for _ in range(2):
        result=subprocess.run([sys.executable,'-c',script,str(path),str(job_id)],cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,timeout=30)
        assert result.returncode==0,result.stderr
        outputs.append(json.loads(result.stdout))
    assert outputs[0]['result']['stop_reason']=='CANDIDATE_READY'
    assert outputs[1]['result'] is None
    assert outputs[0]['proposals']==outputs[1]['proposals']==1
    assert outputs[1]['status']=='SUCCEEDED'


@pytest.mark.parametrize('name,mode',[('missing','missing'),('found','found'),('follow','follow')])
def test_online_smoke_fixture_with_injected_model_never_touches_dev_db(monkeypatch,name,mode):
    from agent.evaluation.smoke import scenario
    monkeypatch.setattr(provider,'get_provider',lambda:TestModel(mode))
    result=scenario(name)
    assert result['passed'] and result['provider']=='test-model'


def test_format_repair_counts_against_model_budget(client,monkeypatch):
    http,engine=client;loop=setup(http,engine,monkeypatch);queue(http,loop)
    class Repair(TestModel):
        def next_action(self,context,history,timeout):
            if not history:raise ProviderError('MODEL_ACTION_NOT_JSON')
            return parse_action({'action':'wait','reason':'等待外部事件','waiting_for':['LAB_RESULT_CREATED']}),{}
    assert run(engine,Repair())['stop_reason']=='WAITING_EVENT'
    result=panel(http,loop)['jobs'][0]['result']
    assert result['model_requests']==2 and result['tool_calls']==0


def test_deepseek_disables_default_thinking_and_accepts_fenced_json(monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY','test-key')
    def transport(request):
        payload=json.loads(request.content)
        assert payload['thinking']=={'type':'disabled'}
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':'```json\n{"action":"request_review","reason":"需核对"}\n```'}}]})
    action,_=DeepSeekProvider(transport=httpx.MockTransport(transport)).next_action({},[],3)
    assert action.action=='request_review'
