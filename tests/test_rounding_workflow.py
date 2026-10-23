"""Exercise the real HTTP contract using an isolated database and fresh sessions."""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.db import Base, get_db
from backend.main import app
from backend.models import Doctor, Patient, Encounter, MedicalRecord, ClinicalIntent, OpenLoop, RoundingSession
from backend.providers.rules import extract_intent


@pytest.fixture
def client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        doctor = Doctor(id=1, username="workflow", name="合成医生", department="测试")
        db.add(doctor)
        db.add(Patient(id=1, name="合成患者", admission_number="TEST-1", bed_number="01", diagnosis="测试", doctor_id=1))
        db.flush()
        db.add(Encounter(id=1, encounter_number="TEST-ENC", patient_id=1, attending_doctor_id=1, admitted_at=datetime.now(timezone.utc)))
        db.commit()

    def session():
        # Match production: autoflush=False exposes missing flushes and returned IDs.
        with Session(engine, autoflush=False) as db:
            yield db
    app.dependency_overrides[get_db] = session
    with TestClient(app) as http:
        yield http, engine
    app.dependency_overrides.clear()
    engine.dispose()


def save_round(http, **changes):
    now = datetime.now(timezone.utc).isoformat()
    payload = dict(doctor_id=1, patient_id=1, encounter_id=1, started_at=now, ended_at=now,
                   transcript="明天复查血常规。监测血氧。联系心内科会诊。血培养结果出来后通知我。",
                   generated_note="合成查房正文 <script>文本</script>", request_key="round-test-1")
    payload.update(changes)
    return http.post("/api/rounding-sessions", json=payload)


def test_rounding_record_can_be_read_after_refresh_and_request_retry(client):
    http, engine = client
    response = save_round(http)
    assert response.status_code == 200, response.text
    saved = response.json()
    assert isinstance(saved["record_id"], int)
    record = http.get(f"/api/medical-records/{saved['record_id']}").json()
    assert record["content"] == "合成查房正文 <script>文本</script>"
    assert record["source_rounding_session_id"] == saved["id"]
    assert record["encounter_id"] == 1
    assert any(r["id"] == record["id"] for r in http.get('/api/medical-records').json())
    replay = save_round(http)
    assert replay.json()["record_id"] == record["id"]
    assert save_round(http, transcript="different").status_code == 409
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(RoundingSession)) == 1
        assert db.scalar(select(func.count()).select_from(MedicalRecord)) == 1
    # Draft-only requests used to be able to return a null record ID with autoflush disabled.
    draft = save_round(http, request_key="draft", submit_event=False).json()
    assert isinstance(draft["record_id"], int)


def test_tasks_keep_targets_schedule_results_and_handoff(client):
    http, engine = client
    assert save_round(http).status_code == 200
    candidates = http.get('/api/encounters/1/intents').json()
    assert {i["intent_type"] for i in candidates} == {"RECHECK", "MONITOR", "ACTION", "FOLLOW_RESULT"}
    due = "2026-10-24T01:00:00+00:00"
    for item in candidates:
        response = http.post(f"/api/intents/{item['id']}/confirm", json={"doctor_id": 1, "due_at": due})
        assert response.status_code == 200, response.text
        assert response.json()["waiting_for"] == item["target"]
    tasks = http.get('/api/tasks?doctor_id=1').json()
    assert len(tasks) == 4
    assert all(t["record_id"] and t["due_at"] for t in tasks)
    # Confirmation retry creates no duplicate task.
    http.post(f"/api/intents/{candidates[0]['id']}/confirm", json={"doctor_id": 1})
    assert len(http.get('/api/tasks').json()) == 4
    task = tasks[0]

    def action(name, version, note="", **extra):
        return http.post(f"/api/loops/{task['id']}/{name}", json={"doctor_id": 1, "expected_version": version, "note": note, **extra})

    assert action("resolve", 1, "不能跳步").status_code == 409
    assert action("record-result", 1, "").status_code == 409
    assert action("record-result", 1, "合成执行记录").json()["status"] == "RESULT_AVAILABLE"
    assert action("schedule", 1, due_at=due).status_code == 409
    assert action("resolve", 2, "仍不能跳过查看").status_code == 409
    assert action("acknowledge", 2).json()["status"] == "ACKNOWLEDGED"
    assert action("resolve", 3, " ").status_code == 409
    assert action("resolve", 3, "医生已记录处理结论").json()["status"] == "RESOLVED"
    history = http.get(f"/api/loops/{task['id']}/history").json()
    assert history["result"]["note"] == "合成执行记录"
    assert history["history"][-1]["detail"]["note"] == "医生已记录处理结论"
    handoff = http.post('/api/encounters/1/handoffs', json={"doctor_id":1}).json()
    assert len(handoff["references"]) == 3
    assert task['id'] not in [r['loop_id'] for r in handoff['references']]


