# Orchestration Reliability Fixes

This change closes the gap that made multi-task work packages look stuck even when an agent had already produced valid code.

## Problem

The previous implementation had two separate failures that compounded:

1. Successful implementation did not reconcile prompt-declared subtasks into `tasks.md`.
   `spec-kitty` correctly rejected `move-task --to for_review` when required subtasks were still unchecked, so a WP could finish code generation and still fail the workflow handoff.
2. Long-running agent executions did not emit incremental provider-local evidence.
   The executor only wrote logs after process exit, and run state was not updated while a process was alive, so a healthy Gemini run looked indistinguishable from a wedged one.

## Fixes

### 1. Completion handshake before review handoff

The orchestrator now:

- parses `subtasks:` from the WP prompt frontmatter
- marks those task IDs `done` through `spec-kitty agent tasks mark-status`
- promotes the WP to `for_review` through `spec-kitty agent tasks move-task`

This keeps the orchestrator aligned with the same task workflow validation that a human operator would trigger from the CLI.

### 2. Live execution visibility

The executor now:

- creates the log file immediately after spawn
- writes `command`, `pid`, and `status: running` before the process exits
- streams stdout and stderr into the log as data arrives
- writes the final exit code when the process completes

The orchestration loop now updates per-role heartbeat timestamps during execution so the provider state file reflects active progress.

## Why the task workflow CLI is used for handoff

Raw lane transitions are not sufficient for the implementation-to-review boundary because review readiness depends on more than lane state. The task workflow CLI already enforces checklist readiness and other workflow invariants. Reusing that path prevents the orchestrator from bypassing the same validation a human agent must satisfy.

## Expected outcome

For features like `004` that use a WP as a bundle of several subtasks:

- the implementation agent can complete the WP without manual checklist repair
- the handoff to `for_review` succeeds when the prompt-declared subtasks are complete
- operators can distinguish `alive and working` from `alive but stalled`
