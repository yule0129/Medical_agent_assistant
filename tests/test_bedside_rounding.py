from datetime import datetime, timedelta, timezone
import importlib
from sqlalchemy import select, func, text, create_engine
from sqlalchemy.orm import Session
from alembic.migration import MigrationContext
from alembic.operations import Operations
from test_rounding_workflow import client
from backend.models import Encounter, RoundingSession, MedicalRecord, ClinicalEvent, ClinicalIntent, AgentRun
from backend.services.rounding_service import rounding_tasks


def setup_time(engine):
    now=datetime.now(timezone.utc)
    with Session(engine) as db:
        db.get(Encounter,1).admitted_at=now-timedelta(hours=1);db.commit()
    return now


def payload(now,**changes):
    result=dict(doctor_id=1,patient_id=1,encounter_id=1,transcript='医生：今天感觉如何？患者：咳嗽减轻。',
                started_at=now.isoformat(),request_key='bedside-1',transcript_source='manual')
    result.update(changes)
    return result


def test_first_rounding_deadline_scope_and_no_other_admission_completion(client):
    _,engine=client
    now=datetime.now(timezone.utc)
    with Session(engine) as db:
        encounter=db.get(Encounter,1);encounter.admitted_at=now-timedelta(hours=47,minutes=59);db.commit()
        tasks=rounding_tasks(db,1,now)
        assert len(tasks)==1 and not tasks[0]['overdue']
        assert datetime.fromisoformat(tasks[0]['due_at'])==now+timedelta(minutes=1)
        assert not rounding_tasks(db,2,now)
        encounter.admitted_at=now-timedelta(hours=48);db.commit()
        assert rounding_tasks(db,1,now)[0]['overdue']
        db.add(Encounter(id=2,patient_id=1,encounter_number='OLD',attending_doctor_id=1,
                         admitted_at=now-timedelta(days=20),status='DISCHARGED'))
        db.add(RoundingSession(encounter_id=2,doctor_id=1,started_at=now-timedelta(days=19),
                              ended_at=now-timedelta(days=19),transcript='上次住院',status='COMPLETED'))
        db.commit()
        assert rounding_tasks(db,1,now)[0]['status']=='WAITING_EVENT'
        encounter.status='DISCHARGED';db.commit()
        assert not rounding_tasks(db,1,now)
        encounter.status='INPATIENT';encounter.admitted_at=now+timedelta(seconds=1);db.commit()
        assert not rounding_tasks(db,1,now)


def test_transcript_save_edit_complete_without_note_event_or_agent(client):
    http,engine=client
    now=setup_time(engine)
    data=payload(now)
    response=http.post('/api/bedside-roundings',json=data)
    assert response.status_code==200,response.text
    row=response.json()
    assert row['status']=='DRAFT' and row['record_id'] is None
    assert datetime.fromisoformat(row['started_at']).utcoffset() is not None
    assert http.get('/api/rounding-tasks').json()[0]['status']=='WAITING_EVENT'
    replay=http.post('/api/bedside-roundings',json=data).json()
    assert replay['id']==row['id']
    changed={**data,'transcript':'医生核对修改后的查房问诊','expected_version':row['version']}
    updated=http.put(f"/api/bedside-roundings/{row['id']}",json=changed).json()
    assert updated['version']==row['version']+1
    assert http.put(f"/api/bedside-roundings/{row['id']}",json=changed).status_code==409
    finish={'doctor_id':1,'expected_version':updated['version']}
    completed=http.post(f"/api/bedside-roundings/{row['id']}/complete",json=finish)
    assert completed.status_code==200,completed.text
    assert completed.json()['status']=='COMPLETED'
    assert http.post(f"/api/bedside-roundings/{row['id']}/complete",json=finish).status_code==200
    assert http.get('/api/rounding-tasks').json()[0]['status']=='RESOLVED'
    assert http.put(f"/api/bedside-roundings/{row['id']}",json={**changed,'expected_version':completed.json()['version']}).status_code==409
    history=http.get('/api/patients/1/history').json()
    assert len(history['roundings'])==1 and history['roundings'][0]['record_mode']=='TRANSCRIPT'
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(RoundingSession))==1
        assert db.get(RoundingSession,row['id']).generated_note is None
        for model in (MedicalRecord,ClinicalEvent,ClinicalIntent,AgentRun):
            assert db.scalar(select(func.count()).select_from(model))==0


def test_bedside_validation_and_repeated_visits(client):
    http,engine=client;now=setup_time(engine)
    assert http.post('/api/bedside-roundings',json=payload(now,transcript='  ')).status_code==422
    assert http.post('/api/bedside-roundings',json=payload(now,doctor_id=2)).status_code==403
    assert http.post('/api/bedside-roundings',json=payload(now,patient_id=2)).status_code==404
    assert http.post('/api/bedside-roundings',json=payload(now,started_at=(now+timedelta(days=1)).isoformat())).status_code==422
    assert http.post('/api/bedside-roundings',json=payload(now,started_at=(now-timedelta(days=1)).isoformat())).status_code==422
    first=http.post('/api/bedside-roundings',json=payload(now)).json()
    assert http.post(f"/api/rounding-sessions/{first['id']}/finish",json={'doctor_id':1}).status_code==409
    assert http.post(f"/api/bedside-roundings/{first['id']}/complete",json={'doctor_id':1,'expected_version':99}).status_code==409
    assert http.post(f"/api/bedside-roundings/{first['id']}/complete",json={'doctor_id':2,'expected_version':1}).status_code==403
    http.post(f"/api/bedside-roundings/{first['id']}/complete",json={'doctor_id':1,'expected_version':1})
    second=http.post('/api/bedside-roundings',json=payload(now,request_key='second',transcript='第二次查房问诊')).json()
    assert second['id']!=first['id']
    tasks=http.get('/api/rounding-tasks').json()
    assert len(tasks)==1 and tasks[0]['status']=='RESOLVED'
    assert len(http.get('/api/patients/1/history').json()['roundings'])==2


def test_existing_completed_rounding_satisfies_first_visit_and_marks_late(client):
    _,engine=client;now=datetime.now(timezone.utc)
    with Session(engine) as db:
        db.get(Encounter,1).admitted_at=now-timedelta(days=3)
        db.add(RoundingSession(encounter_id=1,doctor_id=1,started_at=now-timedelta(hours=1),ended_at=now,
                              transcript='原有工作流记录',status='COMPLETED'))
        db.commit()
        task=rounding_tasks(db,1,now)[0]
        assert task['status']=='RESOLVED' and task['late'] and not task['overdue']


def test_migration_preserves_existing_rounding_and_is_repeatable():
    engine=create_engine('sqlite://')
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE rounding_sessions (id INTEGER PRIMARY KEY, transcript TEXT)'))
        conn.execute(text("INSERT INTO rounding_sessions VALUES (1,'existing transcript')"))
        module=importlib.import_module('backend.alembic.versions.0008_bedside_rounding')
        with Operations.context(MigrationContext.configure(conn)):
            module.upgrade();module.upgrade()
        row=conn.execute(text('SELECT transcript, record_mode, version FROM rounding_sessions')).one()
        assert tuple(row)==('existing transcript','WORKFLOW',1)
    engine.dispose()
