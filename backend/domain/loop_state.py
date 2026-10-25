from __future__ import annotations

from dataclasses import dataclass

from ..contracts.loops import LoopStatus, LoopTransitionRequest


class InvalidLoopTransition(ValueError):
    """Raised when a workflow state change lacks the required evidence/action."""


@dataclass(frozen=True)
class TransitionResult:
    status: LoopStatus
    reason: str


def transition_loop(request: LoopTransitionRequest) -> TransitionResult:
    current, target = request.current, request.target
    if current == target:
        return TransitionResult(target, "状态未变化")
    allowed = {
        LoopStatus.PROPOSED: {LoopStatus.WAITING_EVENT, LoopStatus.REJECTED, LoopStatus.CANCELLED},
        LoopStatus.WAITING_EVENT: {LoopStatus.RESULT_AVAILABLE, LoopStatus.CANCELLED},
        LoopStatus.RESULT_AVAILABLE: {LoopStatus.ACKNOWLEDGED, LoopStatus.CANCELLED},
        LoopStatus.ACKNOWLEDGED: {LoopStatus.RESOLVED, LoopStatus.CANCELLED},
        LoopStatus.RESOLVED: set(),
        LoopStatus.REJECTED: set(),
        LoopStatus.CANCELLED: set(),
    }
    if request.intent_type in {"ACTION", "MONITOR"}:
        # An execution task has no lab report to wait for or acknowledge.
        allowed[LoopStatus.WAITING_EVENT].add(LoopStatus.RESOLVED)
        allowed[LoopStatus.RESULT_AVAILABLE].add(LoopStatus.RESOLVED)
    if request.report_revision and request.has_evidence and request.intent_type in {"FOLLOW_RESULT", "RECHECK"}:
        allowed[LoopStatus.ACKNOWLEDGED].add(LoopStatus.RESULT_AVAILABLE)
        allowed[LoopStatus.RESOLVED].add(LoopStatus.RESULT_AVAILABLE)
    if target not in allowed[current]:
        raise InvalidLoopTransition(f"不允许从 {current} 转换到 {target}")
    if target == LoopStatus.RESULT_AVAILABLE and not request.has_evidence:
        raise InvalidLoopTransition("没有证据不能进入 RESULT_AVAILABLE")
    if target == LoopStatus.ACKNOWLEDGED and not request.clinician_confirmed:
        raise InvalidLoopTransition("没有医生确认不能进入 ACKNOWLEDGED")
    if target == LoopStatus.RESOLVED:
        if not request.clinician_confirmed:
            raise InvalidLoopTransition("完成处理必须有医生确认")
        if not request.resolution_note or not request.resolution_note.strip():
            raise InvalidLoopTransition("完成处理必须提供处理结论")
    if target == LoopStatus.CANCELLED and not request.resolution_note:
        raise InvalidLoopTransition("取消任务必须提供原因")
    return TransitionResult(target, "状态转换通过")
