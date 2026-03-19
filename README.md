# spec-kitty-orchestrator

External orchestrator for the [spec-kitty](https://github.com/spec-kitty/spec-kitty) workflow system.

Coordinates multiple AI agents to autonomously implement and review work packages (WPs) in parallel. Integrates with spec-kitty **exclusively** via the versioned `orchestrator-api` CLI contract — no direct file access, no internal imports.

---

## How it works

```
spec-kitty-orchestrator
        │
        │  spec-kitty orchestrator-api <cmd> --json
        ▼
   spec-kitty (host)
        │
        └── kitty-specs/<feature>/tasks/WP01..WPn.md
```

The orchestrator polls the host for ready work packages, spawns AI agents in worktrees, and transitions each WP through `planned → claimed → in_progress → for_review → done` by calling the host API at each step. All workflow state lives in spec-kitty; the orchestrator only tracks provider-local data (retry counts, log paths, agent choices).

---

## Requirements

- Python 3.10+
- [spec-kitty](https://github.com/spec-kitty/spec-kitty) ≥ 2.x installed and on PATH (provides the `orchestrator-api` contract)
- At least one supported AI agent CLI installed (see [Supported agents](#supported-agents))

---

## Installation

```bash
pip install spec-kitty-orchestrator
```

Or from source:

```bash
git clone https://github.com/spec-kitty/spec-kitty-orchestrator
cd spec-kitty-orchestrator
pip install -e ".[dev]"
```

---

## Quick start

```bash
# Verify contract compatibility with the installed spec-kitty
spec-kitty orchestrator-api contract-version --json

# Dry-run to validate configuration
spec-kitty-orchestrator orchestrate --feature 034-my-feature --dry-run

# Run the orchestration loop
spec-kitty-orchestrator orchestrate --feature 034-my-feature
```

The orchestrator will:
1. List all WPs with satisfied dependencies
2. Claim each ready WP via the host API
3. Spawn the implementation agent in the WP's worktree
4. Reconcile prompt-declared subtasks, then submit to review through the task workflow CLI
5. Transition to `done` on review approval, or re-implement with feedback on rejection
6. Accept the feature when all WPs are done

---

## CLI reference

```
spec-kitty-orchestrator orchestrate  --feature <slug>
                                     [--impl-agent <id>]
                                     [--review-agent <id>]
                                     [--max-concurrent <n>]
                                     [--actor <identity>]
                                     [--repo-root <path>]
                                     [--dry-run]

spec-kitty-orchestrator status       [--repo-root <path>]

spec-kitty-orchestrator resume       [--actor <identity>]
                                     [--repo-root <path>]

spec-kitty-orchestrator abort        [--cleanup-worktrees]
                                     [--repo-root <path>]
```

### `orchestrate`

Starts a new orchestration run for the named feature. Runs until all WPs reach a terminal lane (`done`, `canceled`, or `blocked`) or a dependency deadlock is detected.

| Flag | Default | Description |
|------|---------|-------------|
| `--feature` | required | Feature slug (e.g. `034-auth-system`) |
| `--impl-agent` | `claude-code` | Override implementation agent |
| `--review-agent` | `claude-code` | Override review agent |
| `--max-concurrent` | `4` | Max WPs in flight simultaneously |
| `--actor` | `spec-kitty-orchestrator` | Actor identity recorded in events |
| `--dry-run` | off | Validate config only, don't execute |

### `status`

Shows the provider-local run state (retry counts, agent choices, errors) from the most recent run.

### `resume`

Resumes an interrupted run from saved state. The host already tracks lane state, and the loop now re-enters orphaned WPs already sitting in `in_progress` or `for_review` instead of only waiting for newly ready `planned` work.

### `abort`

Records the run as aborted. Use `--cleanup-worktrees` to delete the provider state file.

---

## Configuration

Optional YAML config at `.kittify/orchestrator.yaml`:

```yaml
max_concurrent_wps: 4

agents:
  implementation:
    - claude-code
    - gemini
  review:
    - claude-code
  max_retries: 2
  timeout_seconds: 3600
  single_agent_mode: false
```

---

## Supported agents

| Agent ID | CLI binary | stdin? | Notes |
|----------|-----------|--------|-------|
| `claude-code` | `claude` | yes | Default; JSON output via `--output-format json` |
| `codex` | `codex` | yes | `codex exec -` with `--full-auto` |
| `copilot` | `gh` | no | Requires `gh extension install github/gh-copilot` |
| `gemini` | `gemini` | yes | Specific exit codes for auth/rate-limit errors |
| `qwen` | `qwen` | yes | Fork of Gemini CLI |
| `opencode` | `opencode` | yes | Multi-provider; JSONL streaming output |
| `kilocode` | `kilocode` | no | Prompt as positional arg with `-a --yolo -j` |
| `augment` | `auggie` | no | `--acp` mode; no JSON output |
| `cursor` | `cursor` | no | Always wrapped with `timeout` to prevent hangs |

The orchestrator detects installed agents automatically at startup:

```bash
python3 -c "from spec_kitty_orchestrator.agents import detect_installed_agents; print(detect_installed_agents())"
```

---

## Policy metadata

Every host mutation call includes a `PolicyMetadata` block that declares the orchestrator's identity and capability scope. The host validates and records this alongside every WP event, creating a full audit trail.

```python
PolicyMetadata(
    orchestrator_id="spec-kitty-orchestrator",
    orchestrator_version="0.1.0",
    agent_family="claude",
    approval_mode="full_auto",   # full_auto | interactive | supervised
    sandbox_mode="workspace_write",  # workspace_write | read_only | none
    network_mode="none",         # allowlist | none | open
    dangerous_flags=[],
)
```

Policy fields are validated on both sides: the provider rejects secret-like values before sending; the host rejects missing or malformed policy on run-affecting commands.

---

## Security boundary

The orchestrator has **no direct access** to spec-kitty internals:

- No imports from `specify_cli` or `spec_kitty_events`
- No direct reads or writes to `kitty-specs/`
- No git operations — worktree creation is delegated to the host via `start-implementation`
- All state mutations go through `HostClient` subprocess calls

This is enforced at test time:

```bash
# Boundary check (must print OK)
grep -r "specify_cli\|spec_kitty_events" src/spec_kitty_orchestrator/ && echo "FAIL" || echo "OK"

# AST-level import check in conformance suite
python3.11 -m pytest tests/conformance/test_contract.py::TestBoundaryCheck
```

---

## Provider-local state

The orchestrator writes only to `.kittify/orchestrator-run-state.json` (a file it owns). This tracks:

- Retry counts per WP per role
- Which agents were tried (for fallback)
- Log file paths
- Per-role heartbeat timestamps while a process is still running
- Review feedback from rejected cycles

Lane/status fields are never stored locally — those are always read from the host.

---

## Reliability Notes

Implementation completion now uses the task workflow contract, not only raw lane transitions:

- prompt frontmatter `subtasks:` are reconciled through `spec-kitty agent tasks mark-status`
- review handoff uses `spec-kitty agent tasks move-task --to for_review`

This prevents a WP from appearing complete in code while still being blocked by unchecked task checklist state.

Execution logs are also now live-streamed. The provider log file is created as soon as the agent process starts, records the PID and running status, and appends stdout/stderr incrementally so long-running Gemini or Claude runs no longer look silent by default.

---

## Conformance tests

The `tests/conformance/fixtures/` directory contains 13 canonical JSON fixtures that define the exact shape of every host API response. Both the host and provider test suites use these as source of truth.

```bash
python3.11 -m pytest tests/conformance/ -v
```

---

## Development

```bash
pip install -e ".[dev]"
python3.11 -m pytest tests/
```
