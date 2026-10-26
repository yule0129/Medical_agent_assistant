"""Assemble one investigation context and persist the next waiting plan."""
import os
from agent.contracts import AgentRunInput, RunBudget
from backend.services.report_service import utc


def build_context(loop, intent, encounter, job, run, event, timestamp):
    run_input = AgentRunInput(scope={"actor_id": job.doctor_id, "patient_id": loop.patient_id, "encounter_id": loop.encounter_id,
        "intent_id": loop.intent_id, "loop_id": loop.id, "expected_loop_version": loop.version}, trigger_event_id=event.id,
        goal=intent.raw_text, loop_status=loop.status, matching=loop.matching_criteria,
        window={"from_time": utc(encounter.admitted_at), "to_time": timestamp}, prior_run_id=run.prior_run_id, plan_version=run.plan_version,
        budget=RunBudget(max_tool_calls=int(os.getenv("AGENT_MAX_TOOL_CALLS", "8")), max_model_requests=int(os.getenv("AGENT_MAX_MODEL_REQUESTS", "8")),
            timeout_seconds=float(os.getenv("AGENT_TIMEOUT_SECONDS", "30"))))
    extra = {"trigger": {"type": event.event_type, "text": str(event.payload.get("text", ""))[:12000]},
             "memory": {k:v for k,v in (loop.workflow_memory or {}).items() if k.startswith("agent_")}}
    return run_input, extra


def remember_outcome(loop, run_input, result, run_id):
    if loop.version == run_input.scope.expected_loop_version:
        loop.workflow_memory = {**(loop.workflow_memory or {}), "agent_last_run_id": run_id,
            "agent_stop_reason": result.stop_reason, "agent_waiting_for": getattr(result.final_action, "waiting_for", []),
            "agent_last_plan": result.final_action.model_dump(mode="json")}
