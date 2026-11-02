from datetime import datetime,timedelta,timezone
from sqlalchemy import select,func
from sqlalchemy.orm import Session
from test_matter_progress import client,model,setup_matter,add_note,lab,run,ProgressTestModel
from test_patient_matters import list_data,action
from agent.patient_memory.actions.models import MatterAction,MatterLink
from agent.patient_memory.actions.contracts import ActionCandidate
from agent.patient_memory.models import PatientMatter


class ActionTestModel(ProgressTestModel):
    def observe(self,snapshot):
        result=super().observe(snapshot)
        if snapshot['matter']['kind']=='RECHECK':
            labs=[s for s in snapshot['sources'] if s['kind']=='LAB' and s['label']=='血常规' and not s['is_origin']]
            if labs:
                from agent.patient_memory.progress.contracts import Observation
                result=Observation(state='RESULT_AVAILABLE',summary='合成模型：明确对应血常规已返回',references=[dict(source_id=labs[0]['id'],quote=labs[0]['text'])])
        ids=[r.source_id for r in result.references]
        common={'rationale':'依据本次读取的记录，等待医生核对后执行','source_ids':ids,'due_text':''}
        if result.state=='RESULT_AVAILABLE':result.actions=[ActionCandidate(action='CREATE_FOLLOWUP',purpose='REVIEW_RESULT',title='查看血常规结果并记录处理',**common)]
        elif result.state=='RESPONSE_RECORDED':result.actions=[ActionCandidate(action='COMPLETE_MATTER',title='核对执行记录后确认完成',**common)]
        elif result.state=='PLAN_CHANGED':result.actions=[ActionCandidate(action='REVISE_PLAN',title='明天下午联系家属核实',**common)]
        elif snapshot['past_due']:result.actions=[ActionCandidate(action='RESCHEDULE',title='请医生重新安排跟进时间',**common)]
        return result


def proposals(http,item):return http.get(f"/api/agent/matters/{item['id']}/actions").json()
def approve(http,proposal,**changes):
    payload={'doctor_id':1,'expected_version':proposal['version'],'expected_matter_version':proposal['expected_matter_version'],
             'decision':'APPROVE','request_key':'action-'+str(proposal['id']),'note':'医生已核对原文和当前依据，同意此操作'}
    payload.update(changes)
    return http.post(f"/api/agent/matter-actions/{proposal['id']}/review",json=payload)


def test_approved_followup_is_linked_idempotent_and_does_not_close_parent(client,model):
    http,engine=client;item=setup_matter(http,engine,'RECHECK');lab(engine)
    run(engine,ActionTestModel());proposal=proposals(http,item)[0]
    assert proposal['status']=='PROPOSED'
    with Session(engine) as db:assert db.scalar(select(func.count()).select_from(MatterLink))==0
    due=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()
    result=approve(http,proposal,due_at=due)
    assert result.status_code==200,result.text
    child_id=result.json()['outcome']['child_id']
    assert approve(http,proposal,due_at=due).json()['outcome']['child_id']==child_id
    assert approve(http,proposal,request_key='another-click').status_code==409
    data=list_data(http)['items'];parent=next(i for i in data if i['id']==item['id']);child=next(i for i in data if i['id']==child_id)
    assert parent['status']=='OPEN' and child['status']=='OPEN' and child['parent_id']==parent['id']
    assert child['source']['kind']=='LAB' and child['assigned_doctor_id']==1
    assert parent['children'][0]['id']==child_id
    assert len(http.get('/api/agent/matter-tasks').json())==2
    with Session(engine) as db:assert db.scalar(select(func.count()).select_from(MatterLink))==1


def test_stale_report_or_matter_change_blocks_approval(client,model):
    http,engine=client;item=setup_matter(http,engine,'RECHECK');lab(engine,1)
    run(engine,ActionTestModel());proposal=proposals(http,item)[0]
    lab(engine,2,text='新的修订报告')
    assert proposals(http,item)[0]['stale']
    assert approve(http,proposal).status_code==409
    run(engine,ActionTestModel());proposal=proposals(http,item)[0]
    current=next(i for i in list_data(http)['items'] if i['id']==item['id'])
    action(http,current,'UPDATE',title='医生另行修改的计划')
    assert approve(http,proposal).status_code==409


def test_completion_requires_response_and_doctor_explicit_approval(client,model):
    http,engine=client;item=setup_matter(http,engine)
    add_note(engine,'医生：已向女儿核实药物过敏史，并记录具体情况。')
    run(engine,ActionTestModel());proposal=proposals(http,item)[0]
    assert next(i for i in list_data(http)['items'] if i['id']==item['id'])['status']=='OPEN'
    assert approve(http,proposal,note=' ').status_code==422
    assert approve(http,proposal,doctor_id=2).status_code==403
    result=approve(http,proposal,note='已核对家属核实记录，确认完成')
    assert result.status_code==200,result.text
    assert result.json()['outcome']['status']=='COMPLETED'
    assert next(i for i in list_data(http)['items'] if i['id']==item['id'])['status']=='COMPLETED'


def test_reschedule_requires_doctor_time_and_reject_has_no_effect(client,model):
    http,engine=client;item=setup_matter(http,engine)
    with Session(engine) as db:
        row=db.get(PatientMatter,item['id']);row.due_at=datetime.now(timezone.utc)-timedelta(hours=1);db.commit()
    run(engine,ActionTestModel());proposal=proposals(http,item)[0]
    assert proposal['content']['action']=='RESCHEDULE'
    assert approve(http,proposal).status_code==422
    due=(datetime.now(timezone.utc)+timedelta(hours=2)).isoformat()
    result=approve(http,proposal,due_at=due)
    assert result.status_code==200,result.text
    assert result.json()['outcome']['due_at']==due
    run(engine,ActionTestModel())
    assert not [p for p in proposals(http,item) if p['status']=='PROPOSED' and not p['stale']]


