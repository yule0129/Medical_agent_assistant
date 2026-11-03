from datetime import datetime,timedelta,timezone
import importlib
from sqlalchemy import select,func,create_engine,text
from sqlalchemy.orm import Session
from alembic.migration import MigrationContext
from alembic.operations import Operations
from test_rounding_workflow import client
from test_patient_matters import model,TEXT
from test_bedside_rounding import setup_time,payload
from agent.patient_memory import automatic,service
from agent.patient_memory.models import MatterScan
from backend.models import Encounter
from backend.db import Base


def run(engine):
    with Session(engine,autoflush=False) as db:return automatic.process_once(db)


def save_interview(http,text=TEXT,version=0):
    return http.put('/api/patients/1/admission-inputs',json=dict(doctor_id=1,encounter_id=1,
        transcript=text,examination_text='',expected_input_version=version))


def test_saved_interview_queues_once_and_candidates_require_confirmation(client,model):
    http,engine=client
    saved=save_interview(http)
    assert saved.status_code==200
    assert model.calls==0
    assert save_interview(http,version=saved.json()['agent']['input_version']).status_code==200
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(MatterScan))==1
        assert db.scalar(select(MatterScan)).status=='QUEUED'
    feed=http.get('/api/agent/matter-progress-feedback').json()
    assert feed['extractions'][0]['status']=='QUEUED'
    assert http.get('/api/agent/matter-progress-feedback?doctor_id=999').json()['extractions']==[]
    assert run(engine)['candidate_count']==3
    assert model.calls==1 and http.get('/api/agent/matter-tasks').json()==[]
    feed=http.get('/api/agent/matter-progress-feedback').json()
    assert feed['candidate_count']==3 and feed['extractions'][0]['status']=='READY'
    with Session(engine) as db:automatic.schedule(db)
    assert run(engine) is None and model.calls==1
    item=http.get('/api/agent/encounters/1/matters').json()['items'][0]
    assert http.post(f"/api/agent/matters/{item['id']}/review",json=dict(
        doctor_id=1,expected_version=item['version'],action='CONFIRM')).status_code==200
    assert len(http.get('/api/agent/matter-tasks').json())==1


def test_round_draft_not_extracted_but_completion_is_durable_and_idempotent(client,model):
    http,engine=client;now=setup_time(engine)
    row=http.post('/api/bedside-roundings',json=payload(now,transcript=TEXT)).json()
    with Session(engine) as db:
        automatic.schedule(db)
        assert db.scalar(select(func.count()).select_from(MatterScan))==0
    finish=dict(doctor_id=1,expected_version=row['version'])
    for _ in range(2):assert http.post(f"/api/bedside-roundings/{row['id']}/complete",json=finish).status_code==200
    assert model.calls==0
    with Session(engine) as db:assert db.scalar(select(func.count()).select_from(MatterScan))==1
    assert run(engine)['candidate_count']==3
    assert run(engine) is None and model.calls==1


def test_changed_interview_skips_old_queue_and_discharge_skips_model(client,model):
    http,engine=client
    saved=save_interview(http)
    changed=save_interview(http,TEXT+'新的问诊内容。',saved.json()['agent']['input_version'])
    assert changed.status_code==200
    assert run(engine)['status']=='STALE' and model.calls==0
    assert run(engine)['candidate_count']==3
    assert save_interview(http,TEXT+'再次补充。',changed.json()['agent']['input_version']).status_code==200
    with Session(engine) as db:db.get(Encounter,1).status='DISCHARGED';db.commit()
    assert run(engine)['status']=='STALE' and model.calls==1


def test_failure_retries_are_bounded_and_empty_result_is_visible(client,model,monkeypatch):
    http,engine=client;save_interview(http)
    class Failure:
        def extract(self,*args):raise RuntimeError('private error not exposed')
    monkeypatch.setattr(service,'get_provider',lambda:Failure())
    for attempt in range(1,4):
        assert run(engine)['status']=='FAILED'
        with Session(engine) as db:
            scan=db.scalar(select(MatterScan));assert scan.attempt==attempt
            if attempt<3:
                assert scan.retry_at
                scan.retry_at=datetime.now(timezone.utc)-timedelta(seconds=1);db.commit()
    assert run(engine) is None
    feed=http.get('/api/agent/matter-progress-feedback').json()['extractions'][0]
    assert feed['status']=='FAILED' and feed['retry_at'] is None
    class Empty:
        model='empty-test'
        def extract(self,*args):return {'items':[]}
    monkeypatch.setattr(service,'get_provider',lambda:Empty())
    source=http.get('/api/agent/encounters/1/matters').json()['sources'][0]
    assert http.post('/api/agent/encounters/1/matters/scan',json=dict(doctor_id=1,
        source_kind='ADMISSION',source_id=source['id'],expected_digest=source['digest'])).json()['candidate_count']==0
    feed=http.get('/api/agent/matter-progress-feedback').json()['extractions'][0]
    assert feed['status']=='READY' and feed['total_count']==0


def test_automatic_migration_preserves_existing_scan_and_is_repeatable():
    engine=create_engine('sqlite://');Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text('ALTER TABLE patient_matter_scans DROP COLUMN attempt'))
        connection.execute(text('ALTER TABLE patient_matter_scans DROP COLUMN retry_at'))
        connection.execute(text("INSERT INTO patient_matter_scans (id,scan_key,encounter_id,doctor_id,source_kind,source_id,source_digest,status,created_at) VALUES (1,'existing',1,1,'ADMISSION',1,'digest','READY','2026-01-01')"))
        migration=importlib.import_module('backend.alembic.versions.0012_auto_matter_extraction')
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade();migration.upgrade()
        assert connection.execute(text('SELECT status,attempt,retry_at FROM patient_matter_scans WHERE id=1')).one()==('READY',0,None)
    engine.dispose()


def test_duplicate_model_items_do_not_fail_the_entire_automatic_scan(client,model,monkeypatch):
    http,engine=client;save_interview(http)
    class Duplicate:
        model='duplicate-test'
        def extract(self,source,known):
            item=dict(kind='RECHECK',title='复查血常规',quote='明天复查血常规。',due_text='明天')
            return {'items':[item,item]}
    monkeypatch.setattr(service,'get_provider',lambda:Duplicate())
    assert run(engine)['candidate_count']==1
    assert len(http.get('/api/agent/encounters/1/matters').json()['items'])==1


def test_interrupted_extraction_resumes_without_unlimited_attempts(client,model):
    http,engine=client;save_interview(http)
    with Session(engine) as db:
        scan=db.scalar(select(MatterScan));scan.status='RUNNING';scan.attempt=1
        scan.created_at=datetime.now(timezone.utc)-timedelta(seconds=121);db.commit()
    assert run(engine)['status']=='READY' and model.calls==1
