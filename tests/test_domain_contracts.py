from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from backend.contracts.loops import LoopStatus, LoopTransitionRequest
from backend.domain.loop_state import InvalidLoopTransition, transition_loop


def request(target, **kwargs):
    return LoopTransitionRequest(
        current=LoopStatus.WAITING_EVENT,
        target=target,
        occurred_at=datetime.now(timezone.utc),
        **kwargs,
    )


def test_result_requires_evidence():
    with pytest.raises(InvalidLoopTransition):
        transition_loop(request(LoopStatus.RESULT_AVAILABLE))


def test_acknowledge_requires_clinician_confirmation():
    with pytest.raises(InvalidLoopTransition):
        transition_loop(
            LoopTransitionRequest(
                current=LoopStatus.RESULT_AVAILABLE,
                target=LoopStatus.ACKNOWLEDGED,
                occurred_at=datetime.now(timezone.utc),
                has_evidence=True,
            )
        )


def test_resolution_requires_note_and_confirmation():
    with pytest.raises(InvalidLoopTransition):
        transition_loop(
            LoopTransitionRequest(
                current=LoopStatus.ACKNOWLEDGED,
                target=LoopStatus.RESOLVED,
                occurred_at=datetime.now(timezone.utc),
                clinician_confirmed=True,
            )
        )


def test_dto_rejects_unknown_fields_and_naive_time():
    with pytest.raises(ValidationError):
        LoopTransitionRequest(
            current=LoopStatus.PROPOSED,
            target=LoopStatus.WAITING_EVENT,
            occurred_at="2026-10-19T10:00:00",
            unexpected=True,
        )
