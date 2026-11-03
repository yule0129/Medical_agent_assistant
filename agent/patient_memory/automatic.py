"""Persist extraction before returning saves; workers recover after restart.

Only saved admission interviews and completed rounds in active stays qualify.
Extraction creates reviewable candidates, never accepted clinical tasks.
"""
from datetime import datetime,timedelta,timezone
from fastapi import HTTPException
from sqlalchemy import select,func
from sqlalchemy.exc import SQLAlchemyError
from backend.models import Encounter,AdmissionWorkspace,RoundingSession,Patient
from backend.services.intent_service import require_scope
from backend.services.report_service import utc
from .models import MatterScan,PatientMatter
from . import service


def enqueue(db,encounter,doctor_id,kind,source_id):
    if encounter.status!='INPATIENT':return None
    require_scope(db,encounter.id,doctor_id)
    source=service.current_source(db,encounter,kind,source_id)
    key=service.digest((encounter.id,kind,source_id,source['digest']))
    scan=db.scalar(select(MatterScan).where(MatterScan.scan_key==key))
    if not scan:
        scan=MatterScan(scan_key=key,encounter_id=encounter.id,doctor_id=doctor_id,
            source_kind=kind,source_id=source_id,source_digest=source['digest'],
            status='QUEUED',created_at=datetime.now(timezone.utc),attempt=0)
        db.add(scan);db.flush()
    return scan


def schedule(db):
    # Backfill saved sources and recover a save from an older API version.
    for encounter in db.scalars(select(Encounter).where(Encounter.status=='INPATIENT')
                                .order_by(Encounter.id).with_for_update(skip_locked=True)):
        workspace=db.scalar(select(AdmissionWorkspace).where(AdmissionWorkspace.encounter_id==encounter.id))
        if workspace and workspace.input_version>0 and workspace.transcript.strip():
            doctor_id=encounter.attending_doctor_id or db.get(Patient,encounter.patient_id).doctor_id
            if doctor_id:
                try:enqueue(db,encounter,doctor_id,'ADMISSION',workspace.id)
                except ValueError:pass
        for row in db.scalars(select(RoundingSession).where(RoundingSession.encounter_id==encounter.id,
                RoundingSession.status=='COMPLETED')):
            if row.transcript.strip():
                try:enqueue(db,encounter,row.doctor_id,'ROUNDING',row.id)
                except ValueError:pass
    db.commit()


def eligible(scan,now):
    return (scan.status=='QUEUED' or
        scan.status=='FAILED' and scan.attempt<3 and scan.retry_at and utc(scan.retry_at)<=now or
        scan.status=='RUNNING' and utc(scan.created_at)+timedelta(seconds=120)<=now)


def process_once(db):
    now=datetime.now(timezone.utc)
    for scan in db.scalars(select(MatterScan).where(MatterScan.status.in_(['QUEUED','FAILED','RUNNING']))
                           .order_by(MatterScan.id)).all():
        if not eligible(scan,now):continue
        encounter=db.scalar(select(Encounter).where(Encounter.id==scan.encounter_id)
            .with_for_update(skip_locked=True).execution_options(populate_existing=True))
        if not encounter:continue
        db.refresh(scan)
        if not eligible(scan,now):db.rollback();continue
        try:
            require_scope(db,encounter.id,scan.doctor_id)
            source=service.current_source(db,encounter,scan.source_kind,scan.source_id)
            if encounter.status!='INPATIENT' or source['digest']!=scan.source_digest:
                scan.status,scan.error_code='STALE','SOURCE_OR_ENCOUNTER_CHANGED'
                db.commit();return {'scan_id':scan.id,'status':'STALE'}
            # Recover abandoned final attempts without making unbounded requests.
            if scan.status=='RUNNING' and scan.attempt>=3:
                scan.status,scan.error_code,scan.retry_at='FAILED','EXTRACTION_INTERRUPTED',None
                db.commit();return {'scan_id':scan.id,'status':'FAILED'}
            return service.extract_matters(db,encounter,scan.doctor_id,scan.source_kind,
                scan.source_id,scan.source_digest)
        except (HTTPException,ValueError,SQLAlchemyError) as exc:
            db.rollback();scan=db.get(MatterScan,scan.id)
            if scan.status not in {'FAILED','STALE','READY'}:
                scan.attempt+=1
                scan.status='STALE' if isinstance(exc,ValueError) or getattr(exc,'status_code',None) in {403,404,409} else 'FAILED'
                scan.error_code='SOURCE_OR_SCOPE_CHANGED' if scan.status=='STALE' else 'EXTRACTION_UNAVAILABLE'
                scan.retry_at=now+timedelta(seconds=30*scan.attempt) if scan.status=='FAILED' and scan.attempt<3 else None
                db.commit()
            return {'scan_id':scan.id,'status':scan.status,'error_code':scan.error_code}
    return None


def feedback(db,doctor_id):
    result=[];seen=set()
    for scan in db.scalars(select(MatterScan).order_by(MatterScan.id.desc())):
        source_key=(scan.encounter_id,scan.source_kind,scan.source_id)
        if source_key in seen:continue
        try:require_scope(db,scan.encounter_id,doctor_id)
        except ValueError:continue
        encounter=db.get(Encounter,scan.encounter_id)
        if encounter.status!='INPATIENT':continue
        seen.add(source_key)
        try:stale=service.current_source(db,encounter,scan.source_kind,scan.source_id)['digest']!=scan.source_digest
        except HTTPException:stale=True
        candidates=db.scalar(select(func.count()).select_from(PatientMatter).where(
            PatientMatter.scan_id==scan.id,PatientMatter.status=='CANDIDATE'))
        total=db.scalar(select(func.count()).select_from(PatientMatter).where(PatientMatter.scan_id==scan.id))
        result.append({'scan_id':scan.id,'patient_id':encounter.patient_id,'encounter_id':encounter.id,
            'patient':db.get(Patient,encounter.patient_id).name,'source_kind':scan.source_kind,'source_id':scan.source_id,
            'status':'STALE' if stale else scan.status,'candidate_count':candidates,'total_count':total,
            'error_code':scan.error_code,'attempt':scan.attempt,
            'retry_at':utc(scan.retry_at).isoformat() if scan.retry_at else None})
    return result
