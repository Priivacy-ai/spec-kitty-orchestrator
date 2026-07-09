"""Tests for committing lane work and gating empty WPs (Architecture A, O2/O3).

The orchestrator must commit the implementer's edits onto the lane branch after a
successful run, and must NOT advance a WP that produced no committable output
(the "done without a commit" bug). It must also refuse to commit to a worktree
that is not on the expected lane branch (a stray detached worktree).
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from spec_kitty_orchestrator import loop as loop_mod
from spec_kitty_orchestrator.config import load_config
from spec_kitty_orchestrator.executor import CommitError, commit_lane_work
from spec_kitty_orchestrator.loop import execute_and_advance
from spec_kitty_orchestrator.policy import PolicyMetadata
from spec_kitty_orchestrator.scheduler import ConcurrencyManager
from spec_kitty_orchestrator.state import new_run_state


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


def _count(repo: Path, rng: str) -> int:
    out = subprocess.run(
        ["git", "-C", str(repo), "rev-list", "--count", rng],
        capture_output=True, text=True,
    ).stdout.strip()
    return int(out)


def _init_lane_repo(tmp_path: Path) -> Path:
    """A repo with a base branch `mission-base` and a checked-out lane `lane-a`."""
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "base.txt").write_text("base\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    _git(tmp_path, "branch", "-M", "mission-base")
    _git(tmp_path, "checkout", "-q", "-b", "lane-a")
    return tmp_path


# -- commit_lane_work ---------------------------------------------------------


def test_commits_changes_and_reports_output(tmp_path: Path) -> None:
    repo = _init_lane_repo(tmp_path)
    (repo / "feature.ts").write_text("export const x = 1;\n", encoding="utf-8")
    out = commit_lane_work(repo, "feat(WP01): x", lane_branch="lane-a", output_base="mission-base")
    assert out is True
    assert _count(repo, "mission-base..HEAD") == 1


def test_empty_wp_reports_no_output(tmp_path: Path) -> None:
    repo = _init_lane_repo(tmp_path)
    # Agent changed nothing; no commits beyond base.
    out = commit_lane_work(repo, "feat(WP01): x", lane_branch="lane-a", output_base="mission-base")
    assert out is False
    assert _count(repo, "mission-base..HEAD") == 0


def test_refuses_wrong_branch(tmp_path: Path) -> None:
    repo = _init_lane_repo(tmp_path)  # on lane-a
    with pytest.raises(CommitError):
        commit_lane_work(repo, "m", lane_branch="lane-b", output_base="mission-base")


def test_refuses_detached_head(tmp_path: Path) -> None:
    repo = _init_lane_repo(tmp_path)
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    _git(repo, "checkout", "-q", "--detach", sha)
    with pytest.raises(CommitError):
        commit_lane_work(repo, "m", lane_branch="lane-a", output_base="mission-base")


def test_no_lane_branch_skips_branch_check(tmp_path: Path) -> None:
    repo = _init_lane_repo(tmp_path)
    (repo / "f.ts").write_text("x\n", encoding="utf-8")
    # lane_branch=None -> legacy/non-lane: no branch guard; still commits.
    assert commit_lane_work(repo, "m", lane_branch=None, output_base=None) is True


def test_dependent_lane_empty_wp_not_counted_as_output(tmp_path: Path) -> None:
    """Regression: on a dependent lane, commits from a merged dependency must NOT
    be counted as this WP's output. An empty WP returns False even though commits
    exist beyond the mission base — measuring output against the pre-implementation
    lane tip is what makes this correct (the bug counted them as output and let an
    empty WP reach 'done')."""
    repo = _init_lane_repo(tmp_path)  # on lane-a, tip == mission-base
    # Simulate the dependency lane's code merged into this lane at allocation.
    (repo / "from_dependency.ts").write_text("dep code\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "Merge dependency lane lane-x into lane-a")
    pre_impl = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()

    # This WP produces nothing of its own.
    out = commit_lane_work(repo, "feat(WP02): x", lane_branch="lane-a", output_base=pre_impl)
    assert out is False, "merged dependency commits must not count as this WP's output"
    # The pre-fix gate measured against the mission base, which DOES see the merge.
    assert _count(repo, "mission-base..HEAD") == 1


# -- execute_and_advance: empty WP must not advance ---------------------------


def _cfg(tmp_path: Path):
    (tmp_path / ".kittify").mkdir()
    return load_config(tmp_path, "spec-kitty-orchestrator")


def _policy() -> PolicyMetadata:
    return PolicyMetadata(
        orchestrator_id="spec-kitty-orchestrator", orchestrator_version="0.1.2",
        agent_family="claude", approval_mode="full_auto", sandbox_mode="workspace_write",
        network_mode="none", dangerous_flags=[], tool_restrictions=None,
    )


def test_empty_implementation_is_not_advanced_to_review(tmp_path: Path, monkeypatch) -> None:
    """Agent exits 0 but commits nothing -> WP is marked failed, NOT transitioned
    to for_review. This is the core 'no done without a commit' guarantee."""
    prompt = tmp_path / "WP01.md"
    prompt.write_text("# WP01\n", encoding="utf-8")
    cfg = _cfg(tmp_path)
    run_state = new_run_state("m", _policy())

    async def fake_execute_agent(*a, **k):
        return SimpleNamespace(exit_code=0, errors=[])

    transitions: list[str] = []
    monkeypatch.setattr(loop_mod, "execute_agent", fake_execute_agent)
    monkeypatch.setattr(loop_mod, "is_success", lambda r: True)
    # Agent "succeeded" but produced no committable output.
    monkeypatch.setattr(loop_mod, "commit_lane_work", lambda *a, **k: False)
    monkeypatch.setattr(loop_mod, "_transition_for_review", lambda *a, **k: transitions.append("for_review"))

    host = SimpleNamespace(
        repo_root=tmp_path,
        append_history=lambda *a, **k: None,
    )

    asyncio.run(execute_and_advance(
        "WP01", "m", tmp_path, prompt, "claude-code",
        host, run_state, cfg.agent_selection, cfg, ConcurrencyManager(1),
        lane_branch="lane-a",
    ))

    assert transitions == [], "an empty WP must never reach for_review"
    assert run_state.wp_executions["WP01"].last_error is not None


def test_commit_fires_when_implementation_succeeds_on_retry(tmp_path: Path, monkeypatch) -> None:
    """First attempt fails (the `--print` glitch), retry succeeds — the lane work
    MUST still be committed. Regression for commit-on-success being skipped on the
    retry path."""
    prompt = tmp_path / "WP02.md"
    prompt.write_text("# WP02\n", encoding="utf-8")
    cfg = _cfg(tmp_path)
    run_state = new_run_state("m", _policy())

    # First impl attempt fails, retry succeeds. (Review phase is short-circuited
    # by making the for_review transition raise, so we only exercise impl+retry.)
    results = iter([
        SimpleNamespace(exit_code=1, errors=["Input must be provided ... --print"]),
        SimpleNamespace(exit_code=0, errors=[]),
    ])

    async def fake_execute_agent(*a, **k):
        return next(results)

    commit_calls: list[str] = []

    def fake_commit(workspace_path, message, *, lane_branch, output_base):
        commit_calls.append(message)
        return True  # real output committed

    from spec_kitty_orchestrator.host.client import TransitionRejectedError

    monkeypatch.setattr(loop_mod, "execute_agent", fake_execute_agent)
    monkeypatch.setattr(loop_mod, "is_success", lambda r: r.exit_code == 0)
    monkeypatch.setattr(loop_mod, "should_retry", lambda *a, **k: True)
    monkeypatch.setattr(loop_mod, "classify_failure", lambda *a, **k: SimpleNamespace(value="transient"))
    monkeypatch.setattr(loop_mod, "commit_lane_work", fake_commit)
    # Stop after impl so we don't drive the whole review loop.
    def _stop(*a, **k):
        raise TransitionRejectedError("TRANSITION_REJECTED", "stop after impl")
    monkeypatch.setattr(loop_mod, "_transition_for_review", _stop)

    async def _no_sleep(*_a, **_k):
        return None
    monkeypatch.setattr(loop_mod.asyncio, "sleep", _no_sleep)  # skip retry backoff

    host = SimpleNamespace(repo_root=tmp_path, append_history=lambda *a, **k: None)

    asyncio.run(execute_and_advance(
        "WP02", "m", tmp_path, prompt, "claude-code",
        host, run_state, cfg.agent_selection, cfg, ConcurrencyManager(1),
        lane_branch="lane-b",
    ))

    assert commit_calls, "retry-success must still commit the lane work"
    assert "WP02" in commit_calls[0]


def test_reimplementation_noop_is_not_counted_as_output(
    tmp_path: Path, monkeypatch
) -> None:
    """A rejected WP must prove each reimplementation produced new output.

    Regression: the rework gate reused the pre-implementation base, so the first
    implementation commit made a later no-op reimplementation look non-empty.
    """
    prompt = tmp_path / "WP03.md"
    prompt.write_text("# WP03\n", encoding="utf-8")
    cfg = _cfg(tmp_path)
    run_state = new_run_state("m", _policy())

    results = iter([
        SimpleNamespace(exit_code=0, errors=[]),  # initial implementation
        SimpleNamespace(exit_code=1, errors=["needs changes"]),  # review rejects
        SimpleNamespace(exit_code=0, errors=[]),  # no-op reimplementation
    ])

    async def fake_execute_agent(*a, **k):
        return next(results)

    heads = iter(["base0", "head1"])

    def fake_head(_workspace_path):
        return next(heads, "head1")

    commit_bases: list[str | None] = []

    def fake_commit(workspace_path, message, *, lane_branch, output_base):
        commit_bases.append(output_base)
        if len(commit_bases) == 1:
            return True
        # No-op rework: old code passed base0 and falsely counted the initial
        # implementation commit as rework output; fixed code passes head1.
        return output_base == "base0"

    transitions: list[str] = []

    def fake_transition(mission, wp, to, **kwargs):
        transitions.append(to)
        return SimpleNamespace(to_lane=to)

    host = SimpleNamespace(
        repo_root=tmp_path,
        append_history=lambda *a, **k: None,
        transition=fake_transition,
        start_review=lambda *a, **k: SimpleNamespace(to_lane="in_review"),
    )

    monkeypatch.setattr(loop_mod, "execute_agent", fake_execute_agent)
    monkeypatch.setattr(loop_mod, "is_success", lambda r: r.exit_code == 0)
    monkeypatch.setattr(loop_mod, "extract_review_feedback", lambda r: "change requested")
    monkeypatch.setattr(loop_mod, "current_lane_head", fake_head)
    monkeypatch.setattr(loop_mod, "commit_lane_work", fake_commit)

    asyncio.run(execute_and_advance(
        "WP03", "m", tmp_path, prompt, "claude-code",
        host, run_state, cfg.agent_selection, cfg, ConcurrencyManager(1),
        lane_branch="lane-c",
    ))

    assert commit_bases == ["base0", "head1"]
    assert transitions == ["for_review", "in_progress", "blocked"]
