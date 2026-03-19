from __future__ import annotations

from spec_kitty_orchestrator.config import AgentSelectionConfig


def test_gemini_family_expands_to_model_fallbacks_for_review() -> None:
    cfg = AgentSelectionConfig(review_agents=["gemini"])

    assert cfg.select_reviewer(tried=[]) == "gemini"
    assert cfg.select_reviewer(tried=["gemini"]) == "gemini-2.5-flash-lite"
    assert cfg.select_reviewer(tried=["gemini", "gemini-2.5-flash-lite"]) == "gemini-2.5-flash"


def test_gemini_family_expands_to_model_fallbacks_for_implementation() -> None:
    cfg = AgentSelectionConfig(implementation_agents=["gemini"])

    assert cfg.select_implementer(tried=[]) == "gemini"
    assert cfg.select_implementer(tried=["gemini"]) == "gemini-2.5-flash-lite"


def test_review_candidates_expand_family_aliases() -> None:
    cfg = AgentSelectionConfig(review_agents=["gemini"])

    assert cfg.review_candidates() == [
        "gemini",
        "gemini-2.5-flash-lite",
        "gemini-2.5-flash",
        "gemini-3-flash-preview",
    ]
