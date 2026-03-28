# spec-kitty-orchestrator

External orchestrator for the [spec-kitty](https://github.com/spec-kitty/spec-kitty) workflow system.

Coordinates multiple AI agents to autonomously implement and review work packages (WPs) in parallel. Integrates with spec-kitty via the versioned `orchestrator-api` CLI contract for workflow state and `agent tasks mark-status` for authoritative subtask checkbox reconciliation.

---

## How it works

```
spec-kitty-orchestrator
        │
        │  spec-kitty orchestrator-api <cmd> [--json]
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
spec-kitty orchestrator-api contract-version

# Dry-run to validate configuration
spec-kitty-orchestrator orchestrate --feature 034-my-feature --dry-run

# Run the orchestration loop
spec-kitty-orchestrator orchestrate --feature 034-my-feature
```

The orchestrator will:
1. List all WPs with satisfied dependencies
2. Claim each ready WP via the host API
3. Spawn the implementation agent in the WP's worktree
4. Submit to review when implementation completes
5. Transition to `done` only after an explicit review approval verdict, or re-implement with feedback on rejection
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

Resumes an interrupted run from saved state. The loop re-polls ready WPs and also recovers tracked WPs stranded in `in_progress` or `for_review`.

### `abort`

Records the run as aborted. Use `--cleanup-worktrees` to delete the provider state file.

---

## Configuration

Optional config at `.kittify/orchestrator.yaml` (preferred) or `.kittify/orchestrator.toml` (legacy):

```yaml
max_concurrent_wps: 4
max_retries: 2
timeout_seconds: 3600
single_agent_mode: false

agents:
  implementation:
    - claude-code
    - gemini
  review:
    - claude-code
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
    agent_family="codex",
    approval_mode="full_auto",   # full_auto | interactive | supervised
    sandbox_mode="workspace_write",  # workspace_write | read_only | none
    network_mode="open",         # allowlist | none | open
    dangerous_flags=[],
)
```

Policy fields are validated on both sides: the provider rejects secret-like values before sending; the host rejects missing or malformed policy on run-affecting commands.

---

## Host boundary

The orchestrator has **no direct access** to spec-kitty internals:

- No imports from `specify_cli` or `spec_kitty_events`
- No direct writes to `kitty-specs/`
- Worktree creation is delegated to the host via `start-implementation`
- Workflow lane transitions use `spec-kitty orchestrator-api ...`
- Subtask checkbox reconciliation uses `spec-kitty agent tasks mark-status ...`
- All workflow mutations go through `HostClient` subprocess calls

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
- Review feedback from rejected cycles

Lane/status fields are never stored locally — those are always read from the host.

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
