---
name: cc-worker
description: Delegate substantial code implementation, focused refactors, bug fixes, and tests to the local Claude Code CLI while Codex owns investigation, task design, and diff review. Use for implementation work that benefits from a separate worker context, or when the user asks to have cc or Claude Code implement a task. Supports isolated parallel workers and reviewed patch integration.
---

# CC Worker

Reduce Codex context consumption by delegating substantial implementation and tests
to the configured local Claude Code CLI. Codex remains responsible for understanding
the repository, choosing the design, defining independent tasks, and accepting the
actual changes. Claude Code can use Anthropic, DeepSeek, or another compatible provider.

## Choose the right work

- Delegate substantial implementation by default once this skill applies. Handle
  trivial edits and small finishing corrections directly. Take over only when the
  worker actually ran and could not complete the task (repeated failure, missing
  session, or a design correction only Codex can make), never because the worker
  could not start or could not reach the model API.
- Codex performs source discovery and reads the complete behavior-critical code
  and immediate contracts. Do not offload discovery or prescribe a design based
  only on a worker summary. Keep task context focused on the relevant source.
- Split along coherent behavior and dependency boundaries, without a fixed file
  count. Give the worker enough scope to complete a meaningful change in one run.
- Batch only independent tasks with disjoint write paths and no cross-task
  write/read dependencies. Sequence shared types, interfaces, lockfiles, integration
  points, and tasks sharing mutable test resources. Do not parallelize by default
  when independence is uncertain.
- Delegation preserves the user's scope and existing permissions. Planning or
  read-only requests do not authorize worker edits; provide a plan instead.

## Dispatch

Resolve `scripts/run-cc.sh` relative to this loaded skill. Read
[dispatch.md](references/dispatch.md) before the first dispatch for the task schema,
commands, statuses and snapshot limitations. Run `doctor` before the first dispatch
in a session; it checks capabilities and API reachability without calling a model.

The worker process needs outbound network access to the model API. Codex's
`workspace-write` sandbox disables network access by default, and dispatch fails
with a connection error when started under it. If `doctor` reports `api.status` as
`unreachable`, prepare task files normally and run `run`/`resume` with
`sandbox_permissions: "require_escalated"` and a short justification, or ask the
user to enable network access for Codex (`[sandbox_workspace_write]
network_access = true`). `skipped` only means a proxy, a cloud provider flag, or
`CC_WORKER_SKIP_API_CHECK` bypassed the direct probe. A connectivity failure is
not a reason for Codex to take over implementation: fix the environment and
dispatch a new run.

Codex writes a task JSON with an exact file allowlist, relevant references, concrete
behavior and acceptance conditions, and the narrowest meaningful check commands.
Carry forward applicable repository instructions and the user's authorization limits.
Use [prompt-templates.md](references/prompt-templates.md) only when a dispatch example
would help. Do not prewrite the entire implementation in the prompt: specify real
interfaces and important decisions, leaving routine coding to the worker.

Prefer an isolated run or an independent batch. Put task files and runtime artifacts
outside the target repository. Capture the returned run IDs and artifact paths.
Poll the existing execution handle when a command is still running; do not dispatch
the same task again. Share brief progress updates without loading verbose logs into
Codex. Dependencies are not copied into worktrees: arrange the project's authorized
setup there when needed, or use an in-place serial run when that is more appropriate.

## Review, repair and integrate

1. Inspect the recorded status and runner check results, then review the actual
   patch and relevant source. Check scope, correctness, error handling and public
   contracts; a worker summary is not evidence of successful implementation.
2. For a focused repair, send a concise correction referencing the exact check log
   and use `resume --run ID`. This resumes that task's session, never the most
   recent global conversation. The runner permits at most two worker attempts total.
   Missing sessions, repeated failure or a mistaken design call for Codex takeover.
3. Apply successful isolated results with `apply --run ID` only after review.
   In-place results already modify the source directory and need direct review.
   Preserve user edits and staging. Do not reset, stash, or blindly restore a file
   to discard worker changes; inspect and correct only the affected delta.
4. When a patch conflicts or source inputs changed, inspect the retained worktree
   and reconcile deliberately. Do not force application or silently redispatch.
   Review successful parallel siblings independently; one failure need not discard them.
5. Run proportionate combined checks after integration. Report actual outcomes and
   any remaining blockers. Clean isolated resources only after their work has been
   integrated or explicitly discarded; patches and logs remain for review.
