"""Host interface: typed models and subprocess client for spec-kitty orchestrator-api."""

from .client import HostClient, HostError, ContractMismatchError
from .models import (
    HostResponse,
    ContractVersionData,
    FeatureStateData,
    ListReadyData,
    StartImplData,
    StartReviewData,
    TransitionData,
    AppendHistoryData,
    AcceptFeatureData,
    MergeData,
    WorkPackageInfo,
    ReadyWorkPackage,
)

__all__ = [
    "HostClient",
    "HostError",
    "ContractMismatchError",
    "HostResponse",
    "ContractVersionData",
    "FeatureStateData",
    "ListReadyData",
    "StartImplData",
    "StartReviewData",
    "TransitionData",
    "AppendHistoryData",
    "AcceptFeatureData",
    "MergeData",
    "WorkPackageInfo",
    "ReadyWorkPackage",
]
