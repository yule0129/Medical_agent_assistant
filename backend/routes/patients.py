"""Patient basic information; admission facts always belong to the selected encounter."""
from fastapi import APIRouter, Depends, HTTPException
from datetime import datetime, timezone
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import AuditLog, Encounter, MedicalRecord, Patient
from ..services.admission_service import admission_state
from ..services.intent_service import require_scope
from ..services.report_service import utc
from agent.admission.service import workspace_for, generate_admission, assert_reviewable

router = APIRouter(prefix="/api/patients", tags=["patients"])


def scoped_admission(db, patient_id, encounter_id, doctor_id, lock=False):
    query = select(Encounter).where(Encounter.id == encounter_id, Encounter.patient_id == patient_id)
    encounter = db.scalar(query.with_for_update() if lock else query)
    if encounter is None:
        raise HTTPException(404, "该患者的住院记录不存在")
    try:
        require_scope(db, encounter.id, doctor_id)
    except ValueError as exc:
        raise HTTPException(403, "无权查看或修改该次住院资料") from exc
    return encounter


@router.get("/{patient_id}/admission-record")
def get_admission(patient_id: int, encounter_id: int, doctor_id: int = 1, db: Session = Depends(get_db)):
    encounter = scoped_admission(db, patient_id, encounter_id, doctor_id)
    return admission_state(db, encounter)


class AdmissionInputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    doctor_id: int = Field(gt=0)
    encounter_id: int = Field(gt=0)
    transcript: str = Field(max_length=50000)
    examination_text: str = Field(default="", max_length=50000)
    expected_input_version: int = Field(ge=0)


@router.put("/{patient_id}/admission-inputs")
def save_admission_inputs(patient_id: int, payload: AdmissionInputs, db: Session = Depends(get_db)):
    encounter = scoped_admission(db, patient_id, payload.encounter_id, payload.doctor_id, lock=True)
    state = admission_state(db, encounter)
    if state['reviewed']:
        raise HTTPException(409, "已签署记录的问诊资料不能修改")
    row = workspace_for(db, encounter.id)
    if (row.input_version if row else 0) != payload.expected_input_version:
        raise HTTPException(409, "问诊资料已更新，请保留输入并刷新后核对")
    if row is None:
        row = workspace_for(db, encounter.id, True)
    changed = row.transcript != payload.transcript.strip() or row.examination_text != payload.examination_text.strip()
    if changed:
        row.transcript, row.examination_text = payload.transcript.strip(), payload.examination_text.strip()
        row.input_version += 1
        row.status, row.generation_token = 'INPUT_SAVED', None
        row.updated_at = datetime.now(timezone.utc)
    db.add(AuditLog(actor_type='DOCTOR', actor_id=str(payload.doctor_id), action='SAVE_ADMISSION_INTERVIEW',
        resource_type='Encounter', resource_id=str(encounter.id), old_state={},
        new_state={'input_version': row.input_version, 'transcript': row.transcript, 'examination_text': row.examination_text},
        created_at=datetime.now(timezone.utc)))
    db.commit()
    return admission_state(db, encounter)


class AdmissionGenerate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    doctor_id: int = Field(gt=0)
    encounter_id: int = Field(gt=0)
    expected_revision: str
    expected_source_digest: str


@router.post("/{patient_id}/admission-generate")
def admission_generate(patient_id: int, payload: AdmissionGenerate, db: Session = Depends(get_db)):
    encounter = scoped_admission(db, patient_id, payload.encounter_id, payload.doctor_id, lock=True)
    state = admission_state(db, encounter)
    if state['agent']['source_digest'] != payload.expected_source_digest:
        raise HTTPException(409, "问诊或检查资料已更新，请刷新核对后生成")
    return generate_admission(db, encounter, payload.doctor_id, payload.expected_revision)


class AdmissionWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    doctor_id: int = Field(gt=0)
    encounter_id: int = Field(gt=0)
    content: str = Field(min_length=1, max_length=100000)
    expected_revision: str

    @field_validator("content")
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError("请填写入院记录正文")
        return value.strip()


class AdmissionReview(AdmissionWrite):
    expected_source_digest: str
    confirmed: bool


