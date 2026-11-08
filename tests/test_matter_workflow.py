from copy import deepcopy
from datetime import datetime, timedelta, timezone
import pytest
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from test_patient_matters import client, model, add_source, list_data, scan, action
from test_matter_progress import setup_matter, run, lab, progress, ProgressTestModel
from backend.models import AuditLog
from agent.patient_memory.models import PatientMatter
from agent.patient_memory.progress.contracts import Observation
from agent.patient_memory.progress.runtime import validate
from agent.patient_memory.evaluation import score, GOLD
from agent.admission.contracts import DraftError


def test_patient_summary_and_contract_do_not_complete_on_report_or_ack(client, model):
    http, engine = client
    item = setup_matter(http, engine, 'RECHECK')
    assert '医生审阅' in item['completion_contract']['required']
    lab(engine); run(engine, ProgressTestModel())
    feed=http.get('/api/agent/matter-progress-feedback')
    assert feed.status_code==200,feed.text
    assert feed.json()['summaries']['1']['groups']['needs_review'][0]['id']==item['id']
    data = list_data(http)
    entry = data['work_summary']['groups']['needs_review'][0]
    assert entry['id'] == item['id'] and entry['evidence'][0]['label'] == '血常规'
    assert action(http, item, 'COMPLETE', note='已处理报告').status_code == 422
    latest = progress(http, item)
    ack = http.post(f"/api/agent/matters/{item['id']}/progress/acknowledge", json={
        'doctor_id':1, 'expected_version':item['version'], 'result_id':latest['result_id']})
    assert ack.status_code == 200
    data = list_data(http)
    assert next(i for i in data['items'] if i['id']==item['id'])['status'] == 'OPEN'
    assert '填写报告处理结论' in data['work_summary']['groups']['needs_review'][0]['next_step']
    done = action(http, item, 'COMPLETE', note='医生审阅并记录实际结论，已交接后续安排')
    assert done.status_code == 200, done.text
    assert all(i['id'] != item['id'] for group in list_data(http)['work_summary']['groups'].values() for i in group)
    with Session(engine) as db:
        audit = db.scalar(select(AuditLog).where(AuditLog.action=='MATTER_COMPLETE'))
        assert audit.new_state['completion_evidence']['result_id'] == latest['result_id']


def test_complete_with_explicit_current_report_review_and_reject_stale_result(client, model):
    http, engine = client; item = setup_matter(http, engine, 'RECHECK')
    lab(engine); run(engine, ProgressTestModel()); old = progress(http, item)
    lab(engine, 2); run(engine, ProgressTestModel()); latest = progress(http, item)
    assert action(http,item,'COMPLETE',note='已审阅',reviewed_result_id=old['result_id']).status_code==409
    response=action(http,item,'COMPLETE',note='已审阅当前报告，记录处理结论',reviewed_result_id=latest['result_id'])
    assert response.status_code==200,response.text
    assert response.json()['status']=='COMPLETED'


def test_later_round_link_is_in_original_timeline_without_second_open_task(client, model):
    http, engine = client; old = setup_matter(http, engine, 'RECHECK')
    source_id = add_source(engine)
    source = next(s for s in list_data(http)['sources'] if s['id']==source_id)
    scan(http, source)
    candidate = next(i for i in list_data(http)['items'] if i['kind']=='RECHECK' and i['status']=='CANDIDATE')
    response=action(http,candidate,'CONFIRM',duplicate_decision='LINK',duplicate_matter_id=old['id'],
        duplicate_expected_version=old['version'],note='再次查房确认继续跟进同一安排')
    assert response.status_code==200,response.text
    timeline=http.get(f"/api/agent/matters/{old['id']}/timeline").json()['items']
    linked=next(e for e in timeline if e['type']=='LINKED_NOTE')
    assert linked['source']['id']==source_id and linked['quote']==candidate['quote']
    assert linked['note']=='再次查房确认继续跟进同一安排'
    assert sum(i['status']=='OPEN' for i in list_data(http)['items'])==1
    assert http.get(f"/api/agent/matters/{old['id']}/timeline?doctor_id=2").status_code==403
    assert list_data(http)['quality_feedback']['reasons']['DUPLICATE']==1


def test_feedback_is_scoped_versioned_idempotent_and_preserves_plan(client, model):
    http, engine = client; item=setup_matter(http, engine)
    url=f"/api/agent/matters/{item['id']}/quality-feedback"
    payload=dict(doctor_id=1,expected_version=item['version'],reason='WRONG_EVIDENCE',note='引用的是既往资料，需要核对本次记录')
    assert http.post(url,json={**payload,'doctor_id':2}).status_code==403
    assert http.post(url,json={**payload,'expected_version':99}).status_code==409
    assert http.post(url,json={**payload,'note':'  '}).status_code==422
    assert http.post(url,json=payload).status_code==200
    assert http.post(url,json=payload).status_code==200
    data=list_data(http)
    assert data['quality_feedback']['total']==1
    unchanged=next(i for i in data['items'] if i['id']==item['id'])
    assert (unchanged['status'],unchanged['title'],unchanged['version'])==(item['status'],item['title'],item['version'])
    assert http.get('/api/agent/encounters/1/matters?doctor_id=2').status_code==403


