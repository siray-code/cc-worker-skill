---
name: cc-worker
description: Delegate substantial code implementation, focused refactors, bug fixes, and tests to the local Claude Code CLI while Codex owns investigation, task design, and diff review. Use for implementation work that benefits from a separate worker context, or when the user asks to have cc or Claude Code implement a task. Supports isolated parallel workers, an out-of-sandbox daemon queue for Codex sandboxes, and reviewed patch integration.
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
  session, or a design correction only Codex can make), or when the user explicitly
  chooses takeover after dispatch is blocked. Do not silently take over merely
  because the worker could not start or could not reach the model API.
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

`doctor` returns a `dispatch` object that decides the path:

- `dispatch.mode == "daemon"`: submit the task; the launchd daemon runs the worker
  outside Codex's sandbox and checks. This is the preferred mode.
- `dispatch.mode == "direct"`: this process can reach the model API; use `run`/`resume`.
- `dispatch.status == "blocked"`: ask the user **once** to install/start the daemon
  with the exact `remedy` command, then rerun `doctor`. If the user declines, ask
  whether to run the prepared command manually or let Codex implement directly.
  Never repeat "please run manually" without giving the user a choice.

Daemon commands:

```bash
"$RUNNER" submit --task /tmp/task.json --workdir /path/to/repo --isolated
"$RUNNER" status --request <id>
"$RUNNER" await --request <id>
```

Use `status` every 30-60 seconds and report its `progress`; do not block `await` for
a long task. Review steps use `submit --resume --run <id> --prompt-file <file>`,
`submit --apply --run <id>` and `submit --cleanup --run <id>`. In daemon mode,
`batch` means multiple independent `submit` requests.

The worker needs outbound network access. Codex's `workspace-write` sandbox disables
it by default, and `network_access = true` may not apply to a managed profile.
`skipped` only means a proxy, a cloud provider flag, or `CC_WORKER_SKIP_API_CHECK`
bypassed the direct probe. A connectivity failure alone is not a reason to silently
take over; let the user choose the fallback. For non-Anthropic endpoints the runner
sets `ENABLE_TOOL_SEARCH=false` and `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1` in the
worker; do not disable `CC_WORKER_PROVIDER_COMPAT` unless the endpoint is known to
support tool search.

Codex writes a task JSON with an exact file allowlist, relevant references, concrete
behavior and acceptance conditions, and the narrowest meaningful check commands.
Carry forward applicable repository instructions and the user's authorization limits.
Use [prompt-templates.md](references/prompt-templates.md) only when a dispatch example
would help. Do not prewrite the entire implementation in the prompt: specify real
interfaces and important decisions, leaving routine coding to the worker.

Prefer an isolated run or an independent batch. Put task files and runtime artifacts
outside the target repository. Capture the returned run IDs and artifact paths.
Poll `inspect`/`status` for progress; do not dispatch the same task again. Share brief
progress updates without loading verbose logs into Codex. Dependencies are not copied
into worktrees: arrange the project's authorized setup there when needed, or use an
in-place serial run when that is more appropriate.

## Review, repair and integrate

1. Inspect the recorded status, `progress` and runner check results, then review the
   actual patch and relevant source. Check scope, correctness, error handling and
   public contracts; a worker summary is not evidence of successful implementation.
   `permission_denials` means the worker tried commands outside `checks`; surface it
   and decide whether the task's checks need to authorize them.
2. For a focused repair, send a concise correction referencing the exact check log
   and use `resume --run ID` (direct mode) or
   `submit --resume --run ID --prompt-file FILE` (daemon mode). This resumes that
   task's session, never the most recent global conversation. The runner permits at
   most two worker attempts total. Missing sessions, repeated failure or a mistaken
   design call allows Codex takeover.
3. Apply successful isolated results with `apply --run ID` (direct) or
   `submit --apply --run ID` (daemon) only after review. In-place results already
   modify the source directory and need direct review. Preserve user edits and
   staging. Do not reset, stash, or blindly restore a file to discard worker changes;
   inspect and correct only the affected delta.
4. When a patch conflicts or source inputs changed, inspect the retained worktree
   and reconcile deliberately. Do not force application or silently redispatch.
   Review successful parallel siblings independently; one failure need not discard them.
5. Run proportionate combined checks after integration. Report actual outcomes and
   any remaining blockers. Clean isolated resources with `cleanup --run ID` (direct)
   or `submit --cleanup --run ID` (daemon) only after their work has been integrated
   or explicitly discarded; patches and logs remain for review.
