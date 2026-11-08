"""Quality admission, explicit physician clarification and scoped duplicate decisions."""
from datetime import datetime,timezone
import importlib
import pytest
from sqlalchemy import select,create_engine,text
from sqlalchemy.orm import Session
from alembic.migration import MigrationContext
from alembic.operations import Operations
from test_rounding_workflow import client
from test_patient_matters import model,add_source,list_data,scan,action,TestMatterModel,TEXT
from backend.db import Base
from backend.models import RoundingSession
from agent.patient_memory import service
from agent.patient_memory.models import PatientMatter,MatterScan
from agent.patient_memory.contracts import Extraction


def proposal(quote,**changes):
    return dict(kind='RECHECK',title='复查血常规',quote=quote,due_text='',classification='ACTIONABLE',
        action_text='复查',target_text='血常规',evidence_kind='LAB_REPORT',**changes)


def inject(engine,monkeypatch,text,item,decision='KEEP',duplicate_ids=None):
    with Session(engine) as db:
        row=RoundingSession(encounter_id=1,doctor_id=1,started_at=datetime.now(timezone.utc),
            transcript=text,record_mode='TRANSCRIPT',status='COMPLETED')
        db.add(row);db.commit()
    class Provider:
        model='quality-fixture'
        def extract(self,*args):return {'items':[item]}
        def review(self,*args):return {'assessments':[dict(index=0,decision=decision,reason='固定质量审核案例',duplicate_ids=duplicate_ids or [])]}
    monkeypatch.setattr(service,'configured',lambda:True)
    monkeypatch.setattr(service,'get_provider',lambda:Provider())


@pytest.mark.parametrize('quote,expected',[
    ('医生：明天复查血常规。','KEEP'),
    ('医生：暂不复查血常规。','SKIP'),
    ('医生：已经复查血常规。','SKIP'),
    ('患者：我想复查血常规。','CLARIFY'),
])
def test_backend_gates_even_when_model_incorrectly_says_keep(client,model,monkeypatch,quote,expected):
    http,engine=client;inject(engine,monkeypatch,quote,proposal(quote))
    response=scan(http,list_data(http)['sources'][0]);assert response.status_code==200,response.text
    items=list_data(http)['items']
    if expected=='SKIP':
        assert items==[] and response.json()['skipped_count']==1
        assert list_data(http)['scans'][0]['quality_report']['items'][0]['quality']['admission']=='SKIP'
    else:
        assert items[0]['quality']['admission']==expected
        accepted=action(http,items[0],'CONFIRM')
        assert accepted.status_code==(200 if expected=='KEEP' else 422)


def test_vague_plan_needs_explicit_physician_supplement_and_keeps_original(client,model,monkeypatch):
    http,engine=client
    quote='医生：完善检查。'
    item=proposal(quote);item.update(title='完善检查',action_text='完善',target_text='检查')
    inject(engine,monkeypatch,quote,item)
    assert scan(http,list_data(http)['sources'][0]).status_code==200
    item=list_data(http)['items'][0]
    assert item['quality']['admission']=='CLARIFY'
    assert action(http,item,'CONFIRM').status_code==422
    fields=dict(action_text='复查',target_text='血常规',evidence_kind='LAB_REPORT',
        clarification_note='医生已核实并明确本次复查项目为血常规。',title='复查血常规')
    bad={**fields,'target_text':'检查','title':'复查检查'}
    assert action(http,item,'CONFIRM',**bad).status_code==422
    accepted=action(http,item,'CONFIRM',**fields)
    assert accepted.status_code==200,accepted.text
    data=accepted.json();assert data['status']=='OPEN' and data['quote']==quote
    assert data['source']['text']==quote and data['quality']['clarified_by']==1
    assert data['quality']['clarification_note']==fields['clarification_note']
    assert len(http.get('/api/agent/matter-tasks').json())==1


@pytest.mark.parametrize('classification,review_decision',[
    ('BACKGROUND','KEEP'),('ACTIONABLE','SKIP'),('PATIENT_REQUEST','KEEP'),('CONDITIONAL','KEEP')])
def test_classification_and_independent_review_cannot_be_bypassed(client,model,monkeypatch,classification,review_decision):
    http,engine=client;quote='医生：复查血常规。';item=proposal(quote);item['classification']=classification
    inject(engine,monkeypatch,quote,item,review_decision)
    assert scan(http,list_data(http)['sources'][0]).status_code==200
    items=list_data(http)['items']
    if classification=='BACKGROUND' or review_decision=='SKIP':assert items==[]
    else:assert items[0]['quality']['admission']=='CLARIFY' and action(http,items[0],'CONFIRM').status_code==422


