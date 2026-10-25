"""Rule-based, scoped report investigation. Model text never controls state."""
import hashlib
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from ..contracts.loops import LoopTransitionRequest
from ..domain.loop_state import transition_loop
from ..domain.matching import matches_report, normalized_test
from ..models import (AgentRun, AuditLog, ClinicalEvent, ClinicalIntent, Encounter,
                      EvidenceNode, Finding, LabReport, OpenLoop, ResultAcknowledgement)
from ..services.run_service import add_step
from ..tools.repository_tools import build_default_registry
from ..tools.schemas import ToolContext, ToolStatus


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def current_evidence(db, loop):
    if loop.current_evidence_id:
        evidence = db.get(EvidenceNode, loop.current_evidence_id)
        if evidence and (evidence.patient_id, evidence.encounter_id) == (loop.patient_id, loop.encounter_id):
            return evidence
        raise ValueError("任务证据范围不一致")
    return db.scalar(select(EvidenceNode).where(EvidenceNode.evidence_key == f"TASK-{loop.id}-RESULT"))


def require_latest_evidence(db, evidence):
    if evidence and evidence.content.get("report_id"):
        latest = db.scalar(select(LabReport.report_version).where(LabReport.patient_id == evidence.patient_id,
            LabReport.encounter_id == evidence.encounter_id, LabReport.report_id == evidence.content["report_id"]).order_by(LabReport.report_version.desc()))
        if latest != evidence.content.get("report_version"):
            raise ValueError("报告已有更新版本，请刷新并核对最新报告")