def test_scopes_and_input_validation(client):
    http, _ = client
    assert save_round(http, transcript=" ").status_code == 422
    assert save_round(http, patient_id=999).status_code == 404
    assert save_round(http).status_code == 200
    candidate = http.get('/api/encounters/1/intents').json()[0]
    assert http.post(f"/api/intents/{candidate['id']}/confirm", json={"doctor_id":999}).status_code != 200
    assert http.post(f"/api/intents/{candidate['id']}/reject", json={"doctor_id":999,"reason":"无权限"}).status_code == 403
    assert http.post(f"/api/intents/{candidate['id']}/confirm", json={"doctor_id":1,"due_at":"2026-10-24T09:00:00"}).status_code == 422
    http.post(f"/api/intents/{candidate['id']}/confirm", json={"doctor_id":1})
    task = http.get('/api/tasks').json()[0]
    assert http.get('/api/tasks?doctor_id=999').json() == []
    assert http.post(f"/api/loops/{task['id']}/record-result", json={"doctor_id":999,"expected_version":1,"note":"越权"}).status_code == 403
    assert http.get(f"/api/loops/{task['id']}/history?doctor_id=999").status_code == 403


def test_multiple_tasks_in_one_clause_keep_distinct_targets(client):
    http, _ = client
    assert save_round(http, transcript="复查血常规并复查电解质").status_code == 200
    candidates = http.get('/api/encounters/1/intents').json()
    assert {i['target'] for i in candidates} == {'血常规', '电解质'}
    for item in candidates:
        result = http.post(f"/api/intents/{item['id']}/confirm", json={"doctor_id": 1})
        assert result.json()['waiting_for'] == item['target']


@pytest.mark.parametrize('text', ['不复查血常规', '昨日复查血常规', '已联系心内科会诊', '无需监测血氧', '复查血常规示白细胞下降'])
def test_completed_historical_and_negated_plans_do_not_create_tasks(text):
    assert extract_intent(text) == []


def test_rounding_finish_persists_without_closing_followup_tasks(client):
    http, engine = client
    saved = save_round(http).json()
    round_id = saved['id']
    assert http.get('/api/patients').json()[0]['rounding_status'] == '待结束'
    assert http.post(f'/api/rounding-sessions/{round_id}/finish', json={'doctor_id':1}).status_code == 409
    candidates = http.get('/api/encounters/1/intents').json()
    for item in candidates:
        assert http.post(f"/api/intents/{item['id']}/confirm", json={'doctor_id':1}).status_code == 200
    response = http.post(f'/api/rounding-sessions/{round_id}/finish', json={'doctor_id':1})
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'COMPLETED'
    assert response.json()['open_task_count'] == 4
    assert response.json()['record_status'] == '待审核'
    assert all(t['status']=='WAITING_EVENT' for t in http.get('/api/tasks').json())
    assert http.get(f'/api/rounding-sessions/{round_id}').json()['status'] == 'COMPLETED'
    assert http.get('/api/rounding-sessions?encounter_id=1').json()[0]['status'] == 'COMPLETED'
    assert http.get('/api/patients').json()[0]['rounding_status'] == '已完成'
    # Finish retries are harmless, and authorization still applies to closed rounds.
    assert http.post(f'/api/rounding-sessions/{round_id}/finish', json={'doctor_id':1}).status_code == 200
    assert http.post(f'/api/rounding-sessions/{round_id}/finish', json={'doctor_id':999}).status_code == 403
    assert http.get(f'/api/rounding-sessions/{round_id}?doctor_id=999').status_code == 403
    from backend.models import AuditLog
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action=='FINISH_ROUNDING')) == 1


