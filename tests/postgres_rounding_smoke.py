"""Verify the configured PostgreSQL using rollback-only synthetic data.

Run from the repository root: python tests/postgres_rounding_smoke.py
All application commits stay inside an outer transaction that is rolled back.
Existing clinical rows are never edited or deleted.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.db import engine, get_db
from backend.main import app
from backend.models import (AuditLog, ClinicalIntent, Encounter, EvidenceNode,
                            MedicalRecord, OpenLoop, RoundingSession)


def main():
    assert engine.dialect.name == 'postgresql', 'This check requires the configured PostgreSQL database.'
    models = [MedicalRecord, RoundingSession, ClinicalIntent, OpenLoop, EvidenceNode, AuditLog]
    with engine.connect() as connection:
        transaction = connection.begin()

        def counts():
            return [connection.scalar(select(func.count()).select_from(model)) for model in models]

        before = counts()

        def scoped_db():
            with Session(bind=connection, autoflush=False, join_transaction_mode='create_savepoint') as session:
                yield session

        app.dependency_overrides[get_db] = scoped_db
        try:
            with Session(bind=connection, join_transaction_mode='create_savepoint') as db:
                encounter = db.scalar(select(Encounter).where(Encounter.status=='INPATIENT', Encounter.attending_doctor_id==1))
                assert encounter is not None, 'A synthetic encounter assigned to development doctor 1 is required.'
                encounter_id, patient_id = encounter.id, encounter.patient_id
            now = datetime.now(timezone.utc).isoformat()
            with TestClient(app) as http:
                def post(path, data):
                    response = http.post('/api'+path, json=data)
                    assert response.status_code == 200, (path, response.status_code, response.text)
                    return response.json()

                saved = post('/rounding-sessions', dict(doctor_id=1, patient_id=patient_id, encounter_id=encounter_id,
                    started_at=now, ended_at=now, request_key='VERIFY-'+str(uuid4()),
                    transcript='合成回归验证。复查血常规。监测血氧。联系心内科会诊。血培养结果出来后通知我。'))
                round_id = saved['id']
                assert len(saved['workflow']['intents']) == 4
                pending = http.post(f'/api/rounding-sessions/{round_id}/finish', json={'doctor_id':1})
                assert pending.status_code == 409
                candidates = http.get(f'/api/encounters/{encounter_id}/intents').json()
                source_ids = {item['intent_id'] for item in saved['workflow']['intents']}
                tasks = []
                for item in candidates:
                    if item['id'] in source_ids:
                        tasks.append((item['intent_type'], post(f"/intents/{item['id']}/confirm", {'doctor_id':1})['loop_id']))
                finished = post(f'/rounding-sessions/{round_id}/finish', {'doctor_id':1})
                assert finished['status'] == 'COMPLETED' and finished['open_task_count'] == 4
                for kind, task_id in tasks:
                    if kind in {'ACTION','MONITOR'}:
                        result = post(f'/loops/{task_id}/complete', {'doctor_id':1,'expected_version':1,'note':'合成执行情况与处理结论'})
                    else:
                        for version, action, note in [(1,'record-result','合成检验结果'),(2,'acknowledge','医生已查看'),(3,'resolve','合成处理结论')]:
                            result = post(f'/loops/{task_id}/{action}', {'doctor_id':1,'expected_version':version,'note':note})
                    assert result['status'] == 'RESOLVED' and result['completed_at']
                    history = http.get(f'/api/loops/{task_id}/history').json()
                    assert history['task']['status'] == 'RESOLVED' and history['result']
                readback = http.get(f'/api/rounding-sessions/{round_id}').json()
                assert readback['status'] == 'COMPLETED'
                assert readback['open_task_count'] == 0 and readback['completed_task_count'] == 4
                assert http.get(f"/api/medical-records/{saved['record_id']}").status_code == 200
                assert http.get(f'/api/encounters/{encounter_id}/timeline').status_code == 200
                assert post(f'/rounding-sessions/{round_id}/finish', {'doctor_id':1})['status'] == 'COMPLETED'
                second = post('/rounding-sessions', dict(doctor_id=1,patient_id=patient_id,encounter_id=encounter_id,
                    started_at=now,ended_at=now,request_key='VERIFY-HISTORY-'+str(uuid4()),transcript='同一患者再次查房的合成验证'))
                assert second['id'] != round_id and second['record_id'] != saved['record_id']
                historical = http.get(f'/api/patients/{patient_id}/history').json()
                assert {saved['record_id'],second['record_id']}.issubset({r['id'] for r in historical['records']})
                assert {round_id,second['id']}.issubset({r['id'] for r in historical['roundings']})
                first_note = http.get(f"/api/medical-records/{saved['record_id']}").json()['content']
                second_note = http.get(f"/api/medical-records/{second['record_id']}").json()['content']
                assert '再次查房' not in first_note and '再次查房' in second_note
                print('PASS PostgreSQL: finish rounding with follow-ups preserved -> complete all four task types -> read persisted finish, task history and summary.')
                print('PASS PostgreSQL: second rounding and note remain independent; patient history returns both sessions and records.')
        finally:
            app.dependency_overrides.clear()
            transaction.rollback()
        assert counts() == before
        print('PASS rollback: existing record/rounding/intent/task/evidence/audit counts unchanged.')


if __name__ == '__main__':
    main()