def test_invented_target_and_missing_review_fail_closed(client,model,monkeypatch):
    http,engine=client;quote='医生：复查血常规。';item=proposal(quote);item['target_text']='胸部CT'
    inject(engine,monkeypatch,quote,item)
    assert scan(http,list_data(http)['sources'][0]).status_code==200
    candidate=list_data(http)['items'][0]
    assert candidate['quality']['admission']=='CLARIFY'
    assert action(http,candidate,'CONFIRM').status_code==422
    class Incomplete(TestMatterModel):
        def review(self,*args):return {'assessments':[]}
    add_source(engine);monkeypatch.setattr(service,'get_provider',lambda:Incomplete())
    assert scan(http,list_data(http)['sources'][0]).status_code==502
    assert http.get('/api/agent/matter-tasks').json()==[]


def duplicate_setup(http,engine):
    add_source(engine);scan(http,list_data(http)['sources'][0])
    old=next(i for i in list_data(http)['items'] if i['kind']=='RECHECK')
    old=action(http,old,'CONFIRM').json()
    source_id=add_source(engine);source=next(s for s in list_data(http)['sources'] if s['id']==source_id)
    assert scan(http,source).status_code==200
    new=next(i for i in list_data(http)['items'] if i['kind']=='RECHECK' and i['id']!=old['id'])
    return old,new


def test_duplicate_requires_explicit_decision_and_link_never_changes_original(client,model):
    http,engine=client;old,new=duplicate_setup(http,engine)
    assert any(d['id']==old['id'] for d in new['quality']['duplicates'])
    assert action(http,new,'CONFIRM').status_code==422
    link=dict(duplicate_decision='LINK',duplicate_matter_id=old['id'],duplicate_expected_version=old['version'],note='核对为同一次复查安排，关联已有事项。')
    assert action(http,new,'CONFIRM',**{**link,'duplicate_matter_id':9999}).status_code==409
    changed=action(http,old,'UPDATE',title=old['title']).json()
    assert action(http,new,'CONFIRM',**link).status_code==409
    result=action(http,new,'CONFIRM',**{**link,'duplicate_expected_version':changed['version']})
    assert result.status_code==200,result.text
    assert result.json()['status']=='DISMISSED' and result.json()['quality']['linked_matter_id']==old['id']
    original=next(i for i in list_data(http)['items'] if i['id']==old['id'])
    assert original['status']=='OPEN' and original['version']==changed['version']
    assert len(http.get('/api/agent/matter-tasks').json())==1


def test_physician_may_keep_a_distinct_repeat_with_reason(client,model):
    http,engine=client;old,new=duplicate_setup(http,engine)
    assert action(http,new,'CONFIRM',duplicate_decision='SEPARATE').status_code==422
    separate=action(http,new,'CONFIRM',duplicate_decision='SEPARATE',note='这是另一日期新安排的复查，单独跟进。')
    assert separate.status_code==200 and separate.json()['status']=='OPEN'
    assert len(http.get('/api/agent/matter-tasks').json())==2


def test_model_cannot_reference_an_unknown_duplicate(client,model,monkeypatch):
    http,engine=client;quote='医生：复查血常规。'
    inject(engine,monkeypatch,quote,proposal(quote),duplicate_ids=[999])
    assert scan(http,list_data(http)['sources'][0]).status_code==502
    assert list_data(http)['items']==[]


def test_reassessment_preserves_accepted_arrangements_and_marks_legacy_candidates(client,model,monkeypatch):
    http,engine=client;add_source(engine);source=list_data(http)['sources'][0];scan(http,source)
    item=list_data(http)['items'][0];opened=action(http,item,'CONFIRM').json()
    with Session(engine) as db:
        scan_row=db.scalar(select(MatterScan));scan_row.policy_version='legacy'
        candidate=db.scalar(select(PatientMatter).where(PatientMatter.status=='CANDIDATE'));candidate.quality={};db.commit()
    legacy=next(i for i in list_data(http)['items'] if i['quality'].get('policy_version')=='legacy')
    assert action(http,legacy,'CONFIRM').status_code==422
    class Background(TestMatterModel):
        def extract(self,*args):
            result=super().extract(*args)
            for p in result.items:p.classification='BACKGROUND'
            return result
    monkeypatch.setattr(service,'get_provider',lambda:Background())
    assert scan(http,source).status_code==200
    preserved=next(i for i in list_data(http)['items'] if i['id']==opened['id'])
    assert preserved['status']=='OPEN' and preserved['version']==opened['version'] and preserved['quality']==opened['quality']


def test_quality_migration_is_additive_and_repeatable():
    engine=create_engine('sqlite://');Base.metadata.create_all(engine)
    with engine.begin() as connection:
        for table,column in [('patient_matters','quality'),('patient_matter_scans','policy_version'),('patient_matter_scans','quality_report')]:
            connection.execute(text(f'ALTER TABLE {table} DROP COLUMN {column}'))
        migration=importlib.import_module('backend.alembic.versions.0013_extraction_quality')
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade();migration.upgrade()
        columns={c['name'] for c in __import__('sqlalchemy').inspect(connection).get_columns('patient_matters')}
        assert 'quality' in columns
    engine.dispose()