def test_plan_revision_is_applied_only_after_approval(client,model):
    http,engine=client;item=setup_matter(http,engine)
    add_note(engine,'医生：原计划改为明天下午联系家属。')
    run(engine,ActionTestModel());proposal=proposals(http,item)[0]
    assert next(i for i in list_data(http)['items'] if i['id']==item['id'])['title']==item['title']
    result=approve(http,proposal,title='医生审定后的联系家属计划')
    assert result.status_code==200,result.text
    assert next(i for i in list_data(http)['items'] if i['id']==item['id'])['title']=='医生审定后的联系家属计划'


def test_reject_and_unsupported_suggestions_never_mutate_plan(client,model):
    http,engine=client;item=setup_matter(http,engine)
    class Bad(ActionTestModel):
        def observe(self,snapshot):
            result=super().observe(snapshot)
            result.actions=[ActionCandidate(action='CANCEL_PLAN',title='错误取消',rationale='无依据',source_ids=[])]
            return result
    run(engine,Bad());proposal=proposals(http,item)[0]
    assert proposal['status']=='BLOCKED'
    assert approve(http,proposal).status_code==409
    add_note(engine,'医生：已向女儿核实药物过敏史。')
    run(engine,ActionTestModel());proposal=proposals(http,item)[0]
    rejected=approve(http,proposal,decision='REJECT',note='医生暂不采纳')
    assert rejected.status_code==200,rejected.text
    assert rejected.json()['status']=='REJECTED'
    assert next(i for i in list_data(http)['items'] if i['id']==item['id'])['status']=='OPEN'


def test_handoff_includes_memory_actions_and_refuses_changed_or_omitted_matters(client,model):
    http,engine=client;item=setup_matter(http,engine,'RECHECK');lab(engine)
    run(engine,ActionTestModel());proposal=proposals(http,item)[0]
    report=http.post('/api/encounters/1/handoffs',json={'doctor_id':1}).json()
    assert f"M#{item['id']}" in report['content'] and f"A#{proposal['id']}" in report['content']
    assert any('matter_id' in ref for ref in report['references'])
    confirm={'doctor_id':1,'expected_version':1,'request_key':'handoff-1'}
    assert http.post(f"/api/agent/handoffs/{report['id']}/confirm",json=confirm).status_code==200
    new_report=http.post('/api/encounters/1/handoffs',json={'doctor_id':1}).json()
    approve(http,proposal)
    assert http.post(f"/api/agent/handoffs/{new_report['id']}/confirm",json=confirm).status_code==409
    updated=http.post('/api/encounters/1/handoffs',json={'doctor_id':1}).json()
    assert '前序事项' in updated['content']
    revision=http.post(f"/api/agent/handoffs/{updated['id']}/revisions",json={
        'doctor_id':1,'expected_version':1,'content':'医生误删了事项标识'}).json()
    assert http.post(f"/api/agent/handoffs/{revision['id']}/confirm",json={**confirm,'expected_version':2}).status_code==409


def test_explicit_cancellation_requires_doctor_approval(client,model):
    http,engine=client;item=setup_matter(http,engine,'CONTACT')
    add_note(engine,'医生：取消原定心内科会诊，停止当前联系计划。')
    class Cancel(ActionTestModel):
        def observe(self,snapshot):
            from agent.patient_memory.progress.contracts import Observation
            source=next(s for s in snapshot['sources'] if '取消原定' in s['text'])
            return Observation(state='PLAN_CHANGED',summary='新记录取消原计划',references=[dict(source_id=source['id'],quote=source['text'])],
                actions=[ActionCandidate(action='CANCEL_PLAN',title='请医生核对取消原会诊事项',rationale='医生新记录明确取消',source_ids=[source['id']])])
    run(engine,Cancel());proposal=proposals(http,item)[0]
    assert proposal['status']=='PROPOSED'
    assert next(i for i in list_data(http)['items'] if i['id']==item['id'])['status']=='OPEN'
    assert approve(http,proposal).json()['outcome']['status']=='CANCELLED'


def test_followup_depth_limit_blocks_recursive_creation(client,model):
    http,engine=client;item=setup_matter(http,engine,'RECHECK');lab(engine)
    run(engine,ActionTestModel());parent_proposal=proposals(http,item)[0]
    child_id=approve(http,parent_proposal).json()['outcome']['child_id']
    from agent.patient_memory.actions.service import validation_error
    from agent.patient_memory.progress.sources import build_snapshot
    from agent.patient_memory.progress.contracts import Observation
    with Session(engine) as db:
        snapshot,_=build_snapshot(db,db.get(PatientMatter,child_id))
    evidence=next(s for s in snapshot['sources'] if s['kind']=='LAB')
    observation=Observation(state='RESULT_AVAILABLE',summary='模型重复看到同一报告',references=[dict(source_id=evidence['id'],quote=evidence['text'])])
    candidate=ActionCandidate(action='CREATE_FOLLOWUP',purpose='REVIEW_RESULT',title='重复创建',rationale='错误建议',source_ids=[evidence['id']])
    assert validation_error(candidate,observation,snapshot)=='FOLLOWUP_DEPTH_LIMIT'


def test_handoff_rejects_new_report_even_before_worker_rechecks(client,model):
    http,engine=client;setup_matter(http,engine,'RECHECK')
    report=http.post('/api/encounters/1/handoffs',json={'doctor_id':1}).json()
    lab(engine)
    result=http.post(f"/api/agent/handoffs/{report['id']}/confirm",json={'doctor_id':1,'expected_version':1,'request_key':'new-lab'})
    assert result.status_code==409
