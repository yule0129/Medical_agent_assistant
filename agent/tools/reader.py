"""Execute the allow-listed clinical read tools under server-injected scope."""
from sqlalchemy import select
from sqlalchemy.orm import Session
from backend.domain.matching import matches_report
from backend.models import ClinicalEvent, Encounter, EvidenceNode, LabReport, MedicalRecord, OpenLoop, ResultAcknowledgement
from backend.services.intent_service import require_scope
from backend.services.report_service import utc
from agent.contracts import ToolCallAction
from .evidence import snapshot

TOOL_NAMES = ("get_patient_snapshot", "get_lab_reports", "get_progress_notes", "get_loop_state", "get_result_acknowledgements", "get_evidence")


def execute_read(bind, run_input, action, remaining):
    if remaining <= 0:
        raise TimeoutError("TOOL_BUDGET_EXHAUSTED")
    if not isinstance(action, ToolCallAction) or action.tool_name not in TOOL_NAMES:
        raise ValueError("TOOL_NOT_ALLOWED")
    scope, args = run_input.scope, action.arguments
    window = args.window or run_input.window
    if window.from_time < run_input.window.from_time or window.to_time > run_input.window.to_time:
        raise ValueError("WINDOW_OUTSIDE_RUN")
    allowed = {
        "get_patient_snapshot": set(), "get_loop_state": set(),
        "get_lab_reports": {"window", "limit", "report_id", "report_version"},
        "get_progress_notes": {"window", "limit"},
        "get_result_acknowledgements": {"window", "limit"},
        "get_evidence": {"evidence_id"},
    }
    effective_fields = {key for key in args.model_fields_set if getattr(args, key) is not None and not (key == "limit" and args.limit == 20)}
    if effective_fields - allowed[action.tool_name]:
        raise ValueError("UNSUPPORTED_TOOL_ARGUMENT")
    result = {"tool_name": action.tool_name, "window": window.model_dump(mode="json"), "query_succeeded": True,
              "complete": True, "status": "empty", "items": [], "sources": []}
    with Session(bind, join_transaction_mode="create_savepoint") as db:
        # Statement timeout protects PostgreSQL reads as part of the total runtime deadline.
        if db.bind.dialect.name == "postgresql":
            from sqlalchemy import text
            db.execute(text("SELECT set_config('statement_timeout', :timeout, true)"), {"timeout": str(max(1, int(remaining * 1000)))})
        require_scope(db, scope.encounter_id, scope.actor_id)
        loop = db.get(OpenLoop, scope.loop_id)
        if not loop or (loop.patient_id, loop.encounter_id, loop.intent_id) != (scope.patient_id, scope.encounter_id, scope.intent_id):
            raise ValueError("SCOPE_MISMATCH")
        if action.tool_name == "get_patient_snapshot":
            encounter = db.get(Encounter, scope.encounter_id)
            result["items"] = [{"patient_id": loop.patient_id, "encounter_id": loop.encounter_id, "admitted_at": utc(encounter.admitted_at).isoformat()}]
        elif action.tool_name == "get_loop_state":
            result["items"] = [{"loop_id": loop.id, "version": loop.version, "status": loop.status, "matching": loop.matching_criteria, "current_evidence_id": loop.current_evidence_id}]
        elif action.tool_name == "get_evidence":
            evidence = db.get(EvidenceNode, args.evidence_id or loop.current_evidence_id) if (args.evidence_id or loop.current_evidence_id) else None
            if evidence and evidence.id != loop.current_evidence_id:
                raise ValueError("EVIDENCE_NOT_CURRENT_FOR_LOOP")
            if evidence:
                if (evidence.patient_id, evidence.encounter_id) != (scope.patient_id, scope.encounter_id):
                    raise ValueError("EVIDENCE_SCOPE_MISMATCH")
                content = dict(evidence.content)
                if len(str(content.get("note", ""))) > 6000:
                    content["note"] = str(content["note"])[:6000]
                    result["complete"] = False
                result["items"] = [{"id": evidence.id, "content": content}]
                report = db.scalar(select(LabReport).where(LabReport.event_id == evidence.event_id))
                if report:
                    item = snapshot(db, "lab_report", report.id)
                    if len(item["text"]) > 6000:
                        item["text"] = item["text"][:6000]
                        result["complete"] = False
                    result["sources"] = [item]
        else:
            if action.tool_name == "get_lab_reports":
                query = select(LabReport).where(LabReport.patient_id == scope.patient_id, LabReport.encounter_id == scope.encounter_id)
                for key in ("specimen_id", "order_id", "report_id"):
                    value = loop.matching_criteria.get(key)
                    if value:
                        query = query.where(getattr(LabReport, key) == value)
                if args.report_id:
                    query = query.where(LabReport.report_id == args.report_id)
                if args.report_version:
                    query = query.where(LabReport.report_version == args.report_version)
                query = query.where(LabReport.reported_at >= window.from_time, LabReport.reported_at <= window.to_time)
                rows = db.scalars(query.order_by(LabReport.id.desc()).limit(args.limit + 1)).all()
                kind = "lab_report"
            elif action.tool_name == "get_progress_notes":
                query = select(MedicalRecord).where(MedicalRecord.patient_id == scope.patient_id, MedicalRecord.encounter_id == scope.encounter_id,
                    MedicalRecord.created_at >= window.from_time, MedicalRecord.created_at <= window.to_time)
                rows = db.scalars(query.order_by(MedicalRecord.id.desc()).limit(args.limit + 1)).all()
                kind = "medical_record"
            else:
                query = select(ResultAcknowledgement).where(ResultAcknowledgement.loop_id == loop.id, ResultAcknowledgement.evidence_id == loop.current_evidence_id,
                    ResultAcknowledgement.created_at >= window.from_time, ResultAcknowledgement.created_at <= window.to_time)
                rows = db.scalars(query.order_by(ResultAcknowledgement.id.desc()).limit(args.limit + 1)).all()
                kind = "result_acknowledgement"
            result["complete"] = len(rows) <= args.limit
            remaining_text = 24000
            for row in rows[:args.limit]:
                if kind == "lab_report" and not matches_report(loop, row):
                    continue
                item = snapshot(db, kind, row.id)
                allowed_text = max(0, min(6000, remaining_text))
                if len(item["text"]) > allowed_text:
                    item["text"] = item["text"][:allowed_text]
                    result["complete"] = False
                remaining_text -= len(item["text"])
                result["sources"].append(item)
                result["items"].append({"id": row.id, **({"report_id": row.report_id, "report_version": row.report_version, "test_name": row.test_name,
                    "specimen_id": row.specimen_id, "order_id": row.order_id} if kind == "lab_report" else {}), "source_ref": {k:item[k] for k in ("source_type", "source_id", "source_version")}})
    result["status"] = "partial" if not result["complete"] else "found" if result["items"] else "empty"
    return result