def investigate(db, loop, event):
    """One immutable run per triggering event and physician-supplied criteria."""
    if loop.status in {"CANCELLED", "REJECTED"}:
        return {"loop_id": loop.id, "reason": "TASK_CANCELLED"}
    intent = db.get(ClinicalIntent, loop.intent_id)
    if intent.intent_type not in {"FOLLOW_RESULT", "RECHECK"}:
        return {"loop_id": loop.id, "reason": "NOT_A_REPORT_TASK"}
    criteria = loop.matching_criteria or {}
    fingerprint = hashlib.sha256(json.dumps(criteria, sort_keys=True).encode()).hexdigest()[:16]
    run_key = f"REPORT-{event.id}-{loop.id}-{fingerprint}"
    existing = db.scalar(select(AgentRun).where(AgentRun.run_key == run_key))
    if existing:
        return {"loop_id": loop.id, "run_id": existing.id, "reason": existing.stop_reason, "duplicate": True}
    prior = db.scalar(select(AgentRun).where(AgentRun.intent_id == intent.id).order_by(AgentRun.id.desc()))
    now = datetime.now(timezone.utc)
    run = AgentRun(run_key=run_key, intent_id=intent.id, loop_id=loop.id, prior_run_id=prior.id if prior else None,
        trigger_event_id=event.id, plan_version=(prior.plan_version + (prior.loop_id != loop.id) if prior else 1),
        provider="rules", status="RUNNING", started_at=now)
    db.add(run)
    db.flush()
    add_step(db, run, 1, "OBSERVE", result={"event_id": event.id, "criteria": criteria})
    add_step(db, run, 2, "PLAN", result={"steps": ["get_lab_reports", "verify_identity_and_version", "check_structured_acknowledgements", "propose_review"]})

    def stop(reason, **extra):
        run.status, run.stop_reason, run.finished_at = "SUSPENDED", reason, datetime.now(timezone.utc)
        loop.workflow_memory = {**(loop.workflow_memory or {}), "last_run_id": run.id,
            "last_plan": {"version": run.plan_version, "target": loop.waiting_for}, "stop_reason": reason,
            "waiting_for": loop.waiting_for, "dependencies": [loop.parent_loop_id] if loop.parent_loop_id else []}
        if reason not in {"REQUIRES_CLINICIAN_REVIEW", "ALREADY_ACKNOWLEDGED", "ALREADY_LINKED"}:
            loop.state_reason = {"IDENTITY_REQUIRED": "请补充标本号、医嘱号或报告号后匹配", "AMBIGUOUS_REPORTS": "有多份不同报告符合条件，请补充标本号或报告号核实", "TOOL_ERROR": "报告查询失败，任务继续保留", "SOURCE_CONFLICT": "报告来源或身份不一致，需人工核对", "SCOPE_INCOMPLETE": "查询结果被截断，请补充更精确的匹配编号"}.get(reason, "等待符合匹配条件的报告")
        return {"loop_id": loop.id, "run_id": run.id, "reason": reason, **extra}

    if not any(criteria.get(k) for k in ("report_id", "specimen_id", "order_id")):
        return stop("IDENTITY_REQUIRED")
    context = ToolContext(actor_id=event.payload["doctor_id"], patient_id=loop.patient_id, encounter_id=loop.encounter_id,
        report_id=criteria.get("report_id"), specimen_id=criteria.get("specimen_id"), order_id=criteria.get("order_id"),
        from_time=utc(db.get(Encounter, loop.encounter_id).admitted_at), to_time=now + timedelta(minutes=5), limit=100)
    tool = build_default_registry().call("get_lab_reports", db, context)
    add_step(db, run, 3, "ACT", "get_lab_reports", {"status": tool.status.value, "complete": tool.complete, "sources": tool.sources, "query": tool.query})
    if tool.status == ToolStatus.ERROR:
        return stop("TOOL_ERROR")
    if not tool.complete:
        return stop("SCOPE_INCOMPLETE")
    latest = {}
    for item in tool.items:
        report = db.get(LabReport, item["id"])
        if not matches_report(loop, report):
            continue
        bound = (loop.workflow_memory or {}).get("bound_report_id")
        if bound and bound != report.report_id:
            continue
        if report.report_id not in latest or latest[report.report_id].report_version < report.report_version:
            latest[report.report_id] = report
    if len(latest) > 1:
        return stop("AMBIGUOUS_REPORTS")
    if not latest:
        return stop("WAITING_EXTERNAL_EVENT")
    report = next(iter(latest.values()))
    # Serialize creation of a shared evidence node when two confirmed loops
    # legitimately refer to the same immutable report version.
    report = db.scalar(select(LabReport).where(LabReport.id == report.id).with_for_update())
    encounter = db.get(Encounter, loop.encounter_id)
    source = db.get(ClinicalEvent, report.event_id)
    if utc(report.reported_at) < utc(encounter.admitted_at) or not source or source.event_type != "LAB_RESULT_CREATED" or utc(source.occurred_at) != utc(report.reported_at) or (source.patient_id, source.encounter_id) != (loop.patient_id, loop.encounter_id):
        return stop("SOURCE_CONFLICT")
    for key in ("report_id", "report_version", "test_name", "specimen_id", "order_id", "result_text"):
        if source.payload.get(key) != getattr(report, key):
            return stop("SOURCE_CONFLICT")
    previous = current_evidence(db, loop)
    if previous:
        if previous.content.get("report_id") != report.report_id:
            return stop("SOURCE_CONFLICT")
        if previous.content.get("report_version", 0) >= report.report_version:
            return stop("ALREADY_LINKED", evidence_id=previous.id)
    evidence = db.scalar(select(EvidenceNode).where(EvidenceNode.evidence_key == f"LAB-{report.id}"))
    if evidence is None:
        evidence = EvidenceNode(evidence_key=f"LAB-{report.id}", event_id=report.event_id,
            patient_id=loop.patient_id, encounter_id=loop.encounter_id, source_type="STRUCTURED_LAB_REPORT",
            source_ref=f"{report.report_id}:v{report.report_version}", captured_at=now, verification_status="IDENTITY_AND_VERSION_VERIFIED",
            content={"note": report.result_text, "report_id": report.report_id, "report_version": report.report_version,
                "test_name": report.test_name, "specimen_id": report.specimen_id, "order_id": report.order_id,
                "reported_at": utc(report.reported_at).isoformat(), "source": source.source})
        db.add(evidence)
        db.flush()
    ack = db.scalar(select(ResultAcknowledgement).where(ResultAcknowledgement.loop_id == loop.id, ResultAcknowledgement.evidence_id == evidence.id))
    add_step(db, run, 4, "VERIFY", result={"evidence_id": evidence.id, "report_id": report.report_id, "report_version": report.report_version, "existing_acknowledgement": ack.id if ack else None})
    if ack:
        return stop("ALREADY_ACKNOWLEDGED", evidence_id=evidence.id)
    transition_loop(LoopTransitionRequest(current=loop.status, target="RESULT_AVAILABLE", has_evidence=True,
        report_revision=previous is not None, intent_type=intent.intent_type, occurred_at=now))
    before = {"status": loop.status, "version": loop.version, "evidence_id": loop.current_evidence_id}
    for old in db.scalars(select(Finding).where(Finding.loop_id == loop.id, Finding.evidence_id != evidence.id, Finding.status != "SUPERSEDED")):
        old.status, old.version = "SUPERSEDED", old.version + 1
    loop.current_evidence_id, loop.status, loop.updated_at = evidence.id, "RESULT_AVAILABLE", now
    loop.version += 1
    loop.state_reason = f"报告 {report.report_id} v{report.report_version} 已核对，等待医生查看与处理"
    loop.workflow_memory = {**(loop.workflow_memory or {}), "bound_report_id": report.report_id, "report_version": report.report_version, "current_evidence_id": evidence.id}
    finding = db.scalar(select(Finding).where(Finding.loop_id == loop.id, Finding.evidence_id == evidence.id))
    if finding is None:
        finding = Finding(loop_id=loop.id, evidence_id=evidence.id,
            claim=f"{report.test_name}报告 {report.report_id} v{report.report_version} 已返回，请医生核对。", confidence=1.0,
            searched_sources=[{"source": "本系统结构化结果确认记录", "query_succeeded": True,
                "loop_id": loop.id, "evidence_id": evidence.id, "acknowledgement_found": False,
                "through": now.isoformat(), "grace_until": (now + timedelta(minutes=5)).isoformat(),
                "coverage": "仅核对本系统明确绑定该报告版本的查看确认，不代表已覆盖医院全部病程或其他系统；报告刚到达不判断医生遗漏。"}],
            status="PENDING_REVIEW", created_at=now)
        db.add(finding)
        db.flush()
    db.add(AuditLog(actor_type="WORKER", actor_id="rules", action="REPORT_MATCHED", resource_type="OpenLoop",
        resource_id=str(loop.id), old_state=before, new_state={"status": loop.status, "evidence_id": evidence.id, "report_version": report.report_version}, run_id=run.id, created_at=now))
    return stop("REQUIRES_CLINICIAN_REVIEW", evidence_id=evidence.id, finding_id=finding.id)


