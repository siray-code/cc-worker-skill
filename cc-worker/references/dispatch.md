# Dispatch and artifact reference

Run commands using `scripts/run-cc.sh` from this installed skill, not a guessed
global path. Runtime requirements: Bash, Python 3.9+, Git, and Claude Code in PATH.
`doctor` checks local capabilities, lists credential-related variable names found in
the environment or user-level Claude settings, and probes the model API host (DNS
plus TCP, no model call) without printing values.

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
"$RUNNER" submit --task /tmp/task.json --workdir /path/to/repo --isolated
"$RUNNER" submit --resume --run <run-id> --prompt-file /tmp/correction.txt
"$RUNNER" submit --apply --run <run-id>
"$RUNNER" submit --cleanup --run <run-id>
"$RUNNER" status --request <request-id>
"$RUNNER" await --request <request-id>
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
available after an attempt, inspect the result and take over; a run that never
started a worker (for example, an API preflight failure) should be dispatched again
instead. Supply the exact failed-check log and a focused correction; avoid
rediscovering the task or pasting full logs into Codex.

## Sandbox and network

The worker must reach the model API. Before launching Claude Code, `run` and `resume`
probe the configured endpoint (`ANTHROPIC_BASE_URL`, then user-level Claude
settings, then `https://api.anthropic.com`) with DNS and a TCP connection. A failure returns
`worker_failed` immediately with an actionable `error`, an `api_check` object, no
attempt directory, no consumed attempt, and no source changes. Fix network access and
dispatch a new run; there is no session to resume after a preflight failure.

Codex's `workspace-write` sandbox has `network_access: false` by default. Task files
can be written inside the sandbox, but `run`/`resume` must execute with
`sandbox_permissions: "require_escalated"` and a short justification, or after the
user enables network access for Codex
(`[sandbox_workspace_write] network_access = true`). In a managed profile that setting
may not apply to the running session; check `doctor`, do not assume it. `doctor`
reports `api.status` as `ok`, `unreachable`, `invalid`, or `skipped`. `skipped` is not
success: it means `CC_WORKER_SKIP_API_CHECK` is set, or a proxy variable or cloud
provider flag (`CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX`,
`CLAUDE_CODE_USE_FOUNDRY`) is configured, so the direct probe was bypassed.

The sandbox may also expose the repository's `.git` as read-only. Snapshots then use
a private object directory and run in place; `--isolated` falls back with a
`fallback_reason`. If `prepare` fails with `Git command failed`, inspect the error's
Git stderr and check `.git`/`.git/objects` write access. Git LFS and custom
clean/smudge filters can still write under `.git` (for example `.git/lfs`) or need
network; object redirection does not isolate filter drivers, so such repositories
need the daemon or a normal writable checkout.

## Daemon (out-of-sandbox execution)

`doctor` reports `daemon.running`, `daemon.installed`, and a `dispatch` object. When
`dispatch.mode` is `daemon`, submit tasks instead of running them from inside the
sandbox:

```bash
"$RUNNER" submit --task /tmp/task.json --workdir /path/to/repo --isolated
"$RUNNER" status --request <request-id>
"$RUNNER" await --request <request-id>
```

`submit` validates the task, checks the daemon heartbeat/API/Claude binary, and writes
the task content into `queue/requests/<id>.json` atomically. It returns
`{"status":"ok","request_status":"queued","request_id":...,"state_dir":...}`.
`status` returns `{"status":"ok","request":{...}}`, including `progress` while running
and `result` when finished. `await` polls until completion and returns the final
`public_result`. Use `status` every 30-60 seconds for long tasks; do not block `await`
for an hour.

Request types are `run`, `resume`, `apply`, and `cleanup`:

- `submit --resume --run <id> --prompt-file <file>` resumes the exact session.
- `submit --apply --run <id>` applies a reviewed isolated patch outside the sandbox.
- `submit --cleanup --run <id>` removes the run-owned worktree.
- `batch` in daemon mode is multiple independent `submit` requests; the daemon's
  `--jobs` and the source lock serialize the same repository.

Install the daemon once in a normal terminal (not inside Codex):

```bash
"$HOME/.agents/skills/cc-worker/scripts/install-daemon.sh"
# or, when launchd cannot read the project (macOS TCC):
"$HOME/.agents/skills/cc-worker/scripts/install-daemon.sh" --terminal
```

The installer writes a marked `CC_WORKER_STATE_DIR` block into `~/.cc-worker.env`,
loads a launchd agent (`com.siray.cc-worker`), and prints a self-test. The state/queue
directory must be writable by both Codex and the daemon; the default is the per-user
temp directory. If the daemon cannot read the project, grant Full Disk Access to the
daemon's Python or use `--terminal`, which inherits the terminal's TCC/PATH. Rerun
`install-daemon.sh` after changing `PATH` (Go/Node toolchains) or the state directory.
Use `install.sh --update` to refresh an existing directory copy of the skill and
`install.sh --daemon` to install/update and start the daemon.

For non-Anthropic endpoints the runner sets `ENABLE_TOOL_SEARCH=false` and
`CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1` in the worker only. Set
`CC_WORKER_PROVIDER_COMPAT=off` only when the endpoint is known to support Claude
Code's tool-search payload. `doctor.provider` reports the detected host and compat
keys; `result.compat_env` lists the keys actually injected.

The daemon is a deliberate sandbox bypass: the worker and its declared checks run
with the user's normal privileges, and the shared queue is writable by same-user
processes inside the Codex sandbox. Treat the state directory as trusted user data;
do not put it on a shared multi-user path.

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
`attempts`, `summary`, `changed_files`, `out_of_scope`, `checks`, `api_check`,
`provider`, `compat_env`, `permission_denials`, `progress`, `private_objects`,
`git_metadata_writable`, `artifacts`, `workdir`, `result_file`, and `patch` when
available. CLI-provided usage/cost metrics are recorded when present; they do not
measure Codex savings or guarantee proxy billing. Full state and logs live in
`result_file` and `attempt-N/`; `progress.json` is updated live, and
`attempt-N/events.jsonl` exists when the CLI supports stream-json.
Large change lists in stdout are limited to 50 entries with a corresponding
`changed_files_count` or `out_of_scope_count`; the result file retains the full lists.

Statuses: `preparation_failed`, `prepared`, `running`, `worker_failed`, `timed_out`,
`cancelled`, `validation_failed`, `scope_violation`, `ready_for_review`, `applied`.
Queue requests use `queued`, `running`, `done`, `failed`, `interrupted`, `rejected`.
`worker_failed` also covers a model API preflight failure; in that case `api_check`
explains why, `attempts` is unchanged, and no worker ran. Scope violations block
application even if the worker or checks otherwise succeeded.
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
`CC_WORKER_SKIP_API_CHECK=1` disables the API reachability probe when a proxy or
custom transport makes the direct check misleading; use it deliberately, since it
also disables the sandbox/no-network fast failure.
