import hashlib
import json
from sqlalchemy import select
from backend.models import LabReport, Patient
from backend.services.report_service import utc


def collect_sources(db, encounter, workspace):
    patient = db.get(Patient, encounter.patient_id)
    profile = {key: getattr(patient, key) for key in (
        "name", "gender", "age", "ethnicity", "marital_status", "birthplace", "occupation")}
    profile.update(admitted_at=utc(encounter.admitted_at).isoformat(), history_reporter=encounter.history_reporter,
                   documented_diagnosis=encounter.diagnosis_snapshot or None,
                   recorded_at=utc(encounter.recorded_at).isoformat() if encounter.recorded_at else None)
    # A report revision supersedes earlier versions of the same report in this admission.
    reports = db.scalars(select(LabReport).where(LabReport.patient_id == patient.id,
        LabReport.encounter_id == encounter.id).order_by(LabReport.report_version.desc(), LabReport.id.desc())).all()
    latest = {}
    for row in reports:
        latest.setdefault(row.report_id, row)
    exams = [{"id": f"lab:{r.id}:v{r.report_version}", "kind": "已接入检查报告",
              "report_id": r.report_id, "version": r.report_version, "test_name": r.test_name,
              "reported_at": utc(r.reported_at).isoformat(), "text": r.result_text} for r in latest.values()]
    if workspace and workspace.examination_text.strip():
        exams.append({"id": f"manual-exam:{encounter.id}:v{workspace.input_version}",
                      "kind": "医生补充检查结果", "text": workspace.examination_text})
    return {"read_patient_profile": [{"id": f"profile:{patient.id}:{encounter.id}", "data": profile}],
            "read_interview": [{"id": f"interview:{encounter.id}:v{workspace.input_version if workspace else 0}",
                                "text": workspace.transcript if workspace else ""}],
            "read_examinations": exams}


def source_digest(sources):
    return hashlib.sha256(json.dumps(sources, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
