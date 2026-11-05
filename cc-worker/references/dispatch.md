# Dispatch and artifact reference

Run commands using `scripts/run-cc.sh` from this installed skill, not a guessed
global path. Runtime requirements: Bash, Python 3.9+, Git, and Claude Code in PATH.
`doctor` checks local capabilities without calling a model or printing credentials.

## Task contract

```json
{
  "id": "csv-export",
  "prompt": "Implement CSV export using the existing repository API. Include an empty-result header and standard CSV escaping. Keep existing public methods unchanged.",
  "read_files": ["src/types/user.ts"],
  "write_files": ["src/services/user.ts", "tests/user.test.ts"],
  "checks": [["npm", "run", "typecheck"], ["npm", "test", "--", "user.test.ts"]]
}
```

- `id` and `prompt` are required. IDs use 1-64 letters, digits, underscores or hyphens.
- `write_files` is a required nonempty list of exact file paths; new files are allowed.
- `read_files` defaults to `[]`; listed references must exist. This is a context
  and dependency declaration, not a prohibition against reading other source.
- `checks` defaults to `[]`. Each entry is an argv array executed directly in the
  repository root, with no implicit shell. Use existing project commands. An empty
  list means no runner checks were performed; review must identify any remaining validation.
- Paths are relative to the repository root, or the supplied directory outside Git.
  Do not use globs, directories, symlinks, `.git`, parent traversal or ignored files.
  Existing tracked files remain eligible even if ignore rules match them.
- A manifest is `{"tasks": [<task>, <task>]}`. IDs must be unique. Cross-task
  write/write and write/read overlaps are rejected, including ancestor paths.
  Only batch tasks whose changes, tests, external resources and interfaces are independent.

## Commands

```bash
"$RUNNER" doctor
"$RUNNER" run --task /tmp/task.json --workdir /path/to/repo --isolated
"$RUNNER" batch --manifest /tmp/tasks.json --workdir /path/to/repo --jobs 2
"$RUNNER" inspect --run <run-id>
"$RUNNER" inspect --run <run-id> --patch
"$RUNNER" resume --run <run-id> --prompt-file /tmp/correction.txt
"$RUNNER" apply --run <run-id>
"$RUNNER" cleanup --run <run-id>
```

`RUNNER` denotes the resolved script in this skill. `run` is in place unless
`--isolated` is supplied. `batch` requests isolation. Without a Git commit, or
outside Git, isolation falls back to sequential in-place execution and reports why.
No initial commit is created. In-place results are already in the source directory;
review them directly rather than calling `apply`.

Run/batch options: `--timeout` (900 seconds per worker attempt), `--check-timeout`
(300 seconds per check), `--max-turns` (30 when supported by the CLI), `--model`
(otherwise inherit Claude configuration). Jobs default to 2. Resume retains these
options and permits at most two worker attempts total. There are no automatic retries.

Use the run ID to resume exactly its session. Never use `claude --continue`: parallel
sessions can make "most recent" point to another task. If no usable session is
available, inspect the result and take over. Supply the exact failed-check log and
a focused correction; avoid rediscovering the task or pasting full logs into Codex.

## Snapshot and review behavior

Git snapshots use an alternate index and private commit objects, without moving
the source HEAD, creating user branch commits, or updating the source index.
They include current tracked content (including staged additions and unstaged edits)
and only explicitly listed untracked inputs/outputs. Other untracked and ignored
files, such as dependency directories and local credentials, are not copied.
Submodules need a separate project-specific workflow; automatic snapshots reject them.

Worktrees share Git metadata and are not security sandboxes. Scope checking detects
changes to tracked and non-ignored files after execution. It does not enforce OS
confinement or monitor arbitrary ignored files or external services. Worker prompts
prohibit Git metadata changes, nested workers and unrelated operations. The CLI
exposes only the coding tools; declared check commands receive Bash allow rules.
User/project Claude settings and hooks still apply. A missing CLI capability never
enables permission bypass. The runner executes checks directly under the caller's
existing permissions; declare only commands authorized by the task.

Checks run in the worker directory. Prepare dependencies there using the project's
authorized setup process if needed; the runner neither installs packages nor shares
writable dependency directories. Network access and test databases are not isolated
by worktrees, so serialize tasks that would contend for them.

Inspect `changes.patch`, source implementations and `result.json` before applying.
`apply` accepts only successful, retained isolated runs. It checks source target
and declared reference fingerprints, detects post-validation worker edits and patch
tampering, and preflights the entire patch. It does not stage, commit, auto-merge or
partially apply rejected patches. Conflicts leave source files unchanged. It is
idempotent once applied. Run the necessary combined checks after integration.

Failed runs retain their worktree and logs. `cleanup` explicitly removes only a
run-owned worktree, including failed edits, but retains the patch, summaries and logs.
Do not clean a run until its artifacts are reviewed or the work is no longer needed.

## Results and state

Structured commands emit one compact JSON result. `inspect --patch` emits the diff.
Legacy `run-cc.sh "<prompt>" [workdir]` emits text and a run ID; it is in place,
has no file allowlist or runner checks, and is intended only for compatibility.

Result fields include `run_id`, `task_id`, `status`, `isolated`, `session_id`,
`attempts`, `summary`, `changed_files`, `out_of_scope`, `checks`, `artifacts`,
`workdir`, `result_file`, and `patch` when available. CLI-provided usage/cost metrics
are recorded when present; they do not measure Codex savings or guarantee proxy billing.
Full state and logs live in `result_file` and `attempt-N/`.
Large change lists in stdout are limited to 50 entries with a corresponding
`changed_files_count` or `out_of_scope_count`; the result file retains the full lists.

Statuses: `preparation_failed`, `prepared`, `running`, `worker_failed`, `timed_out`,
`cancelled`, `validation_failed`, `scope_violation`, `ready_for_review`, `applied`.
Scope violations block application even if the worker or checks otherwise succeeded.
A batch reports every task separately and returns `batch_failed` if any task fails.
Successful siblings remain available for independent review and application.

Exit codes: 0 for successful runs/application/diagnostics or completed cleanup;
1 for unsuccessful task results; 2 for invalid inputs or rejected operations;
130 when interrupted. `inspect` preserves the recorded task's success/failure code.

Artifacts default to `${TMPDIR:-/tmp}/cc-worker-<uid>/` (Python's platform temporary
directory), outside the target repository. The directory is private, mode 700.
Set `CC_WORKER_STATE_DIR` to an outside-repository directory for longer retention;
use the same setting for subsequent commands. OS temporary-file cleanup can remove
default artifacts. Logs may contain source and provider error details; do not commit
them. No environment variable values are printed by `doctor`.

The Bash entrypoint loads `~/.cc-worker.env` as trusted shell code in its subprocess.
Its assignments override inherited values. `CC_WORKER_ENV_FILE` selects another file;
an empty value disables loading. `--model` overrides the resulting model configuration.