def test_rounding_finish_only_checks_its_own_candidates_and_handles_no_tasks(client):
    http, _ = client
    first = save_round(http).json()
    second = save_round(http, request_key='no-tasks', transcript='患者精神尚可').json()
    assert http.post(f"/api/rounding-sessions/{second['id']}/finish", json={'doctor_id':1}).status_code == 200
    assert http.post(f"/api/rounding-sessions/{first['id']}/finish", json={'doctor_id':1}).status_code == 409
    for intent in http.get('/api/encounters/1/intents').json():
        http.post(f"/api/intents/{intent['id']}/reject", json={'doctor_id':1,'reason':'合成测试中忽略'})
    assert http.post(f"/api/rounding-sessions/{first['id']}/finish", json={'doctor_id':1}).status_code == 200


@pytest.mark.parametrize('kind', ['ACTION', 'MONITOR'])
def test_execution_tasks_can_really_complete_in_one_explicit_action(client, kind):
    http, engine = client
    saved = save_round(http).json()
    candidate = next(i for i in http.get('/api/encounters/1/intents').json() if i['intent_type']==kind)
    task = http.post(f"/api/intents/{candidate['id']}/confirm", json={'doctor_id':1}).json()
    path = f"/api/loops/{task['loop_id']}/complete"
    assert http.post(path, json={'doctor_id':1,'expected_version':1,'note':' '}).status_code == 409
    assert http.post(path, json={'doctor_id':999,'expected_version':1,'note':'执行'}).status_code == 403
    result = http.post(path, json={'doctor_id':1,'expected_version':1,'note':'实际执行完成，记录处理情况'})
    assert result.status_code == 200, result.text
    assert result.json()['status'] == 'RESOLVED'
    assert result.json()['completed_at']
    assert result.json()['source_rounding_session_id'] == saved['id']
    assert http.post(path, json={'doctor_id':1,'expected_version':1,'note':'重复'}).status_code == 409
    history = http.get(f"/api/loops/{task['loop_id']}/history").json()
    assert history['task']['status'] == 'RESOLVED'
    assert history['result']['note'] == '实际执行完成，记录处理情况'
    assert history['history'][-1]['action'] == 'COMPLETE'
    assert http.post('/api/encounters/1/handoffs',json={'doctor_id':1}).json()['references'] == [
        {'intent_id': i['id']} for i in http.get('/api/encounters/1/intents').json() if i['status']=='CANDIDATE'
    ]


@pytest.mark.parametrize('kind', ['FOLLOW_RESULT', 'RECHECK'])
def test_report_tasks_cannot_use_execution_shortcut(client, kind):
    http, _ = client
    save_round(http)
    candidate = next(i for i in http.get('/api/encounters/1/intents').json() if i['intent_type']==kind)
    task = http.post(f"/api/intents/{candidate['id']}/confirm", json={'doctor_id':1}).json()
    response = http.post(f"/api/loops/{task['loop_id']}/complete", json={'doctor_id':1,'expected_version':1,'note':'跳过报告'})
    assert response.status_code == 409
    assert http.get(f"/api/loops/{task['loop_id']}/history").json()['task']['status'] == 'WAITING_EVENT'


def test_patient_picker_only_lists_doctor_accessible_patients(client):
    http, engine = client
    with Session(engine) as db:
        db.add(Doctor(id=2, username='other',name='另一合成医生',department='测试'))
        db.add(Patient(id=2, name='另一合成患者', admission_number='TEST-2', bed_number='02', diagnosis='测试', doctor_id=2))
        db.flush()
        db.add(Encounter(id=2, encounter_number='TEST-ENC-2', patient_id=2, attending_doctor_id=2, admitted_at=datetime.now(timezone.utc)))
        db.commit()
    assert [p['id'] for p in http.get('/api/patients').json()] == [1]
    assert [p['id'] for p in http.get('/api/patients?doctor_id=2').json()] == [2]
    assert http.get('/api/patients?doctor_id=999').json() == []