def test_doctor_revision_records_feedback_and_stops_matching_previous_target(client, model):
    http, engine=client; item=setup_matter(http, engine,'RECHECK')
    revised=action(http,item,'UPDATE',title='复查肝功能',note='新查房明确调整检查项目').json()
    assert revised['quality']['target_text']==''
    assert revised['quality']['prior_plan']['target_text']=='血常规'
    assert list_data(http)['quality_feedback']['reasons']['PLAN_CHANGED']==1
    timeline=http.get(f"/api/agent/matters/{item['id']}/timeline").json()['items']
    change=next(e for e in timeline if e['type']=='MATTER_UPDATE')
    assert change['before']['title']=='复查血常规' and change['after']['title']=='复查肝功能'
    lab(engine);run(engine,ProgressTestModel())
    assert progress(http,item)['state']=='UNCLEAR'


def test_wrong_report_in_adjacent_original_text_is_not_current_plan_evidence():
    snapshot={'matter':{'kind':'RECHECK','title':'复查肝功能','quote':'复查肝功能。血常规已经复查。',
        'original_source':{'at':'2026-11-01T00:00:00+00:00'},'clarified_plan':{'target_text':'肝功能'}},
        'sources':[dict(id='lab',kind='LAB',label='血常规',text='合成结果',at='2026-11-02T00:00:00+00:00',is_origin=False)],
        'coverage':{'complete':True}}
    observation=Observation(state='RESULT_AVAILABLE',summary='结果已到',references=[dict(source_id='lab',quote='合成结果')])
    validate(snapshot,observation)
    assert observation.state=='UNCLEAR'


def test_old_note_cannot_prove_execution_of_later_plan():
    snapshot={'matter':{'original_source':{'at':'2026-11-03T00:00:00+00:00'}},
        'sources':[dict(id='old',kind='NOTE',text='已经联系会诊',at='2026-11-01T00:00:00+00:00',is_origin=False)],
        'coverage':{'complete':True}}
    observation=Observation(state='RESPONSE_RECORDED',summary='已经执行',references=[dict(source_id='old',quote='已经联系会诊')])
    with pytest.raises(DraftError,match='PROGRESS_PREDATES_PLAN'):validate(snapshot,observation)


def test_revised_plan_does_not_match_report_from_before_revision():
    snapshot={'matter':{'kind':'RECHECK','title':'复查肝功能','quote':'复查肝功能',
        'original_source':{'at':'2026-11-01T00:00:00+00:00'},
        'plan_started_at':'2026-11-03T00:00:00+00:00'},
        'sources':[dict(id='lab',kind='LAB',label='肝功能',text='旧合成结果',at='2026-11-02T00:00:00+00:00',is_origin=False)],
        'coverage':{'complete':True}}
    observation=Observation(state='RESULT_AVAILABLE',summary='已返回',references=[dict(source_id='lab',quote='旧合成结果')])
    validate(snapshot,observation)
    assert observation.state=='UNCLEAR'


def test_overdue_summary_is_scoped_and_closed_items_disappear(client,model):
    http,engine=client;item=setup_matter(http,engine,'CONTACT')
    due=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()
    item=action(http,item,'UPDATE',due_at=due).json()
    assert [i['id'] for i in list_data(http)['work_summary']['groups']['overdue']]==[item['id']]
    assert http.get('/api/agent/matter-progress-feedback?doctor_id=2').json()['summaries']=={}
    assert action(http,item,'CANCEL',note='医生确认取消该安排').status_code==200
    assert list_data(http)['work_summary']['groups']['overdue']==[]


def synthetic_cases():
    cases=[]
    for name, labels in GOLD.items():
        rows=[]
        for action_text,target_text,admission in labels:
            quote=action_text+target_text+'。'
            rows.append({'proposal':dict(action_text=action_text,target_text=target_text,quote=quote),
                'quality':dict(admission=admission,duplicate_ids=[7] if name=='duplicate' else [])})
        cases.append({'case':name,'rows':rows,'source':{'text':''.join(r['proposal']['quote'] for r in rows)}})
    return cases


def test_evaluation_detects_omission_extra_duplicate_and_bad_grounding():
    cases=synthetic_cases(); assert score(cases)['precision']==score(cases)['recall']==1
    broken=deepcopy(cases)
    broken[0]['rows'].pop()
    broken[1]['rows'].append(deepcopy(broken[1]['rows'][0]))
    result=score(broken)
    assert result['false_positive']==1 and result['false_negative']==1
    broken=deepcopy(cases);broken[0]['source']['text']='没有引用资料'
    assert score(broken)['evidence_grounding_rate']<1
    broken=deepcopy(cases);broken[-1]['rows'][0]['quality']['duplicate_ids']=[]
    assert score(broken)['duplicate_detection_rate']==0
    with pytest.raises(ValueError):score(cases[:-1])