@router.post("/{patient_id}/admission-review")
def review_admission(patient_id: int, payload: AdmissionReview, db: Session = Depends(get_db)):
    encounter = scoped_admission(db, patient_id, payload.encounter_id, payload.doctor_id, lock=True)
    state = admission_state(db, encounter)
    if not payload.confirmed:
        raise HTTPException(422, "请确认已核对问诊、检查资料及入院记录内容")
    if not state['record'] or state['reviewed'] or state['revision'] != payload.expected_revision:
        raise HTTPException(409, "病历状态已变化或尚无草稿，请刷新核对")
    if state['agent']['source_digest'] != payload.expected_source_digest:
        raise HTTPException(409, "问诊或检查已更新，请重新核对")
    record = db.get(MedicalRecord, state['record']['id'])
    assert_reviewable(db, record, payload.doctor_id)
    before = record.content
    record.content, record.status, record.signed_at = payload.content, '已签署', datetime.now(timezone.utc)
    db.add(AuditLog(actor_type='DOCTOR', actor_id=str(payload.doctor_id), action='REVIEW_ADMISSION_RECORD',
        resource_type='MedicalRecord', resource_id=str(record.id), old_state={'content': before, 'status': '待审核'},
        new_state={'content': record.content, 'status': record.status}, created_at=record.signed_at))
    db.commit()
    return admission_state(db, encounter)


@router.put("/{patient_id}/admission-record")
def save_admission(patient_id: int, payload: AdmissionWrite, db: Session = Depends(get_db)):
    encounter = scoped_admission(db, patient_id, payload.encounter_id, payload.doctor_id, lock=True)
    state = admission_state(db, encounter)
    if state["revision"] != payload.expected_revision:
        raise HTTPException(409, "入院记录已更新，请保留输入内容并刷新后核对")
    if state["record"] and state["record"]["status"] == "已签署":
        raise HTTPException(409, "已签署入院记录不能修改")
    now = datetime.now(timezone.utc)
    before = state["record"]["content"] if state["record"] else None
    if state["record"]:
        record = db.get(MedicalRecord, state["record"]["id"])
        record.content = payload.content
    else:
        record = MedicalRecord(record_number=f"ADM-{encounter.id:06d}", patient_id=patient_id,
                               doctor_id=payload.doctor_id, encounter_id=encounter.id,
                               record_type="入院记录", content=payload.content, status="待审核", created_at=now)
        db.add(record)
    db.flush()
    db.add(AuditLog(actor_type="DOCTOR", actor_id=str(payload.doctor_id), action="SAVE_ADMISSION_RECORD",
                    resource_type="MedicalRecord", resource_id=str(record.id),
                    old_state={"content": before}, new_state={"content": record.content, "encounter_id": encounter.id},
                    created_at=now))
    db.commit()
    return admission_state(db, encounter)


@router.get("/{patient_id}/profile")
def patient_profile(patient_id: int, encounter_id: int | None = None, doctor_id: int = 1, db: Session = Depends(get_db)):
    patient = db.get(Patient, patient_id)
    if patient is None:
        raise HTTPException(404, "患者不存在")
    accessible = []
    for row in db.scalars(select(Encounter).where(Encounter.patient_id == patient_id).order_by(Encounter.admitted_at.desc(), Encounter.id.desc())):
        try:
            require_scope(db, row.id, doctor_id)
            accessible.append(row)
        except ValueError:
            continue
    if patient.doctor_id != doctor_id and not accessible:
        raise HTTPException(403, "无权查看该患者资料")
    if encounter_id is not None:
        encounter = next((row for row in accessible if row.id == encounter_id), None)
        if encounter is None:
            raise HTTPException(404, "该患者的可访问住院记录不存在")
    else:
        encounter = next((row for row in accessible if row.status == "INPATIENT"), accessible[0] if accessible else None)
    return {
        "id": patient.id, "name": patient.name, "gender": patient.gender, "age": patient.age,
        "ethnicity": patient.ethnicity, "marital_status": patient.marital_status,
        "birthplace": patient.birthplace, "occupation": patient.occupation,
        "admission_number": patient.admission_number, "bed": patient.bed_number, "diagnosis": patient.diagnosis,
        "encounter_id": encounter.id if encounter else None,
        "encounter_number": encounter.encounter_number if encounter else None,
        "admitted_at": utc(encounter.admitted_at).isoformat() if encounter else None,
        "recorded_at": utc(encounter.recorded_at).isoformat() if encounter and encounter.recorded_at else None,
        "history_reporter": encounter.history_reporter if encounter else None,
        "encounters": [{"id": row.id, "encounter_number": row.encounter_number, "status": row.status,
                        "admitted_at": utc(row.admitted_at).isoformat()} for row in accessible],
    }
