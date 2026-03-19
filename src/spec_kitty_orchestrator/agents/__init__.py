"""Agent registry for spec-kitty-orchestrator.

Provides agent discovery and instantiation. All agents implement the AgentInvoker
protocol from base.py.
"""

from __future__ import annotations

import shutil

from .augment import AugmentInvoker
from .base import AgentInvoker, BaseInvoker, InvocationResult
from .claude import ClaudeInvoker
from .codex import CodexInvoker
from .copilot import CopilotInvoker
from .cursor import CursorInvoker
from .gemini import GeminiInvoker
from .kilocode import KilocodeInvoker
from .opencode import OpenCodeInvoker
from .qwen import QwenInvoker

_REGISTRY: dict[str, type[BaseInvoker]] = {
    "claude-code": ClaudeInvoker,
    "codex": CodexInvoker,
    "copilot": CopilotInvoker,
    "gemini": GeminiInvoker,
    "qwen": QwenInvoker,
    "opencode": OpenCodeInvoker,
    "kilocode": KilocodeInvoker,
    "augment": AugmentInvoker,
    "cursor": CursorInvoker,
}

_GEMINI_MODEL_ALIASES: dict[str, str] = {
    "gemini": "auto",
    "gemini-auto": "auto",
    "gemini-2.5-flash-lite": "gemini-2.5-flash-lite",
    "gemini-2.5-flash": "gemini-2.5-flash",
    "gemini-3-flash-preview": "gemini-3-flash-preview",
}


def get_invoker(agent_id: str) -> BaseInvoker:
    """Return an invoker instance for the given agent ID.

    Args:
        agent_id: One of the registered agent IDs.

    Raises:
        KeyError: If agent_id is not in the registry.
    """
    if agent_id in _GEMINI_MODEL_ALIASES:
        return GeminiInvoker(model=_GEMINI_MODEL_ALIASES[agent_id])

    cls = _REGISTRY[agent_id]
    return cls()


def detect_installed_agents() -> list[str]:
    """Return list of agent IDs that are currently installed on the system."""
    installed = []
    for agent_id, cls in _REGISTRY.items():
        try:
            invoker = cls()
            if invoker.is_installed():
                installed.append(agent_id)
        except Exception:
            pass
    return installed


def all_agent_ids() -> list[str]:
    """Return all registered agent IDs."""
    return list(_REGISTRY.keys())


__all__ = [
    "AgentInvoker",
    "BaseInvoker",
    "InvocationResult",
    "get_invoker",
    "detect_installed_agents",
    "all_agent_ids",
    "ClaudeInvoker",
    "CodexInvoker",
    "CopilotInvoker",
    "GeminiInvoker",
    "QwenInvoker",
    "OpenCodeInvoker",
    "KilocodeInvoker",
    "AugmentInvoker",
    "CursorInvoker",
]
