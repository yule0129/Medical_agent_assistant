"""Doctor-facing feedback derived from persisted agent jobs and reviewed proposals."""
from sqlalchemy import select, func

from backend.models import OpenLoop, Patient
from agent.persistence.models import AgentJob, AgentProposal
from backend.services.intent_service import require_scope
from agent.guard.evidence import recheck_snapshots

LABELS = {"RESPONSE_FOUND": "发现医生响应，待核对", "RESPONSE_NOT_FOUND": "检索范围内未发现明确响应，待核对", "FOLLOW_UP": "发现后续跟进计划，待批准", "AMBIGUOUS": "证据存在歧义，需核对"}


def proposal_stale(db, loop, proposal):
    if proposal.status != "PENDING_REVIEW":
        return False
    if proposal.expected_loop_version != loop.version or proposal.evidence_id != loop.current_evidence_id:
        return True
    try:
        recheck_snapshots(db, loop, proposal.content)
    except ValueError:
        return True
    return False


def doctor_feedback(db, doctor_id, encounter_id=None):
    if encounter_id is not None:
        require_scope(db, encounter_id, doctor_id)
    query = select(OpenLoop).order_by(OpenLoop.id.desc())
    if encounter_id is not None:
        query = query.where(OpenLoop.encounter_id == encounter_id)
    loops = []
    authorized = {}
    for loop in db.scalars(query):
        if loop.encounter_id not in authorized:
            try:
                require_scope(db, loop.encounter_id, doctor_id)
                authorized[loop.encounter_id] = True
            except ValueError:
                authorized[loop.encounter_id] = False
        if authorized[loop.encounter_id]:
            loops.append(loop)
    ids = [loop.id for loop in loops]
    jobs = {r.loop_id: r for r in db.scalars(select(AgentJob).where(AgentJob.id.in_(select(func.max(AgentJob.id)).where(AgentJob.loop_id.in_(ids)).group_by(AgentJob.loop_id))))} if ids else {}
    proposals = {r.loop_id: r for r in db.scalars(select(AgentProposal).where(AgentProposal.id.in_(select(func.max(AgentProposal.id)).where(AgentProposal.loop_id.in_(ids)).group_by(AgentProposal.loop_id))))} if ids else {}
    items = []
    for loop in loops:
        job, proposal = jobs.get(loop.id), proposals.get(loop.id)
        if not job and not proposal:
            continue
        stale = proposal_stale(db, loop, proposal) if proposal else False
        candidate = proposal.content["candidate"] if proposal else None
        state, title, summary, action = "waiting", "正在持续跟进", "等待相关事件后继续调查。", "查看跟进"
        attention = False
        if loop.status in {"CANCELLED", "REJECTED"}:
            state, title, summary = "closed", "该跟进已取消", "保留历史调查与医生决定。"
        elif proposal and proposal.status == "PENDING_REVIEW":
            attention = True
            if stale:
                state, title, summary, action = "stale", "原候选已过期，需重新调查", "任务、报告或引用原文已经更新，请使用当前证据重新调查。", "重新核对"
            else:
                state, title, summary, action = "pending_review", LABELS[candidate["kind"]], candidate["summary"], "查看证据并审核"
        elif job and job.status in {"PENDING", "RUNNING"}:
            state, title, summary = ("running", "Agent 正在调查", "正在核对报告及医生响应，完成后自动反馈。") if job.status == "RUNNING" else ("queued", "Agent 等待调查", "调查已排队，等待后台 Worker 处理。")
        elif job and job.status == "FAILED":
            state, title, summary, action, attention = "failed", "Agent 调查未完成", "本次调查失败，任务仍保留，请查看原因并选择重试或人工处理。", "查看并重试", True
        elif job and job.result.get("stop_reason") in {"NEEDS_REVIEW", "STALE_CONTEXT"}:
            state, title, summary, action, attention = "needs_review", "Agent 需要医生协助核对", "当前证据存在歧义、不完整或已经变化，尚未形成可提交候选。", "查看调查情况", True
        elif proposal and proposal.status in {"ACCEPTED", "REJECTED"}:
            state, title, summary = "reviewed", "医生已处理 Agent 候选", "已接受候选。临床任务是否完成，以任务状态为准。" if proposal.status == "ACCEPTED" else "已驳回候选，保留原文与审核说明。"
            if proposal.child_loop_id:
                summary = f"已批准后续任务 #{proposal.child_loop_id}；原任务的处理完成仍需单独记录。"
        elif job and job.result.get("stop_reason") == "WAITING_EVENT":
            summary = job.result.get("final_action", {}).get("reason") or summary
        if loop.status == "RESOLVED" and not attention and state not in {"queued", "running", "failed", "needs_review"}:
            state, title = "completed", "临床任务已完成，保留调查记录"
        patient = db.get(Patient, loop.patient_id)
        items.append({"loop_id": loop.id, "patient_id": loop.patient_id, "patient": patient.name, "encounter_id": loop.encounter_id,
            "goal": loop.waiting_for, "loop_version": loop.version, "loop_status": loop.status,
            "state": state, "title": title, "summary": summary, "requires_attention": attention, "action_label": action,
            "job_id": job.id if job else None, "job_status": job.status if job else None,
            "proposal_id": proposal.id if proposal else None, "proposal_version": proposal.version if proposal else None,
            "candidate_kind": candidate["kind"] if candidate else None, "child_loop_id": proposal.child_loop_id if proposal else None})
    rank = {"pending_review": 0, "needs_review": 1, "failed": 2, "stale": 3, "running": 4, "queued": 5}
    items.sort(key=lambda item: (rank.get(item["state"], 6), -item["loop_id"]))
    return {"items": items, "counts": {"attention": sum(item["requires_attention"] for item in items),
        "pending_review": sum(item["state"] == "pending_review" for item in items),
        "running": sum(item["state"] in {"queued", "running"} for item in items)}}
