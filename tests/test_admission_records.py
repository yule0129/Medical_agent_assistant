from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from test_rounding_workflow import client
from backend.models import Encounter, MedicalRecord
from backend.services.admission_service import admission_tasks


def test_admission_deadline_boundary_and_future_discharge(client):
    _, engine = client
    now = datetime(2026, 10, 26, 4, tzinfo=timezone.utc)
    with Session(engine) as db:
        encounter = db.get(Encounter, 1)
        encounter.admitted_at = now - timedelta(hours=23, minutes=59)
        db.commit()
        task = admission_tasks(db, 1, now)[0]
        assert task['status'] == 'WAITING_EVENT' and not task['overdue']
        assert datetime.fromisoformat(task['due_at']) == now + timedelta(minutes=1)
        encounter.admitted_at = now - timedelta(hours=24)
        db.commit()
        assert admission_tasks(db, 1, now)[0]['overdue']
        encounter.admitted_at = now - timedelta(days=2)
        db.commit()
        assert admission_tasks(db, 1, now)[0]['overdue']
        encounter.admitted_at = now + timedelta(seconds=1)
        db.commit()
        assert not admission_tasks(db, 1, now)
        encounter.admitted_at = now - timedelta(hours=1)
        encounter.status = 'DISCHARGED'
        db.commit()
        assert not admission_tasks(db, 1, now)


def test_admission_save_keeps_review_task_pending_and_preserves_history(client):
    http, engine = client
    path = '/api/patients/1/admission-record'
    initial = http.get(path+'?encounter_id=1').json()
    assert initial['record'] is None and initial['reminder_needed']
    tasks = http.get('/api/admission-tasks').json()
    assert len(tasks) == 1 and tasks[0]['id'] == 'admission-1'
    payload = dict(doctor_id=1, encounter_id=1, content='主诉：合成测试正文', expected_revision=initial['revision'])
    result = http.put(path, json=payload)
    assert result.status_code == 200, result.text
    saved = result.json()
    assert saved['written'] and saved['reminder_needed']
    assert saved['record']['status'] == '待审核'
    assert http.get('/api/admission-tasks').json()[0]['status'] == 'WAITING_EVENT'
    # Refreshes never generate duplicate reminders or records; stale edits are rejected.
    assert http.put(path, json=payload).status_code == 409
    payload['expected_revision'] = saved['revision']
    payload['content'] = '医生修订的合成测试正文'
    updated = http.put(path, json=payload).json()
    assert updated['record']['id'] == saved['record']['id']
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(MedicalRecord)) == 1
    history = http.get('/api/patients/1/history').json()
    assert history['records'][0]['record_type'] == '入院记录'


def test_only_nonempty_admission_for_same_encounter_suppresses_reminder(client):
    http, engine = client
    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        db.add(Encounter(id=2, patient_id=1, encounter_number='OLD', attending_doctor_id=1,
                         admitted_at=now-timedelta(days=20), status='DISCHARGED'))
        db.add(MedicalRecord(record_number='old-admission', patient_id=1, encounter_id=2,
                             doctor_id=1, record_type='入院记录', content='上次住院', created_at=now))
        db.add(MedicalRecord(record_number='daily', patient_id=1, encounter_id=1,
                             doctor_id=1, record_type='日常病程记录', content='不是入院记录', created_at=now))
        db.add(MedicalRecord(record_number='blank', patient_id=1, encounter_id=1,
                             doctor_id=1, record_type='再次或多次入院记录', content=' \n ', created_at=now))
        db.commit()
    assert http.get('/api/admission-tasks').json()[0]['status'] == 'WAITING_EVENT'
    initial = http.get('/api/patients/1/admission-record?encounter_id=1').json()
    payload = dict(doctor_id=1, encounter_id=1, content='再入院正文', expected_revision=initial['revision'])
    saved = http.put('/api/patients/1/admission-record', json=payload).json()
    assert saved['written'] and saved['record']['record_type'] == '再次或多次入院记录'


def test_admission_scope_empty_input_and_signed_lock(client):
    http, _ = client
    path = '/api/patients/1/admission-record'
    assert http.get(path+'?encounter_id=1&doctor_id=2').status_code == 403
    assert http.get('/api/patients/2/admission-record?encounter_id=1').status_code == 404
    assert http.get('/api/admission-tasks?doctor_id=2').json() == []
    payload = dict(doctor_id=1, encounter_id=1, content=' \n ', expected_revision='empty')
    assert http.put(path, json=payload).status_code == 422
    payload['doctor_id'] = 2
    payload['content'] = '越权正文'
    assert http.put(path, json=payload).status_code == 403
    payload['doctor_id'] = 1
    record = http.put(path, json=payload).json()['record']
    http.post(f"/api/medical-records/{record['id']}/sign")
    state = http.get(path+'?encounter_id=1').json()
    payload['expected_revision'] = state['revision']
    assert http.put(path, json=payload).status_code == 409
