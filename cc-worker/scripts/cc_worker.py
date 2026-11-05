#!/usr/bin/env python3
"""Bounded Claude Code dispatch with isolated workers and reviewable artifacts."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from workspace import (
    WorkerError, apply_patch, changed_paths, create_worktree, export_patch,
    head_exists, normalize_path, remove_worktree, repository, scan, snapshot,
    tracked_paths, validate_paths,
)

STOP = threading.Event()
COMMANDS = {"run", "batch", "resume", "inspect", "apply", "cleanup", "doctor"}
SUCCESS = {"ready_for_review", "applied"}
REQUIRED_FLAGS = ("--append-system-prompt", "--output-format", "--permission-mode", "--allowedTools", "--tools")


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WorkerError("Cannot read JSON file: " + str(path)) from exc


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def state_root():
    default = str(Path(tempfile.gettempdir()) / ("cc-worker-" + str(os.getuid())))
    path = Path(os.environ.get("CC_WORKER_STATE_DIR", default)).expanduser().absolute()
    if path.is_symlink():
        raise WorkerError("The state directory cannot be a symlink.")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise WorkerError("The state directory must be owned by you with mode 700.")
    return path.resolve()


@contextmanager
def locked(path):
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise WorkerError("Another operation owns this run or workspace; retry after it finishes.") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def source_lock(root, source):
    name = hashlib.sha256(os.fsencode(str(source))).hexdigest()
    return locked(root / ("workspace-" + name + ".lock"))


def load_run(root, run_id):
    if not re.fullmatch(r"[a-zA-Z0-9_-]+-[a-f0-9]{12}", run_id):
        raise WorkerError("Invalid run ID.")
    directory = root / run_id
    if directory.is_symlink() or not directory.is_dir():
        raise WorkerError("Run not found in the current state directory.")
    state = read_json(directory / "result.json")
    if state.get("run_id") != run_id:
        raise WorkerError("Run metadata does not match its directory.")
    return directory, state


def save(directory, state):
    write_json(directory / "result.json", state)


def public_result(directory, state):
    keys = ("run_id", "task_id", "status", "isolated", "session_id", "attempts", "summary",
            "changed_files", "out_of_scope", "checks", "error", "fallback_reason", "cleaned", "metrics")
    result = {key: state[key] for key in keys if key in state}
    for key in ("changed_files", "out_of_scope"):
        if len(result.get(key, [])) > 50:
            result[key + "_count"] = len(result[key])
            result[key] = result[key][:50]
    result["artifacts"] = str(directory)
    result["workdir"] = state["workdir"]
    result["result_file"] = str(directory / "result.json")
    if state.get("patch_sha256"):
        result["patch"] = str(directory / "changes.patch")
    return result


def validate_task(value):
    if not isinstance(value, dict):
        raise WorkerError("Each task must be a JSON object.")
    unknown = set(value) - {"id", "prompt", "read_files", "write_files", "checks"}
    if unknown:
        raise WorkerError("Unknown task fields: " + ", ".join(sorted(unknown)))
    task = dict(value)
    if not isinstance(task.get("id"), str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", task["id"]):
        raise WorkerError("Task id must contain 1-64 letters, digits, underscores or hyphens.")
    if not isinstance(task.get("prompt"), str) or not task["prompt"].strip():
        raise WorkerError("Task prompt must be a nonempty string.")
    for key in ("read_files", "write_files"):
        paths = task.setdefault(key, [])
        if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
            raise WorkerError(key + " must be an array of individual file paths.")
        task[key] = list(dict.fromkeys(normalize_path(path) for path in paths))
    if not task["write_files"]:
        raise WorkerError("Declare at least one write_files path for an implementation task.")
    checks = task.setdefault("checks", [])
    if not isinstance(checks, list):
        raise WorkerError("checks must be an array of command argument arrays.")
    for argv in checks:
        if not isinstance(argv, list) or not argv or not all(isinstance(arg, str) and arg and "\0" not in arg for arg in argv):
            raise WorkerError("Each check must be a nonempty array of nonempty command arguments.")
    return task


def overlaps(left, right):
    return left == right or left.startswith(right + "/") or right.startswith(left + "/")


def validate_batch(tasks):
    if not tasks:
        raise WorkerError("A batch must contain at least one task.")
    if len({task["id"] for task in tasks}) != len(tasks):
        raise WorkerError("Task IDs must be unique within a batch.")
    for index, left in enumerate(tasks):
        for right in tasks[index + 1:]:
            pairs = [(left, right), (right, left)]
            for writer, reader in pairs:
                if any(overlaps(a, b) for a in writer["write_files"] for b in reader["read_files"] + reader["write_files"]):
                    raise WorkerError("Dependent or overlapping tasks must run in separate batches: " + left["id"] + ", " + right["id"])


def capabilities():
    executable = shutil.which("claude")
    if not executable:
        raise WorkerError("Claude Code was not found in PATH.")
    try:
        result = subprocess.run([executable, "--help"], capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired as exc:
        raise WorkerError("Claude Code --help timed out.") from exc
    if result.returncode:
        raise WorkerError("Claude Code --help failed.")
    help_text = result.stdout + result.stderr
    missing = [flag for flag in REQUIRED_FLAGS if flag not in help_text]
    if missing:
        raise WorkerError("Upgrade Claude Code; required flags are missing: " + ", ".join(missing))
    return executable, help_text


def signal_group(pid, signum):
    # Darwin may briefly report EPERM while an orphaned group becomes zombies.
    # Do not suppress a persistent permission failure or claim the group stopped.
    deadline = time.monotonic() + 1
    while True:
        try:
            os.killpg(pid, signum)
            return
        except ProcessLookupError:
            return
        except PermissionError:
            if sys.platform != "darwin" or time.monotonic() >= deadline:
                raise
            time.sleep(0.02)


def stop_process(process):
    signal_group(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass
    # Kill descendants even when the group leader already exited on SIGTERM.
    signal_group(process.pid, signal.SIGKILL)
    process.wait()


def process_run(argv, workdir, stdout, stderr, timeout, stdin=None):
    started = time.monotonic()
    if STOP.is_set():
        return {"exit_code": None, "status": "cancelled", "seconds": 0}
    try:
        process = subprocess.Popen(argv, cwd=str(workdir), stdin=stdin or subprocess.DEVNULL,
                                   stdout=stdout, stderr=stderr, start_new_session=True)
    except OSError:
        return {"exit_code": None, "status": "launch_failed", "seconds": 0}
    status = "finished"
    try:
        while process.poll() is None:
            if STOP.is_set() or time.monotonic() - started >= timeout:
                status = "cancelled" if STOP.is_set() else "timed_out"
                stop_process(process)
                break
            time.sleep(0.05)
    finally:
        # A check or tool must not leave same-group background work behind.
        stop_process(process)
    return {"exit_code": process.returncode, "status": status, "seconds": round(time.monotonic() - started, 3)}


def prepare(root, source, task, options, extra_files=None, legacy=False):
    run_id = task["id"] + "-" + uuid.uuid4().hex[:12]
    directory = root / run_id
    directory.mkdir(mode=0o700)
    state = {"run_id": run_id, "task_id": task["id"], "task": task, "source": str(source),
             "workdir": str(source), "isolated": False, "is_git": repository(source) is not None,
             "status": "preparing", "attempts": 0, "session_id": None, "legacy": legacy,
             "options": {"timeout": options.timeout, "check_timeout": options.check_timeout,
                         "max_turns": options.max_turns, "model": options.model},
             "checks": [], "changed_files": []}
    save(directory, state)
    try:
        if root == source or source in root.parents:
            raise WorkerError("CC_WORKER_STATE_DIR must be outside the target workspace.")
        names = task["read_files"] + task["write_files"]
        tracked = tracked_paths(source) if state["is_git"] else set()
        validate_paths(source, names, tracked)
        for name in task["read_files"]:
            if not (source / name).is_file():
                raise WorkerError("Reference file not found: " + name)
        state["source_before"] = scan(source, state["is_git"])
        if state["is_git"]:
            state["baseline"] = snapshot(source, directory, extra_files or names, all_untracked=legacy)
        if options.isolated:
            if not state["is_git"] or not head_exists(source):
                state["fallback_reason"] = "Isolation requires a Git repository with an existing commit; running in place."
            else:
                workdir = directory / "worktree"
                create_worktree(source, workdir, state["baseline"])
                state["isolated"] = True
                state["workdir"] = str(workdir)
        if scan(source, state["is_git"]) != state["source_before"]:
            raise WorkerError("Source changed while its snapshot was being prepared; redispatch after review.")
        state["before"] = scan(Path(state["workdir"]), state["is_git"])
        state["status"] = "prepared"
    except (WorkerError, OSError) as exc:
        state["status"] = "preparation_failed"
        state["error"] = str(exc)
    save(directory, state)
    return directory, state


def redact(text):
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        secret = os.environ.get(key)
        if secret:
            text = text.replace(secret, "[redacted]")
    return text


def execute(directory, state, cli, correction=None):
    if state["status"] == "preparation_failed":
        return public_result(directory, state)
    with locked(directory / "run.lock"):
        state = read_json(directory / "result.json")
        executable, help_text = cli
        if correction is not None:
            if state.get("cleaned") or state["status"] == "applied" or state["attempts"] >= 2:
                raise WorkerError("This run cannot resume: it is applied, cleaned, or has reached two attempts.")
            if not state.get("session_id") or "--resume" not in help_text:
                raise WorkerError("No resumable Claude session is available; Codex must take over.")
            if scan(Path(state["workdir"]), state["is_git"]) != state.get("after"):
                raise WorkerError("Workspace changed after the last attempt; review before redispatching.")
        state["attempts"] += 1
        state["status"] = "running"
        state.pop("error", None)
        state["checks"] = []
        state["summary"] = ""
        save(directory, state)
        try:
            task = state["task"]
            rules = (Path(__file__).resolve().parent.parent / "references" / "worker-rules.md").read_text(encoding="utf-8")
            allowed = ["Read", "Edit", "Write", "Glob", "Grep"]
            allowed += ["Bash(" + shlex.join(command) + ")" for command in task["checks"]]
            argv = [executable, "-p", "--append-system-prompt", rules,
                    "--permission-mode", "acceptEdits", "--allowedTools", *allowed,
                    "--tools", "Read,Edit,Write,Glob,Grep,Bash", "--output-format", "json"]
            for flag in ("--disable-slash-commands",):
                if flag in help_text:
                    argv.append(flag)
            if "--permission-prompts" in help_text:
                argv += ["--permission-prompts", "none"]
            if "--max-turns" in help_text:
                argv += ["--max-turns", str(state["options"]["max_turns"])]
            if state["options"]["model"]:
                if "--model" not in help_text:
                    raise WorkerError("This Claude CLI does not support --model.")
                argv += ["--model", state["options"]["model"]]
            if correction is not None:
                argv += ["--resume", state["session_id"]]
            prompt = {"task_id": task["id"], "instructions": correction if correction is not None else task["prompt"],
                      "read_files": task["read_files"], "write_files": task["write_files"], "checks": task["checks"],
                      "note": "The runner will execute checks after you finish. Use them yourself only when debugging requires it."}
            if state["legacy"]:
                prompt["note"] += " Legacy invocation: follow the file boundaries in the instructions."
            attempt = directory / ("attempt-" + str(state["attempts"]))
            attempt.mkdir()
            write_json(attempt / "prompt.json", prompt)
            with (attempt / "prompt.json").open("rb") as stdin, (attempt / "stdout.json").open("wb") as stdout, (attempt / "stderr.log").open("wb") as stderr:
                outcome = process_run(argv, Path(state["workdir"]), stdout, stderr, state["options"]["timeout"], stdin)
            state["worker"] = outcome
            state["status"] = "worker_failed"
            if outcome["status"] in {"timed_out", "cancelled"}:
                state["status"] = outcome["status"]
            else:
                try:
                    payload = read_json(attempt / "stdout.json")
                    if not isinstance(payload, dict) or payload.get("type") != "result" or not isinstance(payload.get("result", ""), str):
                        raise WorkerError("Claude returned an invalid JSON result envelope.")
                    session = payload.get("session_id")
                    if session:
                        state["session_id"] = str(uuid.UUID(session))
                    if outcome["exit_code"] == 0 and not payload.get("is_error") and payload.get("subtype", "success") == "success":
                        if not session or "result" not in payload:
                            raise WorkerError("Claude result lacks a session ID or final result.")
                        state["status"] = "validating"
                        state["summary"] = redact(payload.get("result", ""))[:1200]
                        state["metrics"] = {key: payload[key] for key in ("num_turns", "duration_ms", "total_cost_usd", "usage") if key in payload}
                except (WorkerError, ValueError, TypeError, AttributeError):
                    state["error"] = "Claude did not return a usable JSON result; inspect the attempt logs."
            if state["status"] == "validating":
                for index, command in enumerate(task["checks"], 1):
                    log = attempt / ("check-" + str(index) + ".log")
                    with log.open("wb") as output:
                        checked = process_run(command, Path(state["workdir"]), output, subprocess.STDOUT, state["options"]["check_timeout"])
                    checked.update({"argv": command, "log": str(log)})
                    state["checks"].append(checked)
                    if checked["exit_code"] != 0 or checked["status"] != "finished":
                        state["status"] = "cancelled" if checked["status"] == "cancelled" else "validation_failed"
                        break
                else:
                    state["status"] = "ready_for_review"
        except (WorkerError, OSError) as exc:
            state["status"] = "worker_failed"
            state["error"] = redact(str(exc))
        finally:
            try:
                workdir = Path(state["workdir"])
                state["after"] = scan(workdir, state["is_git"])
                state["changed_files"] = changed_paths(state["before"], state["after"])
                state["out_of_scope"] = [] if state["legacy"] else sorted(set(state["changed_files"]) - set(state["task"]["write_files"]))
                if state["is_git"]:
                    state["patch_sha256"] = export_patch(workdir, directory, state["baseline"], state["before"], state["task"]["write_files"])
                if state["out_of_scope"]:
                    state["status"] = "scope_violation"
                validate_paths(workdir, state["task"]["write_files"], tracked_paths(workdir) if state["is_git"] else set())
            except (WorkerError, OSError) as exc:
                state["status"] = "scope_violation"
                state["error"] = redact(str(exc))
            save(directory, state)
        return public_result(directory, state)


def positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("Must be a positive finite number.")
    return number


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("Must be a positive integer.")
    return number


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    subs = result.add_subparsers(dest="command", required=True)
    for command in ("run", "batch"):
        sub = subs.add_parser(command)
        sub.add_argument("--task" if command == "run" else "--manifest", required=True)
        sub.add_argument("--workdir", default=".")
        sub.add_argument("--timeout", type=positive, default=900)
        sub.add_argument("--check-timeout", type=positive, default=300)
        sub.add_argument("--max-turns", type=positive_int, default=30)
        sub.add_argument("--model")
        if command == "run":
            sub.add_argument("--isolated", action="store_true")
        else:
            sub.add_argument("--jobs", type=positive_int, default=2)
            sub.set_defaults(isolated=True)
    for command in ("resume", "inspect", "apply", "cleanup"):
        sub = subs.add_parser(command)
        sub.add_argument("--run", required=True)
        if command == "resume":
            sub.add_argument("--prompt-file", required=True)
        if command == "inspect":
            sub.add_argument("--patch", action="store_true")
    subs.add_parser("doctor")
    return result


def main(arguments=None):
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    os.umask(0o077)
    if sys.version_info < (3, 9):
        raise WorkerError("Python 3.9 or newer is required.")
    if not shutil.which("git"):
        raise WorkerError("Git was not found in PATH.")
    legacy = bool(arguments and arguments[0] not in COMMANDS and not arguments[0].startswith("-"))
    legacy_task = None
    if legacy:
        if len(arguments) > 2 or not arguments[0].strip():
            raise WorkerError("Usage: run-cc.sh '<task prompt>' [workdir]")
        legacy_task = {"id": "legacy", "prompt": arguments[0], "read_files": [], "write_files": [], "checks": []}
        args = parser().parse_args(["run", "--task", "unused", "--workdir", arguments[1] if len(arguments) == 2 else "."])
    else:
        args = parser().parse_args(arguments)
    root = state_root()
    if args.command == "doctor":
        executable, help_text = capabilities()
        return {"status": "ok", "python": sys.version.split()[0], "claude": executable,
                "git": shutil.which("git"), "state_dir": str(root),
                "resume_supported": "--resume" in help_text, "max_turns_supported": "--max-turns" in help_text,
                "credential_variables_present": [key for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL") if os.environ.get(key)]}
    if args.command in {"inspect", "apply", "cleanup", "resume"}:
        directory, state = load_run(root, args.run)
        if args.command == "inspect":
            if args.patch:
                sys.stdout.write((directory / "changes.patch").read_text(encoding="utf-8", errors="replace"))
                return None
        elif args.command == "resume":
            correction = Path(args.prompt_file).read_text(encoding="utf-8")
            if not correction.strip():
                raise WorkerError("The correction prompt must not be empty.")
            cli = capabilities()
            if state["isolated"]:
                return execute(directory, state, cli, correction)
            with source_lock(root, Path(state["source"])):
                return execute(directory, state, cli, correction)
        else:
            with locked(directory / "run.lock"), source_lock(root, Path(state["source"])):
                # Reload after acquiring the lock so a finishing worker cannot be overwritten.
                directory, state = load_run(root, args.run)
                if args.command == "apply":
                    apply_patch(state, directory)
                else:
                    remove_worktree(state, directory)
                save(directory, state)
        return public_result(directory, state)
    source = Path(args.workdir).expanduser().resolve()
    if not source.is_dir():
        raise WorkerError("Workdir is not a directory.")
    source = repository(source) or source
    if root == source or source in root.parents:
        raise WorkerError("CC_WORKER_STATE_DIR must be outside the target workspace.")
    if args.command == "run":
        tasks = [legacy_task or validate_task(read_json(args.task))]
    else:
        manifest = read_json(args.manifest)
        if not isinstance(manifest, dict) or set(manifest) != {"tasks"} or not isinstance(manifest["tasks"], list):
            raise WorkerError('A batch manifest must be an object containing only a "tasks" array.')
        tasks = [validate_task(value) for value in manifest["tasks"]]
        validate_batch(tasks)
    cli = capabilities()
    can_isolate = args.isolated and repository(source) is not None and head_exists(source)
    entries = []
    if can_isolate:
        extras = [name for task in tasks for name in task["read_files"] + task["write_files"]]
        with source_lock(root, source):
            tracked = tracked_paths(source)
            validate_paths(source, extras, tracked)
            for task in tasks:
                entries.append(prepare(root, source, task, args, extras, legacy))
        jobs = args.jobs if args.command == "batch" else 1
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            results = list(pool.map(lambda entry: execute(*entry, cli), entries))
    else:
        results = []
        with source_lock(root, source):
            for task in tasks:
                entry = prepare(root, source, task, args, legacy=legacy)
                results.append(execute(*entry, cli))
                if STOP.is_set():
                    break
    if legacy:
        result = results[0]
        print(result.get("summary") or result.get("error") or result["status"])
        print("Run: " + result["run_id"] + " | Artifacts: " + result["artifacts"])
        return {"_legacy_exit": 0 if result["status"] in SUCCESS else 1}
    if args.command == "run":
        return results[0]
    return {"status": "ready_for_review" if all(result["status"] in SUCCESS for result in results) else "batch_failed",
            "mode": "isolated" if can_isolate else "in_place_fallback", "runs": results}


if __name__ == "__main__":
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: STOP.set())
    try:
        value = main()
        if value is not None and "_legacy_exit" in value:
            sys.exit(value["_legacy_exit"])
        if value is not None:
            print(json.dumps(value, ensure_ascii=True))
        sys.exit(130 if STOP.is_set() else (0 if value is None or value.get("status") in SUCCESS | {"ok"} or value.get("cleaned") else 1))
    except (WorkerError, OSError) as error:
        print(json.dumps({"status": "error", "error": redact(str(error))}), file=sys.stderr)
        sys.exit(2)
