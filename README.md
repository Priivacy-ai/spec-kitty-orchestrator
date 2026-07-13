# spec-kitty-orchestrator

External orchestrator for the [spec-kitty](https://github.com/Priivacy-ai/spec-kitty) workflow system.

Coordinates multiple AI agents to autonomously implement and review work packages (WPs) in parallel. Integrates with spec-kitty **exclusively** via the versioned `orchestrator-api` CLI contract — no direct file access, no internal imports.

---

## How it works

```
spec-kitty-orchestrator
        │
        │  spec-kitty orchestrator-api <cmd>
        ▼
   spec-kitty (host)
        │
        └── kitty-specs/<mission>/tasks/WP01..WPn.md
```

The orchestrator polls the host for ready work packages, spawns AI agents in worktrees, and transitions each WP through `planned → claimed → in_progress → for_review → done` by calling the host API at each step. All workflow state lives in spec-kitty; the orchestrator only tracks provider-local data (retry counts, log paths, agent choices).

---

## Requirements

- Python 3.10+
- [spec-kitty](https://github.com/Priivacy-ai/spec-kitty) installed and on PATH with `orchestrator-api` contract ≥ 1.3.0
- At least one supported AI agent CLI installed (see [Supported agents](#supported-agents))

---

## Installation

Use `pipx` for an isolated command-line install:

```bash
pipx install spec-kitty-orchestrator
```

If you prefer `uv` tool management:

```bash
uv tool install spec-kitty-orchestrator
```

Source lives at [`Priivacy-ai/spec-kitty-orchestrator`](https://github.com/Priivacy-ai/spec-kitty-orchestrator).
Install from GitHub only when intentionally testing unreleased provider changes.

---

## Quick start

```bash
# Verify contract compatibility with the installed spec-kitty
spec-kitty orchestrator-api contract-version

# Dry-run to validate configuration
spec-kitty-orchestrator orchestrate --mission 034-my-feature --dry-run

# Run the orchestration loop
spec-kitty-orchestrator orchestrate --mission 034-my-feature
```

The orchestrator will:
1. List all WPs with satisfied dependencies
2. Claim each ready WP via the host API
3. Spawn the implementation agent in the WP's worktree
4. Submit to review when implementation completes
5. Transition to `done` on review approval, or re-implement with feedback on rejection
6. Accept the mission when all WPs are done

---

## CLI reference

```
spec-kitty-orchestrator orchestrate  --mission <slug>
                                     [--impl-agent <id>]
                                     [--review-agent <id>]
                                     [--max-concurrent <n>]
                                     [--actor <identity>]
                                     [--repo-root <path>]
                                     [--dry-run]
                                     [--no-caffeinate]

spec-kitty-orchestrator status       [--repo-root <path>]

spec-kitty-orchestrator resume       [--actor <identity>]
                                     [--repo-root <path>]
                                     [--no-caffeinate]

spec-kitty-orchestrator abort        [--cleanup-worktrees]
                                     [--repo-root <path>]
```

### `orchestrate`

Starts a new orchestration run for the named mission. Runs until all WPs reach a terminal lane (`done`, `canceled`, or `blocked`) or a dependency deadlock is detected.

| Flag | Default | Description |
|------|---------|-------------|
| `--mission` | required | Mission slug (e.g. `034-auth-system`) |
| `--impl-agent` | `claude-code` | Override implementation agent |
| `--review-agent` | `claude-code` | Override review agent |
| `--max-concurrent` | `4` | Max WPs in flight simultaneously |
| `--actor` | `spec-kitty-orchestrator` | Actor identity recorded in events |
| `--dry-run` | off | Validate config only, don't execute |
| `--no-caffeinate` | off | Allow macOS idle sleep during the run |

On macOS, `orchestrate` holds an idle-sleep assertion for the loop's lifetime by
default. This does not prevent lid-close sleep. Use `--no-caffeinate` to opt out.

### `status`

Shows the provider-local run state (retry counts, agent choices, errors) from the most recent run.

### `resume`

Resumes an interrupted run from saved state. The host already tracks lane state,
so the loop simply re-polls for ready WPs. On macOS, resumed runs also hold the
idle-sleep assertion by default; use `--no-caffeinate` to opt out.

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
| `pi` | `pi` | yes | Headless print mode with JSON events |
| `letta` | `letta` | yes | Headless mode with JSON output |

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
    orchestrator_version="0.1.3",
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
- No imports from spec-kitty internals and no direct mission-state edits
- Git operations are limited to provider-owned workspace preparation when the host returns a worktree path that does not yet exist
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
- Review feedback from rejected cycles

Lane/status fields are never stored locally — those are always read from the host.

---

## Conformance tests

The `tests/conformance/fixtures/` directory contains 13 canonical JSON fixtures that define the exact shape of every host API response. Both the host and provider test suites use these as source of truth.

```bash
python3.11 -m pytest tests/conformance/ -v
```

## End-to-end tests

The e2e suite lives in `tests/e2e/` and launches real subprocesses for both
`spec-kitty-orchestrator` and `spec-kitty`. It has two modes:

```bash
# Deterministic local suite: fake claude/codex/opencode binaries, real spec-kitty host.
pytest tests/e2e -m "e2e and not real_agents" -q

# Real-agent trusted-runner suite: requires authenticated claude/codex/opencode CLIs.
SK_ORCH_E2E_REAL_AGENTS=1 pytest tests/e2e -m "real_agents" -q
```

By default the harness looks for a sibling `../spec-kitty/.venv/bin/spec-kitty`
checkout. Override with:

```bash
SK_ORCH_E2E_SPEC_KITTY_BIN=/absolute/path/to/spec-kitty pytest tests/e2e -q
SK_ORCH_E2E_SPEC_KITTY_REPO=/absolute/path/to/spec-kitty pytest tests/e2e -q
```

Real-agent matrix defaults to:

- `claude-code -> codex`
- `claude-code -> opencode`
- `codex -> claude-code`
- `opencode -> claude-code`

Override with:

```bash
SK_ORCH_E2E_AGENT_MATRIX=claude-code:codex,codex:claude-code \
SK_ORCH_E2E_REAL_AGENTS=1 \
pytest tests/e2e -m real_agents -q
```

The deterministic suite is intended for CI. Real-agent tests are deselected by
default and should run only on trusted machines with local agent credentials.

---

## Development

```bash
pip install -e ".[dev]"
python3.11 -m pytest tests/
```