def test_patient_history_keeps_multiple_same_day_roundings_and_records(client):
    http, engine = client
    first = save_round(http, transcript='上午查房原文', generated_note='上午病历正文').json()
    http.post(f"/api/medical-records/{first['record_id']}/sign")
    # A second visit is allowed even while an earlier rounding is not finished.
    second = save_round(http, request_key='afternoon', transcript='下午查房原文', generated_note='下午病历正文').json()
    assert first['id'] != second['id'] and first['record_id'] != second['record_id']
    history = http.get('/api/patients/1/history').json()
    assert [r['id'] for r in history['roundings']] == [second['id'], first['id']]
    assert [r['id'] for r in history['records']] == [second['record_id'], first['record_id']]
    assert history['records'][1]['status'] == '已签署'
    assert history['records'][1]['preview'] == '上午病历正文'
    http.patch(f"/api/medical-records/{second['record_id']}", json={'content':'下午修改后正文'})
    assert http.get(f"/api/medical-records/{first['record_id']}").json()['content'] == '上午病历正文'
    assert http.get(f"/api/rounding-sessions/{first['id']}").json()['transcript'] == '上午查房原文'
    assert http.get(f"/api/medical-records/{second['record_id']}").json()['content'] == '下午修改后正文'
    patient = http.get('/api/patients').json()[0]
    assert patient['today_rounding_count'] == 2
    assert http.post(f"/api/rounding-sessions/{first['id']}/finish", json={'doctor_id':1}).status_code == 200
    patient = http.get('/api/patients').json()[0]
    assert patient['today_completed_rounding_count'] == 1
    assert patient['rounding_status'] == '待结束'
    # Recover provenance for old ROUND records that predate the FK migration.
    with Session(engine) as db:
        db.get(MedicalRecord, first['record_id']).source_rounding_session_id = None
        db.commit()
    old = http.get(f"/api/medical-records/{first['record_id']}").json()
    assert old['source_rounding_session_id'] == first['id']
    assert old['patient_id'] == 1 and old['encounter_number'] == 'TEST-ENC'


def test_history_separates_admissions_and_enforces_patient_scope(client):
    http, engine = client
    current = save_round(http, transcript='本次住院查房').json()
    with Session(engine) as db:
        db.add(Encounter(id=2, encounter_number='OLD-ENC', patient_id=1, attending_doctor_id=1, status='DISCHARGED', admitted_at=datetime(2025,1,1,tzinfo=timezone.utc)))
        db.add(Patient(id=2, name='合成患者', admission_number='OTHER-PATIENT', bed_number='02',diagnosis='测试',doctor_id=1))
        db.flush()
        db.add(Encounter(id=3, encounter_number='OTHER-ENC',patient_id=2,attending_doctor_id=1,admitted_at=datetime.now(timezone.utc)))
        db.commit()
    old = save_round(http, encounter_id=2, request_key='old-admission', transcript='上次住院查房').json()
    other = save_round(http, patient_id=2,encounter_id=3,request_key='other-patient',transcript='同名另一患者').json()
    history = http.get('/api/patients/1/history').json()
    assert {r['id'] for r in history['records']} == {current['record_id'],old['record_id']}
    assert {e['id'] for e in history['encounters']} == {1,2}
    assert http.get('/api/patients/1/history?encounter_id=1').json()['roundings'][0]['id'] == current['id']
    assert [r['id'] for r in http.get('/api/patients/1/history?encounter_id=2').json()['records']] == [old['record_id']]
    assert http.get('/api/patients/1/history?encounter_id=3').status_code == 404
    assert http.get('/api/patients/1/history?doctor_id=999').status_code == 403
    assert http.get('/api/patients/999/history').status_code == 404
    assert [r['id'] for r in http.get('/api/medical-records?patient_id=1&encounter_id=2').json()] == [old['record_id']]
