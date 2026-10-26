"""Doctor feedback must reflect persisted outcomes and preserve scope/review boundaries."""
from datetime import datetime, timezone
from sqlalchemy.orm import Session

from test_rounding_workflow import client
from test_agent_runtime import setup, queue, run, TestModel, panel
from test_report_followup import import_lab, detail
from backend.models import AgentJob, MedicalRecord


def feedback(http):
    response=http.get('/api/agent/feedback')
    assert response.status_code==200,response.text
    return response.json()


def test_doctor_gets_pending_result_without_opening_detail(client,monkeypatch):
    http,engine=client;loop=setup(http,engine,monkeypatch);job_id=queue(http,loop)
    assert feedback(http)['items'][0]['state']=='queued'
    with Session(engine) as db:db.get(AgentJob,job_id).status='RUNNING';db.commit()
    assert feedback(http)['items'][0]['state']=='running'
    with Session(engine) as db:db.get(AgentJob,job_id).status='PENDING';db.commit()
    run(engine,TestModel())
    data=feedback(http);item=data['items'][0]
    assert data['counts']['attention']==data['counts']['pending_review']==1
    assert item['loop_id']==loop and item['proposal_id'] and item['requires_attention']
    assert item['action_label']=='查看证据并审核'
    assert '未发现明确响应' in item['title']
    assert http.get('/api/agent/feedback?doctor_id=999').json()['items']==[]
    assert http.get('/api/agent/feedback?doctor_id=999&encounter_id=1').status_code==403


def test_review_removes_attention_but_does_not_complete_task(client,monkeypatch):
    http,engine=client;loop=setup(http,engine,monkeypatch);queue(http,loop);run(engine,TestModel())
    item=feedback(http)['items'][0]
    response=http.post(f"/api/agent/proposals/{item['proposal_id']}/review",json={'doctor_id':1,'expected_version':item['proposal_version'],
        'expected_loop_version':item['loop_version'],'decision':'ACCEPT'})
    assert response.status_code==200,response.text
    data=feedback(http)
    assert data['counts']['attention']==0 and data['items'][0]['state']=='reviewed'
    assert detail(http,loop)['task']['status']=='RESULT_AVAILABLE'


def test_new_report_or_new_note_requires_reinvestigation_not_old_review(client,monkeypatch):
    http,engine=client;loop=setup(http,engine,monkeypatch);queue(http,loop);run(engine,TestModel())
    with Session(engine) as db:
        db.add(MedicalRecord(record_number='LATER',patient_id=1,encounter_id=1,doctor_id=1,content='新的合成病程',created_at=datetime.now(timezone.utc)));db.commit()
    data=feedback(http)
    assert data['items'][0]['state']=='stale' and data['counts']['pending_review']==0
    assert panel(http,loop)['proposals'][0]['stale']
    import_lab(http,event_key='NEWVERSION',report_version=2,result_text='合成更正')
    assert feedback(http)['items'][0]['state']=='stale'


def test_no_candidate_human_review_wait_and_error_are_visible(client,monkeypatch):
    http,engine=client;loop=setup(http,engine,monkeypatch)
    queue(http,loop);run(engine,TestModel('ambiguous'))
    assert feedback(http)['items'][0]['state']=='needs_review'
    queue(http,loop,'wait');run(engine,TestModel('wait'))
    assert feedback(http)['items'][0]['state']=='waiting'
    assert feedback(http)['counts']['attention']==0
    class Broken(TestModel):
        def next_action(self,*args):raise RuntimeError('hidden diagnostic')
    queue(http,loop,'fail');run(engine,Broken())
    assert feedback(http)['items'][0]['state']=='failed'
    assert feedback(http)['counts']['attention']==1
    assert 'hidden diagnostic' not in str(feedback(http))


def test_legacy_manual_task_explains_why_agent_cannot_start(client,monkeypatch):
    from test_report_followup import propose, confirm, action
    from agent.llm import deepseek as provider
    http,engine=client
    monkeypatch.setattr(provider,'configured',lambda:True)
    loop=confirm(http,propose(http))
    action(http,loop,'record-result','合成手工检验结果')
    action(http,loop,'acknowledge')
    data=panel(http,loop)
    assert not data['start_ready'] and '后续随访任务' in data['start_block_reason']
    response=http.post(f'/api/agent/loops/{loop}/runs',json={'doctor_id':1,'expected_version':data['loop_version'],'request_key':'legacy'})
    assert response.status_code==409 and '匹配编号' in response.json()['detail']