def resume_for_event(db, event):
    loops = db.scalars(select(OpenLoop).where(OpenLoop.patient_id == event.patient_id, OpenLoop.encounter_id == event.encounter_id,
        OpenLoop.status.notin_(["CANCELLED", "REJECTED"])).order_by(OpenLoop.id).with_for_update()).all()
    report = db.scalar(select(LabReport).where(LabReport.event_id == event.id))
    if report is None:
        raise ValueError("typed report missing for lab event")
    result = []
    for loop in loops:
        criteria = loop.matching_criteria or {}
        if normalized_test(criteria.get("test_name") or loop.waiting_for) != normalized_test(report.test_name):
            continue
        if any(criteria.get(key) and criteria[key] != getattr(report, key) for key in ("specimen_id", "order_id", "report_id")):
            continue
        if (loop.workflow_memory or {}).get("bound_report_id") not in {None, report.report_id}:
            continue
        result.append(investigate(db, loop, event))
    return result


def backfill_loop(db, loop):
    reports = db.scalars(select(LabReport).where(LabReport.patient_id == loop.patient_id, LabReport.encounter_id == loop.encounter_id).order_by(LabReport.id.desc()))
    report = next((report for report in reports if matches_report(loop, report)), None)
    return investigate(db, loop, db.get(ClinicalEvent, report.event_id)) if report else None
