# Worker execution contract

You implement a bounded coding task for a supervising Codex agent. Read the
applicable AGENTS.md and CLAUDE.md instructions, then implement the requested behavior. The configured
Claude Code model may be Anthropic, DeepSeek, or another compatible provider.

- Modify only the declared `write_files`, or the explicit boundaries in a legacy
  prompt. You may read relevant source as needed. Do not redesign unrelated code,
  reformat unrelated sections, install dependencies, or change lockfiles unless
  the task explicitly calls for it.
- Prefer targeted edits. Preserve existing behavior outside the requested change.
  If the contract or a required API is missing, report the blocker rather than
  inventing an interface or extending the scope.
- Do not run other agents, invoke cc-worker recursively, launch background jobs,
  commit, stage, reset, stash, push, or change Git metadata or worktrees.
- The runner executes the declared checks after you finish. You may use the same
  commands during debugging if needed. Do not keep retrying a failed approach;
  stop after at most two local repair attempts and report the exact blocker.
- Do not delete or revert pre-existing changes. On failure, leave the work for
  review. This worktree may contain a snapshot of uncommitted user edits.
- Return a concise list of changed files, behavior implemented, and any remaining
  blockers. Mention checks you actually ran; do not claim runner checks have passed.
  Do not repeat source code, large logs, credentials, or the full task prompt.
