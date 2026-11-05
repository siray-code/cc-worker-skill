# CC-Worker

[English](README.md) | [简体中文](README_zh.md)

A Codex skill for delegating substantial implementation and tests to your local
Claude Code CLI. Codex investigates the source, designs tasks, reviews actual
diffs, and integrates accepted changes. Workers handle the implementation and
verbose execution context using your configured model, including DeepSeek.

## What it provides

- Coherent task boundaries and exact output-file declarations, without a fixed file-count limit.
- Independent workers in separate Git worktrees, with configurable concurrency.
- Snapshots of current tracked content, including staged and unstaged user changes.
- Recorded session IDs for focused repairs without accidentally continuing another task.
- Worker/check timeouts, actual check exit codes, compact JSON results, and retained logs.
- Reviewed patch application that preserves the original index and branch history.

This reduces the implementation logs and repeated file reads entering Codex's
context. Codex still reads critical source and reviews changes. No fixed token or
cost reduction is promised; provider-reported metrics do not measure Codex savings.

## Requirements and installation

Use macOS, Linux, or WSL with Bash, Python 3.9+, Git 2.25+, Codex, and a configured
[Claude Code CLI](https://code.claude.com/docs/en/overview) available as `claude`.
No Python packages or Agent SDK installation are required at runtime.

### Quick install with Skills CLI

Install directly from GitHub with [Skills CLI](https://github.com/vercel-labs/skills),
without manually cloning this repository. The `npx` command requires Node.js 22.20+:

```bash
npx --yes skills add siray-code/cc-worker-skill --skill cc-worker --agent codex --global --yes
```

Equivalent commands using pnpm or Bun:

```bash
pnpm dlx skills add siray-code/cc-worker-skill --skill cc-worker --agent codex --global --yes
bunx skills add siray-code/cc-worker-skill --skill cc-worker --agent codex --global --yes
```

Choose one command. `--global` installs for your user across projects; omit it and
run from your project root for project-local installation. `--agent codex` selects
Codex, and `--yes` skips Skills CLI confirmation prompts. Node.js/package runners
are only needed for this installation method, not when running the skill.

### Install from a checkout

Clone the repository and run the included installer:

```bash
git clone https://github.com/siray-code/cc-worker-skill.git
cd cc-worker-skill
./install.sh
```

The installer links `cc-worker/` into `~/.agents/skills/cc-worker`, the current
[Codex user skill directory](https://learn.chatgpt.com/docs/build-skills).
It is idempotent and refuses to overwrite another directory or symlink. Keep the
checkout available because the installation is a symlink.

For project-local installation, or a legacy directory explicitly used by your setup:

```bash
./install.sh --target /path/to/project/.agents/skills
# Alternatively: ./install.sh --target "$HOME/.codex/skills"
```

Install in one discovery location to avoid duplicate entries. Copying the complete
`cc-worker/` directory there is also supported. Use `/skills` or `$cc-worker` in Codex;
restart Codex if the skill does not appear.

## Use from Codex

```text
$cc-worker Implement CSV export in the user service and add regression tests.
Investigate the existing API first, delegate implementation, and review the patch.
```

```text
Use cc-worker for two independent fixes. Run them in isolated worktrees,
review each result, then integrate them and run the combined checks.
```

Codex keeps source investigation and architectural decisions. Substantial coding
defaults to delegation; trivial edits and small corrections can be completed
directly. Independent tasks can run concurrently. Shared interfaces, lockfiles,
dependent changes, and tests using shared mutable services must run in sequence.

## Configure a provider

An already configured Claude Code installation works without additional setup.
The runner inherits its environment and configuration; it does not force DeepSeek
or change global Claude settings.

For a dedicated DeepSeek configuration, create `~/.cc-worker.env`:

```bash
export ANTHROPIC_BASE_URL="https://api.deepseek.com/anthropic"
export ANTHROPIC_AUTH_TOKEN="replace-with-your-deepseek-api-key"
export ANTHROPIC_MODEL="deepseek-flash[1m]"
```

Use the model and optional alias settings in the current
[DeepSeek Claude Code integration guide](https://api-docs.deepseek.com/quick_start/agent_integrations/claude_code/).
Protect the file with `chmod 600 ~/.cc-worker.env`. It is sourced as trusted shell
code in the runner subprocess; its assignments override inherited values.
`CC_WORKER_ENV_FILE=/path/to/file` selects a different file, and
`CC_WORKER_ENV_FILE=''` disables loading it. `--model` overrides model configuration.

Check local compatibility without calling a model or displaying credential values:

```bash
RUNNER="$HOME/.agents/skills/cc-worker/scripts/run-cc.sh"
"$RUNNER" doctor
```

The CLI must support JSON output, appended system instructions, `acceptEdits`, and
tool selection. Missing required capabilities fail explicitly. The runner never
falls back to `--dangerously-skip-permissions`. See the
[Claude Code automation documentation](https://code.claude.com/docs/en/headless).

## Direct execution

Normally Codex writes task files for you. To invoke the runner directly, write a
task JSON outside the target repository. Replace these example paths and commands
with ones from your project:

```json
{
  "id": "csv-export",
  "prompt": "Implement CSV export with standard escaping and an empty-result header. Preserve existing public APIs and cover these behaviors in the existing tests.",
  "read_files": ["src/types/user.ts"],
  "write_files": ["src/services/user.ts", "tests/user.test.ts"],
  "checks": [["npm", "run", "typecheck"], ["npm", "test", "--", "user.test.ts"]]
}
```

Paths are individual files relative to the repository root. Checks are argv arrays,
executed directly without a shell. The runner executes them after the worker exits;
an empty `checks` list means no runner validation was performed.

```bash
"$RUNNER" run --task /tmp/task.json --workdir /path/to/project --isolated
```

For a parallel batch, use `{"tasks": [...]}` as the manifest. Cross-task write/write
and write/read overlaps are rejected before dispatch. Hidden semantic dependencies
remain the supervisor's responsibility.

```bash
"$RUNNER" batch --manifest /tmp/tasks.json --workdir /path/to/project --jobs 2
```

Capture each returned `run_id`. Review the patch and checks, then apply accepted results:

```bash
"$RUNNER" inspect --run <run-id>
"$RUNNER" inspect --run <run-id> --patch
# Review changes and evidence before applying.
"$RUNNER" apply --run <run-id>
# After integration or deliberate discard:
"$RUNNER" cleanup --run <run-id>
```

For one focused repair, put the correction in a text file and resume the exact session:

```bash
"$RUNNER" resume --run <run-id> --prompt-file /tmp/correction.txt
```

There are at most two worker attempts per run. No retries happen automatically.
Successful parallel siblings remain reviewable even when another task fails. Codex
takes over when a session cannot resume or the second attempt fails. After applying
multiple results, run the necessary combined project checks.

`run` without `--isolated` executes in place. The legacy
`run-cc.sh "<task prompt>" [workdir]` invocation remains available with text output;
it has no structured file allowlist or runner checks. In-place changes need review
but must not be applied again.

## Defaults, isolation and recovery

| Setting | Default |
| --- | --- |
| Parallel workers | 2, configurable with `--jobs` |
| Worker timeout | 900 seconds per attempt, `--timeout` |
| Check timeout | 300 seconds per command, `--check-timeout` |
| Turn limit | 30 with `--max-turns`, when the CLI advertises support |
| Model | Inherit Claude configuration, override with `--model` |
| State directory | Platform temporary directory, `cc-worker-<uid>/`, mode 700 |

`CC_WORKER_STATE_DIR` selects a persistent private directory outside the target
repository. Keep the same setting for inspection, resume, application, and cleanup.
Default temporary artifacts may be removed by OS cleanup. Full results are stored
in `result.json`; each attempt retains the prompt, CLI output, stderr, and check logs.
Logs can contain project source and provider error details; keep them out of Git.

Snapshots use a temporary index and private Git commit objects, leaving the source
HEAD, staging and branch history unchanged. All current tracked content is included;
untracked files are included only when explicitly declared. Ignored files such as
credentials and dependency directories are not copied. Submodules require a separate
workflow. The runner never installs dependencies or shares writable dependency trees.
Prepare worktree dependencies through the project's authorized process when needed.

Worktrees separate files; they share Git metadata and do not sandbox shell commands,
external services or ignored files. Scope checks cover tracked and non-ignored files.
Only coding tools are exposed, with Bash allow rules for declared checks; existing
Claude settings and hooks still apply. Check commands run under the caller's existing
permissions. Use only trusted repositories and authorized checks.

No Git commit, or no Git repository: isolation falls back to sequential in-place
execution with an explicit reason. The runner does not create an initial commit.

Application checks for changed source targets/references, stale worker results,
scope violations and modified patches, then preflights the whole patch. Conflicts
stop application without staging, resetting or overwriting user changes. Failed runs
retain their worktrees; explicit `cleanup` removes only the run-owned worktree while
retaining logs and patches. Stop editing task targets during snapshot/application;
workspace locks coordinate runner commands, not unrelated editors.

Exit codes: `0` for successful results, `1` for task/check failure, `2` for invalid
input or rejected operations, `130` for interruption. Full contracts and statuses:
[dispatch reference](cc-worker/references/dispatch.md).

## Development

```bash
bash -n install.sh cc-worker/scripts/run-cc.sh
python3 -m unittest discover -s tests -v
```

Tests use temporary Git repositories and a fake `claude` executable, with no network
or model usage. CI runs the same suite on macOS and Linux with Python 3.9 and 3.13.
The deployable skill is the self-contained `cc-worker/` directory; all its content
is in English. Tests and repository documentation are outside the installed package.

[MIT License](LICENSE)
