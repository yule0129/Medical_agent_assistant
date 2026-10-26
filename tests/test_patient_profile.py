"""Patient profile sources, admission selection and scope; no synthetic fallback values."""
from datetime import datetime, timezone
import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from test_rounding_workflow import client
from backend.db import Base
from backend.models import Doctor, Patient, Encounter


def test_profile_reads_demographics_and_encounter_registration(client):
    http,engine=client
    recorded=datetime(2026,10,26,2,30,tzinfo=timezone.utc)
    with Session(engine) as db:
        patient=db.get(Patient,1)
        patient.gender='女';patient.age=52;patient.ethnicity='汉族';patient.marital_status='已婚'
        patient.birthplace='江苏省南京市';patient.occupation='教师'
        encounter=db.get(Encounter,1);encounter.recorded_at=recorded;encounter.history_reporter='患者本人'
        db.commit()
    response=http.get('/api/patients/1/profile')
    assert response.status_code==200,response.text
    data=response.json()
    assert (data['gender'],data['age'],data['ethnicity'],data['marital_status'],data['birthplace'],data['occupation'])==('女',52,'汉族','已婚','江苏省南京市','教师')
    assert datetime.fromisoformat(data['recorded_at'])==recorded
    assert data['history_reporter']=='患者本人' and data['encounter_id']==1
    assert data['admitted_at'] and data['encounters'][0]['id']==1


def test_missing_registration_fields_are_null_not_invented(client):
    http,engine=client
    data=http.get('/api/patients/1/profile').json()
    assert all(data[key] is None for key in ('ethnicity','marital_status','birthplace','occupation','recorded_at','history_reporter'))
    assert data['admitted_at'] is not None
    assert http.get('/api/patients/999999/profile').status_code==404
    assert http.get('/api/patients/1/profile?doctor_id=999').status_code==403


def test_profile_uses_selected_authorized_admission_and_patient_identity(client):
    http,engine=client
    old=datetime(2025,1,1,tzinfo=timezone.utc)
    with Session(engine) as db:
        db.add(Doctor(id=2,username='profile-other',name='合成医生二',department='测试'))
        db.add(Patient(id=2,name='合成患者',admission_number='OTHER-PATIENT',bed_number='02',diagnosis='合成',doctor_id=2))
        db.flush()
        db.add(Encounter(id=2,encounter_number='OLD-PROFILE',patient_id=1,attending_doctor_id=2,admitted_at=old,status='DISCHARGED',history_reporter='上次住院陈述者',recorded_at=old))
        db.add(Encounter(id=3,encounter_number='OTHER-PROFILE',patient_id=2,attending_doctor_id=2,admitted_at=old))
        db.commit()
    assert http.get('/api/patients/1/profile').json()['encounter_id']==1
    selected=http.get('/api/patients/1/profile?encounter_id=2').json()
    assert selected['history_reporter']=='上次住院陈述者' and datetime.fromisoformat(selected['admitted_at'])==old
    assert http.get('/api/patients/1/profile?encounter_id=3').status_code==404
    assert http.get('/api/patients/2/profile').status_code==403
    scoped=http.get('/api/patients/1/profile?doctor_id=2').json()
    assert scoped['encounter_id']==2 and [row['id'] for row in scoped['encounters']]==[2]


def test_profile_migration_preserves_existing_rows_and_is_repeatable():
    engine=create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        for table,names in {'patients':['ethnicity','marital_status','birthplace','occupation'], 'encounters':['recorded_at','history_reporter']}.items():
            for name in names:connection.execute(text(f'ALTER TABLE {table} DROP COLUMN {name}'))
        connection.execute(text("INSERT INTO patients (id,name,admission_number,bed_number,gender,age,diagnosis) VALUES (1,'existing patient','EXISTING','01','未记录',0,'existing diagnosis')"))
        connection.execute(text("INSERT INTO encounters (id,encounter_number,patient_id,admitted_at,status,diagnosis_snapshot) VALUES (1,'EXISTING-STAY',1,'2025-01-01','INPATIENT','existing diagnosis')"))
        migration=importlib.import_module('backend.alembic.versions.0006_patient_profile')
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade();migration.upgrade()
        assert connection.execute(text('SELECT name,ethnicity,occupation FROM patients WHERE id=1')).one()==('existing patient',None,None)
        assert connection.execute(text('SELECT encounter_number,recorded_at,history_reporter FROM encounters WHERE id=1')).one()==('EXISTING-STAY',None,None)
    engine.dispose()
