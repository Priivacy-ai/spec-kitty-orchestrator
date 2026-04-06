"""Host interface: typed models and subprocess client for spec-kitty orchestrator-api."""

from .client import HostClient, HostError, ContractMismatchError
from .models import (
    HostResponse,
    ContractVersionData,
    MissionStateData,
    ListReadyData,
    StartImplData,
    StartReviewData,
    TransitionData,
    AppendHistoryData,
    AcceptMissionData,
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
    "MissionStateData",
    "ListReadyData",
    "StartImplData",
    "StartReviewData",
    "TransitionData",
    "AppendHistoryData",
    "AcceptMissionData",
    "MergeData",
    "WorkPackageInfo",
    "ReadyWorkPackage",
]
