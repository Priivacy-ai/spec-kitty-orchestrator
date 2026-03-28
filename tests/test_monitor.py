from __future__ import annotations

import json

from spec_kitty_orchestrator.agents.base import InvocationResult
from spec_kitty_orchestrator.monitor import (
    extract_review_feedback,
    extract_review_verdict,
    extract_text_output,
    is_review_approved,
)


def _result(stdout: str, *, exit_code: int = 0, success: bool = True) -> InvocationResult:
    return InvocationResult(
        success=success,
        exit_code=exit_code,
        stdout=stdout,
        stderr="",
        duration_seconds=1.0,
    )


def test_extract_text_output_reads_qwen_event_stream() -> None:
    stdout = json.dumps(
        [
            {
                "message": {
                    "content": [
                        {"type": "thinking", "thinking": "hidden"},
                        {"type": "text", "text": "VERDICT: APPROVED\nLooks good."},
                    ]
                }
            }
        ]
    )

    text = extract_text_output(_result(stdout))

    assert text == "VERDICT: APPROVED\nLooks good."


def test_is_review_approved_requires_explicit_approved_verdict() -> None:
    approved = _result(
        json.dumps(
            [
                {
                    "message": {
                        "content": [{"type": "text", "text": "VERDICT: APPROVED\nEvidence follows."}]
                    }
                }
            ]
        )
    )
    ambiguous = _result(
        json.dumps(
            [
                {
                    "message": {
                        "content": [{"type": "text", "text": "I reviewed the work and it seems fine."}]
                    }
                }
            ]
        )
    )

    assert extract_review_verdict(approved) == "APPROVED"
    assert is_review_approved(approved) is True
    assert extract_review_verdict(ambiguous) is None
    assert is_review_approved(ambiguous) is False


def test_extract_review_feedback_flags_missing_verdict_on_success() -> None:
    result = _result(
        json.dumps(
            [
                {
                    "message": {
                        "content": [{"type": "text", "text": "I'll help you implement WP04."}]
                    }
                }
            ]
        )
    )

    feedback = extract_review_feedback(result)

    assert feedback is not None
    assert "required explicit verdict line" in feedback
