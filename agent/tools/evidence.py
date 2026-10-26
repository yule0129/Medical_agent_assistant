"""Read and fingerprint original evidence sources; never mutate clinical records."""
import hashlib
import json
from sqlalchemy import select
from backend.models import ClinicalEvent, LabReport, MedicalRecord, OpenLoop, ResultAcknowledgement
from backend.services.report_service import utc


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()



def source_row(db, kind, source_id):
    model = {"lab_report": LabReport, "medical_record": MedicalRecord, "result_acknowledgement": ResultAcknowledgement, "clinical_event": ClinicalEvent}.get(kind)
    row = db.get(model, source_id) if model else None
    if row is None:
        raise ValueError("SOURCE_NOT_FOUND")
    if kind == "lab_report":
        return row, row.result_text, row.report_version, row.patient_id, row.encounter_id, utc(row.reported_at)
    if kind == "medical_record":
        return row, row.content, 1, row.patient_id, row.encounter_id, utc(row.created_at)
    if kind == "result_acknowledgement":
        loop = db.get(OpenLoop, row.loop_id)
        return row, row.note or "医生已明确确认查看此报告", 1, loop.patient_id, loop.encounter_id, utc(row.created_at)
    return row, json.dumps(row.payload, ensure_ascii=False), 1, row.patient_id, row.encounter_id, utc(row.occurred_at)



def snapshot(db, kind, row_id):
    row, text, version, patient_id, encounter_id, when = source_row(db, kind, row_id)
    return {"source_type": kind, "source_id": row_id, "source_version": version, "text": text,
            "patient_id": patient_id, "encounter_id": encounter_id, "observed_at": when.isoformat(), "digest": digest(text)}
