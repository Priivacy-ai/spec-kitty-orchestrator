"""Pydantic response models for the spec-kitty orchestrator-api contract.

These models validate the canonical JSON envelope emitted by every
`spec-kitty orchestrator-api <cmd> --json` invocation.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class HostResponse(BaseModel):
    """Canonical orchestrator-api response envelope."""

    contract_version: str
    command: str
    timestamp: str
    correlation_id: str
    success: bool
    error_code: str | None
    data: dict[str, Any]


class WorkPackageInfo(BaseModel):
    """WP info item within feature-state data."""

    wp_id: str
    lane: str | None
    dependencies: list[str]
    last_actor: str | None


class ContractVersionData(BaseModel):
    """Data returned by contract-version command."""

    api_version: str
    min_supported_provider_version: str


class FeatureStateData(BaseModel):
    """Data returned by feature-state command."""

    feature_slug: str
    summary: dict[str, Any]
    work_packages: list[WorkPackageInfo]


class ReadyWorkPackage(BaseModel):
    """Single ready WP entry in list-ready response."""

    wp_id: str
    lane: str
    dependencies_satisfied: bool
    recommended_base: str | None


class ListReadyData(BaseModel):
    """Data returned by list-ready command."""

    feature_slug: str
    ready_work_packages: list[ReadyWorkPackage]


class StartImplData(BaseModel):
    """Data returned by start-implementation command."""

    feature_slug: str
    wp_id: str
    from_lane: str
    to_lane: str
    workspace_path: str
    prompt_path: str
    policy_metadata_recorded: bool
    no_op: bool


class StartReviewData(BaseModel):
    """Data returned by start-review command."""

    feature_slug: str
    wp_id: str
    from_lane: str
    to_lane: str
    prompt_path: str
    policy_metadata_recorded: bool


class TransitionData(BaseModel):
    """Data returned by transition command."""

    feature_slug: str
    wp_id: str
    from_lane: str
    to_lane: str
    policy_metadata_recorded: bool


class AppendHistoryData(BaseModel):
    """Data returned by append-history command."""

    feature_slug: str
    wp_id: str
    history_entry_id: str


class AcceptFeatureData(BaseModel):
    """Data returned by accept-feature command."""

    feature_slug: str
    accepted: bool
    mode: str
    accepted_at: str


class MergeData(BaseModel):
    """Data returned by merge-feature command."""

    feature_slug: str
    merged: bool
    target_branch: str
    strategy: str
    merged_wps: list[str]
    worktree_removed: bool


__all__ = [
    "HostResponse",
    "WorkPackageInfo",
    "ContractVersionData",
    "FeatureStateData",
    "ReadyWorkPackage",
    "ListReadyData",
    "StartImplData",
    "StartReviewData",
    "TransitionData",
    "AppendHistoryData",
    "AcceptFeatureData",
    "MergeData",
]
