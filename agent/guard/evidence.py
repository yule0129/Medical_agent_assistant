"""Deterministic checks on actual observations; model claims never authorize writes."""
from datetime import datetime, timedelta
from sqlalchemy import select

from backend.models import EvidenceNode, LabReport, MedicalRecord, ResultAcknowledgement
from backend.services.report_service import require_latest_evidence, utc
from agent.tools.evidence import digest, source_row


def validate_candidate(db, run_input, loop, candidate, observations, now, grace_minutes=5):
    if loop.version != run_input.scope.expected_loop_version or loop.status in {"CANCELLED", "REJECTED"}:
        raise ValueError("STALE_CONTEXT")
    observed = {(s["source_type"], s["source_id"], s["source_version"]): s for tool in observations for s in tool.get("sources", [])}
    snapshots = []
    for ref in candidate.evidence:
        key = (ref.source_type, ref.source_id, ref.source_version)
        item = observed.get(key)
        if not item or ref.quote not in item["text"]:
            raise ValueError("UNOBSERVED_OR_FABRICATED_EVIDENCE")
        row, text, version, patient, encounter, when = source_row(db, ref.source_type, ref.source_id)
        if (patient, encounter, version, digest(text)) != (loop.patient_id, loop.encounter_id, ref.source_version, item["digest"]):
            raise ValueError("EVIDENCE_CHANGED_OR_OUT_OF_SCOPE")
        if ref.source_type == "lab_report":
            from backend.domain.matching import matches_report
            if not matches_report(loop, row):
                raise ValueError("REPORT_NOT_FOR_THIS_TARGET")
            latest = db.scalar(select(LabReport.report_version).where(LabReport.patient_id == patient, LabReport.encounter_id == encounter, LabReport.report_id == row.report_id).order_by(LabReport.report_version.desc()))
            if latest != version:
                raise ValueError("STALE_REPORT_VERSION")
        if utc(datetime.fromisoformat(item["observed_at"])) != utc(when):
            raise ValueError("SOURCE_TIME_CHANGED")
        if not (run_input.window.from_time <= when <= run_input.window.to_time):
            raise ValueError("EVIDENCE_OUTSIDE_RUN_WINDOW")
        snapshots.append(item)
    for coverage in candidate.searched_sources:
        if not any(tool["tool_name"] == coverage.tool_name and tool["window"] == coverage.window.model_dump(mode="json")
            and tool["query_succeeded"] == coverage.query_succeeded and tool["complete"] == coverage.complete for tool in observations):
            raise ValueError("FABRICATED_SEARCH_COVERAGE")
    evidence = db.get(EvidenceNode, loop.current_evidence_id) if loop.current_evidence_id else None
    if not evidence or evidence.source_type != "STRUCTURED_LAB_REPORT":
        raise ValueError("CURRENT_REPORT_EVIDENCE_REQUIRED")
    require_latest_evidence(db, evidence)
    report = db.scalar(select(LabReport).where(LabReport.event_id == evidence.event_id))
    if not report or not any(s["source_type"] == "lab_report" and s["source_id"] == report.id for s in snapshots):
        raise ValueError("CURRENT_REPORT_MUST_BE_CITED")
    if candidate.kind in {"RESPONSE_FOUND", "FOLLOW_UP"} and not any(s["source_type"] in {"medical_record", "result_acknowledgement"} for s in snapshots):
        raise ValueError("RESPONSE_SOURCE_REQUIRED")
    if candidate.kind in {"RESPONSE_FOUND", "FOLLOW_UP"}:
        response_refs = [item for item in snapshots if item["source_type"] in {"medical_record", "result_acknowledgement"}]
        if not any(utc(datetime.fromisoformat(item["observed_at"])) >= utc(report.reported_at) for item in response_refs):
            raise ValueError("RESPONSE_PREDATES_REPORT")
    if candidate.kind == "RESPONSE_NOT_FOUND":
        if utc(now) < utc(report.reported_at) + timedelta(minutes=grace_minutes):
            raise ValueError("RESPONSE_GRACE_PERIOD")
        for name, model, kind in (("get_progress_notes", MedicalRecord, "medical_record"), ("get_result_acknowledgements", ResultAcknowledgement, "result_acknowledgement")):
            calls = [tool for tool in observations if tool["tool_name"] == name and tool["query_succeeded"] and tool["complete"]
                and tool["window"] == run_input.window.model_dump(mode="json")]
            if not calls:
                raise ValueError("RESPONSE_WINDOW_NOT_FULLY_SEARCHED")
            # Repeat the actual structured coverage check at commit; newly arrived
            # notes/confirmations invalidate an earlier negative search.
            query = select(model.id)
            if model is MedicalRecord:
                query = query.where(model.patient_id == loop.patient_id, model.encounter_id == loop.encounter_id)
            else:
                query = query.where(model.loop_id == loop.id, model.evidence_id == evidence.id)
            query = query.where(model.created_at >= run_input.window.from_time, model.created_at <= run_input.window.to_time)
            ids = set(db.scalars(query))
            read_ids = {s["source_id"] for s in calls[-1]["sources"] if s["source_type"] == kind}
            if ids != read_ids or (name == "get_result_acknowledgements" and ids):
                raise ValueError("RESPONSE_COVERAGE_CHANGED_OR_ACKNOWLEDGED")
    if candidate.kind == "FOLLOW_UP":
        matching = candidate.follow_up
        from backend.domain.matching import normalized_test
        if matching.report_id or normalized_test(matching.test_name) == normalized_test(report.test_name):
            raise ValueError("FOLLOW_UP_MUST_TARGET_A_NEW_TEST_WITH_SHARED_SPECIMEN_OR_ORDER")
        identifiers = evidence.content
        # At least one non-null strong identifier must be shared with the report.
        if not any(getattr(matching, key) and getattr(matching, key) == identifiers.get(key) for key in ("specimen_id", "order_id")):
            raise ValueError("FOLLOW_UP_IDENTIFIER_NOT_VERIFIED")
    return snapshots


def recheck_snapshots(db, loop, content):
    for item in content["snapshots"]:
        row, text, version, patient, encounter, when = source_row(db, item["source_type"], item["source_id"])
        if (patient, encounter, version, digest(text), utc(when)) != (loop.patient_id, loop.encounter_id, item["source_version"], item["digest"], utc(datetime.fromisoformat(item["observed_at"]))):
            raise ValueError("SOURCE_CHANGED_REFRESH_REQUIRED")
    from backend.models import EvidenceNode
    require_latest_evidence(db, db.get(EvidenceNode, loop.current_evidence_id))
    candidate = content.get("candidate", {})
    if candidate.get("kind") == "RESPONSE_NOT_FOUND":
        through = max(datetime.fromisoformat(s["window"]["to_time"]) for s in candidate["searched_sources"])
        if db.scalar(select(MedicalRecord.id).where(MedicalRecord.patient_id == loop.patient_id,
            MedicalRecord.encounter_id == loop.encounter_id, MedicalRecord.created_at > through)):
            raise ValueError("PROGRESS_UPDATED_REINVESTIGATE")
