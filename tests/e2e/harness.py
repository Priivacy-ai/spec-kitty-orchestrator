from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = REPO_ROOT.parent
DEFAULT_SPEC_KITTY_REPO = WORKSPACE_ROOT / "spec-kitty"


@dataclass(frozen=True)
class CommandResult:
    args: list[str]
    cwd: Path
    returncode: int
    stdout: str
    stderr: str

    @property
    def combined(self) -> str:
        return f"{self.stdout}\n{self.stderr}"


@dataclass(frozen=True)
class ProjectFixture:
    root: Path
    mission_slug: str
    mission_dir: Path
    wp_path: Path
    bin_dir: Path
    env: dict[str, str]


def run_command(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: int = 120,
) -> CommandResult:
    result = subprocess.run(
        args,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    return CommandResult(
        args=args,
        cwd=cwd,
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )


def require_success(result: CommandResult) -> None:
    assert result.returncode == 0, (
        f"command failed ({result.returncode}): {' '.join(result.args)}\n"
        f"cwd={result.cwd}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def write_executable(path: Path, content: str) -> None:
    path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")
    path.chmod(0o755)


def resolve_spec_kitty_binary(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """Return a `spec-kitty` executable that can run from arbitrary cwd."""
    env: dict[str, str] = {}
    override = os.environ.get("SK_ORCH_E2E_SPEC_KITTY_BIN")
    if override:
        binary = Path(override)
        if not binary.exists():
            pytest.skip(f"SK_ORCH_E2E_SPEC_KITTY_BIN does not exist: {binary}")
        return binary, env

    repo_override = os.environ.get("SK_ORCH_E2E_SPEC_KITTY_REPO")
    spec_kitty_repo = Path(repo_override) if repo_override else DEFAULT_SPEC_KITTY_REPO
    candidate = spec_kitty_repo / ".venv" / "bin" / "spec-kitty"
    if candidate.exists():
        return candidate, env

    found = shutil.which("spec-kitty")
    if found:
        return Path(found), env

    pytest.skip(
        "spec-kitty binary unavailable. Set SK_ORCH_E2E_SPEC_KITTY_BIN or "
        "SK_ORCH_E2E_SPEC_KITTY_REPO, or run `uv run spec-kitty ...` once in "
        f"{spec_kitty_repo} to materialize .venv/bin/spec-kitty."
    )


def create_spec_kitty_wrapper(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    binary, env = resolve_spec_kitty_binary(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    wrapper = bin_dir / "spec-kitty"
    write_executable(
        wrapper,
        f"""\
        #!/usr/bin/env bash
        exec {str(binary)!r} "$@"
        """,
    )
    return bin_dir, env


def make_base_env(bin_dir: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["PYTHONPATH"] = f"{REPO_ROOT / 'src'}{os.pathsep}{env.get('PYTHONPATH', '')}"
    env.setdefault("SPEC_KITTY_HOME", str(bin_dir.parent / ".spec-kitty-home"))
    env.setdefault("SPEC_KITTY_ENABLE_SAAS_SYNC", "0")
    if extra:
        env.update(extra)
    return env


def init_git_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    require_success(run_command(["git", "init", "-b", "main"], cwd=root))
    require_success(run_command(["git", "config", "user.email", "e2e@example.test"], cwd=root))
    require_success(run_command(["git", "config", "user.name", "Spec Kitty Orchestrator E2E"], cwd=root))


def seed_minimal_spec_kitty_project(tmp_path: Path, *, bin_dir: Path, env: dict[str, str]) -> ProjectFixture:
    root = tmp_path / "project"
    init_git_repo(root)
    require_success(run_command(["git", "checkout", "-b", "e2e/orchestrator"], cwd=root))
    (root / ".kittify").mkdir(parents=True, exist_ok=True)
    (root / ".gitignore").write_text(".worktrees/\n.kittify/logs/\n.kittify/orchestrator-run-state.json\n", encoding="utf-8")

    mission_slug = "099-orchestrator-e2e"
    mission_dir = root / "kitty-specs" / mission_slug
    tasks_dir = mission_dir / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    (mission_dir / "meta.json").write_text(
        json.dumps(
            {
                "mission_slug": mission_slug,
                "mission_number": 99,
                "mission_type": "software-dev",
                "title": "Orchestrator E2E",
                "target_branch": "e2e/orchestrator",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (mission_dir / "spec.md").write_text(
        "# Orchestrator E2E\n\n"
        "## Functional Requirements\n\n"
        "| ID | Title | Description | Status |\n"
        "| --- | --- | --- | --- |\n"
        "| FR-001 | Deterministic implementation | Covered by WP01. | proposed |\n",
        encoding="utf-8",
    )
    (mission_dir / "tasks.md").write_text(
        "## WP01\n\n"
        "**Requirement Refs**: FR-001\n\n"
        "- [ ] Deterministic implementation\n",
        encoding="utf-8",
    )
    wp_path = tasks_dir / "WP01-deterministic-implementation.md"
    wp_path.write_text(
        textwrap.dedent(
            """\
            ---
            work_package_id: "WP01"
            title: "Deterministic implementation"
            lane: "planned"
            dependencies: []
            requirement_refs: ["FR-001"]
            owned_files:
              - "src/wp01_impl.py"
            create_intent:
              - "src/wp01_impl.py"
            authoritative_surface: "src/"
            agent: null
            ---

            # WP01

            Create `src/wp01_impl.py` with a `status() -> str` function.
            """
        ),
        encoding="utf-8",
    )
    require_success(run_command(["git", "add", "-A"], cwd=root))
    require_success(run_command(["git", "commit", "-m", "chore: seed orchestrator e2e project"], cwd=root))
    require_success(run_command(
        ["spec-kitty", "agent", "mission", "finalize-tasks", "--mission", mission_slug, "--json"],
        cwd=root,
        env=env,
    ))
    return ProjectFixture(root=root, mission_slug=mission_slug, mission_dir=mission_dir, wp_path=wp_path, bin_dir=bin_dir, env=env)


def create_fake_agent_bin(bin_dir: Path, command_name: str) -> None:
    write_executable(
        bin_dir / command_name,
        f"""\
        #!/usr/bin/env python3
        import json
        import os
        import subprocess
        import sys
        from pathlib import Path

        AGENT = {command_name!r}
        cwd = Path.cwd()
        prompt = sys.stdin.read()
        impl = cwd / "src" / "wp01_impl.py"
        state = cwd / ".fake-review-count"

        def emit(payload):
            if AGENT == "opencode":
                print(json.dumps(payload))
            else:
                print(json.dumps(payload))

        def git(*args):
            subprocess.run(["git", *args], cwd=cwd, check=True, text=True, capture_output=True)

        if "Review Feedback" in prompt or not impl.exists():
            impl.parent.mkdir(parents=True, exist_ok=True)
            impl.write_text(
                "def status() -> str:\\n"
                f"    return 'implemented-by-{{AGENT}}'\\n",
                encoding="utf-8",
            )
            git("add", "src/wp01_impl.py")
            try:
                git("commit", "-m", f"feat(WP01): {{AGENT}} deterministic implementation")
            except subprocess.CalledProcessError:
                pass
            emit({{"result": "implemented", "files_modified": ["src/wp01_impl.py"], "commits": ["fake-commit"]}})
            raise SystemExit(0)

        if os.environ.get("SK_ORCH_FAKE_REVIEW_FAIL_ONCE") == "1" and not state.exists():
            state.write_text("1", encoding="utf-8")
            emit({{"error": "review rejected: add deterministic rework evidence"}})
            raise SystemExit(1)

        if "def status() -> str:" not in impl.read_text(encoding="utf-8"):
            emit({{"error": "review rejected: status() missing"}})
            raise SystemExit(1)

        emit({{"result": "approved", "files_modified": []}})
        raise SystemExit(0)
        """,
    )


def install_fake_agents(bin_dir: Path, names: tuple[str, ...] = ("claude", "codex", "opencode")) -> None:
    for name in names:
        create_fake_agent_bin(bin_dir, name)


def run_orchestrator(
    project: ProjectFixture,
    *,
    impl_agent: str,
    review_agent: str,
    dry_run: bool = False,
    extra_env: dict[str, str] | None = None,
    timeout: int = 180,
) -> CommandResult:
    env = project.env.copy()
    if extra_env:
        env.update(extra_env)
    args = [
        sys.executable,
        "-m",
        "spec_kitty_orchestrator.cli.main",
        "orchestrate",
        "--mission",
        project.mission_slug,
        "--impl-agent",
        impl_agent,
        "--review-agent",
        review_agent,
        "--max-concurrent",
        "1",
        "--repo-root",
        str(project.root),
    ]
    if dry_run:
        args.append("--dry-run")
    return run_command(args, cwd=project.root, env=env, timeout=timeout)


def read_status_events(mission_dir: Path) -> list[dict[str, object]]:
    path = mission_dir / "status.events.jsonl"
    if not path.exists():
        return []
    events: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))
    return events


def final_wp_lane(mission_dir: Path, wp_id: str = "WP01") -> str | None:
    lane = "planned"
    for event in read_status_events(mission_dir):
        if event.get("wp_id") == wp_id and isinstance(event.get("to_lane"), str):
            lane = str(event["to_lane"])
    return lane


def has_real_agent(agent_id: str) -> bool:
    binary = {"claude-code": "claude", "codex": "codex", "opencode": "opencode"}[agent_id]
    return shutil.which(binary) is not None


def truthy(value: str | None) -> bool:
    return value is not None and value.strip().lower() in {"1", "true", "yes", "on", "y"}
