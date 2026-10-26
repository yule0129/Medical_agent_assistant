"""The Agent decision loop: observe tool results, choose an action, act, stop."""
import time
from agent.contracts import AgentRunResult, HumanReviewAction
from agent.llm import deepseek as providers


def investigate(run_input, provider, reader, clock=time.monotonic, context_extra=None):
    started = clock()
    deadline = started + run_input.budget.timeout_seconds
    history, steps = [], []
    model_requests = tool_calls = 0
    context = run_input.model_dump(mode="json", exclude={"scope"})
    context["trusted_context"] = context_extra or {}
    def finish(reason, action=None):
        return AgentRunResult(provider=provider.name, model=provider.model, stop_reason=reason,
            final_action=action or HumanReviewAction(action="request_review", reason=reason),
            model_requests=model_requests, tool_calls=tool_calls), steps, history
    while model_requests < run_input.budget.max_model_requests:
        if clock() >= deadline:
            return finish("BUDGET_EXHAUSTED")
        model_requests += 1
        step_start = clock()
        try:
            action, usage = provider.next_action(context, history, deadline - clock())
        except Exception as exc:
            steps.append({"action": "MODEL_ERROR", "input": {}, "result": {"error_type": type(exc).__name__, "error_code": providers.safe_error_code(exc)}, "duration_ms": int((clock()-step_start)*1000)})
            code = providers.safe_error_code(exc)
            if code.startswith(("MODEL_OUTPUT_INVALID_", "MODEL_EMPTY_CONTENT", "MODEL_ACTION_NOT_JSON")):
                # A format repair is another visible, budgeted model request.
                history.append({"tool_name": "model_validation", "query_succeeded": False, "complete": False,
                    "window": run_input.window.model_dump(mode="json"), "sources": [], "items": [], "error_code": code})
                continue
            return finish("MODEL_ERROR")
        steps.append({"action": "DECIDE", "input": {"model_request": model_requests}, "result": {"decision": action.model_dump(mode="json"), "usage": usage}, "duration_ms": int((clock()-step_start)*1000)})
        if clock() >= deadline:
            return finish("BUDGET_EXHAUSTED")
        if action.action != "call_tool":
            return finish({"submit_candidate": "CANDIDATE_READY", "wait": "WAITING_EVENT", "request_review": "NEEDS_REVIEW"}[action.action], action)
        if tool_calls >= run_input.budget.max_tool_calls:
            return finish("BUDGET_EXHAUSTED")
        tool_calls += 1
        step_start = clock()
        try:
            result = reader(action, deadline - clock())
        except Exception as exc:
            result = {"tool_name": action.tool_name, "window": (action.arguments.window or run_input.window).model_dump(mode="json"),
                "status": "error", "query_succeeded": False, "complete": False, "sources": [], "items": [], "error_type": type(exc).__name__,
                "error_code": str(exc) if isinstance(exc, ValueError) else "TOOL_FAILED"}
        history.append(result)
        steps.append({"action": "ACT", "tool_name": action.tool_name, "input": action.arguments.model_dump(mode="json", exclude_none=True), "result": result, "duration_ms": int((clock()-step_start)*1000)})
        if clock() >= deadline:
            return finish("BUDGET_EXHAUSTED")
        if not result["query_succeeded"]:
            return finish("TOOL_ERROR")
    return finish("BUDGET_EXHAUSTED")
