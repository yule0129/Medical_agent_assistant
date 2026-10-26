from copy import deepcopy

import pytest
from pydantic import ValidationError

from agent.contracts import AgentRunInput, AgentRunResult, RunBudget, action_json_schema, parse_action

WINDOW = {"from_time": "2026-10-25T09:00:00+08:00", "to_time": "2026-10-25T15:00:00+08:00"}


def candidate_action(kind="RESPONSE_NOT_FOUND"):
    return {
        "action": "submit_candidate", "reason": "报告已返回，已查询指定来源。",
        "candidate": {
            "kind": kind, "summary": "本次检索范围内未发现明确响应，待医生核对。",
            "evidence": [{"source_type": "lab_report", "source_id": 17, "source_version": 1, "quote": "合成血培养报告"}],
            "searched_sources": [
                {"tool_name": name, "window": WINDOW, "query_succeeded": True, "complete": True}
                for name in ("get_result_acknowledgements", "get_progress_notes")
            ],
        },
    }


def test_run_input_keeps_scope_and_version_separate_from_model_actions():
    value = AgentRunInput.model_validate({
        "scope": {"actor_id": 1, "patient_id": 2, "encounter_id": 3, "intent_id": 4, "loop_id": 5, "expected_loop_version": 2},
        "trigger_event_id": 6, "goal": "跟进血培养", "loop_status": "WAITING_EVENT",
        "matching": {"test_name": "血培养", "specimen_id": "BC-001"}, "window": WINDOW,
    })
    assert value.scope.expected_loop_version == 2
    assert value.budget.max_tool_calls == 8
    assert value.model_dump(mode="json")["window"] == WINDOW


@pytest.mark.parametrize("action", [
    {"action": "call_tool", "reason": "核验本次报告", "tool_name": "get_lab_reports", "arguments": {"report_id": "BC-R001", "window": WINDOW}},
    candidate_action(),
    {"action": "wait", "reason": "等待药敏", "waiting_for": ["LAB_RESULT_CREATED"]},
    {"action": "request_review", "reason": "培养已阅无法关联具体报告"},
])
def test_actions_roundtrip_and_provider_schema(action):
    value = parse_action(action)
    assert parse_action(value.model_dump(mode="json")) == value
    assert action_json_schema()["discriminator"]["propertyName"] == "action"


@pytest.mark.parametrize("action", [
    {"action": "resolve", "reason": "已完成"},
    {"action": "call_tool", "reason": "执行 SQL", "tool_name": "execute_sql"},
    {"action": "call_tool", "reason": "修改状态", "tool_name": "get_loop_state", "arguments": {"status": "RESOLVED"}},
    {"action": "call_tool", "reason": "换患者", "tool_name": "get_lab_reports", "arguments": {"patient_id": 99}},
    {"action": "call_tool", "reason": "换医生", "tool_name": "get_lab_reports", "actor_id": 99},
    {"action": "call_tool", "reason": "读取过多", "tool_name": "get_progress_notes", "arguments": {"limit": 101}},
    {"action": "request_review", "reason": "   "},
    {"action": "wait", "reason": "等待", "waiting_for": []},
])
def test_rejects_write_actions_scope_override_and_unbounded_reads(action):
    with pytest.raises(ValidationError):
        parse_action(action)


@pytest.mark.parametrize("field", ["query_succeeded", "complete"])
def test_failed_or_partial_search_cannot_claim_missing_response(field):
    action = candidate_action()
    action["candidate"]["searched_sources"][0][field] = False
    with pytest.raises(ValidationError):
        parse_action(action)
    action["candidate"]["kind"] = "AMBIGUOUS"
    assert parse_action(action).candidate.kind == "AMBIGUOUS"


def test_negative_claim_requires_both_response_sources():
    action = candidate_action()
    action["candidate"]["searched_sources"].pop()
    with pytest.raises(ValidationError):
        parse_action(action)


@pytest.mark.parametrize("change", ["no_evidence", "no_review", "missing_follow_up", "unexpected_follow_up"])
def test_candidates_require_evidence_review_and_typed_dependencies(change):
    action = candidate_action()
    item = action["candidate"]
    if change == "no_evidence": item["evidence"] = []
    if change == "no_review": item["requires_clinician_review"] = False
    if change == "missing_follow_up": item["kind"] = "FOLLOW_UP"
    if change == "unexpected_follow_up": item["follow_up"] = {"test_name": "药敏", "specimen_id": "BC-001"}
    with pytest.raises(ValidationError):
        parse_action(action)


def test_follow_up_retains_report_source_and_strong_matching_identifier():
    action = candidate_action("FOLLOW_UP")
    action["candidate"]["follow_up"] = {"test_name": "药敏", "specimen_id": "BC-001"}
    assert parse_action(action).candidate.follow_up.specimen_id == "BC-001"
    action["candidate"]["follow_up"] = {"test_name": "药敏"}
    with pytest.raises(ValidationError):
        parse_action(action)


@pytest.mark.parametrize("window", [
    {**WINDOW, "from_time": "2026-10-25T09:00:00"},
    {"from_time": WINDOW["to_time"], "to_time": WINDOW["from_time"]},
])
def test_search_window_must_be_timezone_aware_and_ordered(window):
    with pytest.raises(ValidationError):
        parse_action({"action": "call_tool", "reason": "查报告", "tool_name": "get_lab_reports", "arguments": {"window": window}})


@pytest.mark.parametrize("budget", [{"max_tool_calls": 0}, {"max_model_requests": True}, {"timeout_seconds": float("inf")}])
def test_invalid_budgets_rejected(budget):
    with pytest.raises(ValidationError):
        RunBudget.model_validate(budget)


def test_runtime_errors_must_transfer_to_review_not_claim_success():
    payload = {"provider": "test", "model": "contract-only", "stop_reason": "BUDGET_EXHAUSTED", "model_requests": 8, "tool_calls": 2,
               "final_action": {"action": "request_review", "reason": "预算耗尽，请人工核对"}}
    assert AgentRunResult.model_validate(payload).stop_reason == "BUDGET_EXHAUSTED"
    invalid = deepcopy(payload)
    invalid["final_action"] = candidate_action()
    with pytest.raises(ValidationError):
        AgentRunResult.model_validate(invalid)
