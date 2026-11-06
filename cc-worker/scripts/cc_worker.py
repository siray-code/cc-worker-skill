#!/usr/bin/env python3
"""Bounded Claude Code dispatch with isolated workers and reviewable artifacts."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
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
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import urllib.parse
import uuid

from workspace import (
    WorkerError, apply_patch, changed_paths, create_worktree, export_patch,
    git_metadata_writable, head_exists, normalize_path, remove_worktree, repository,
    scan, snapshot, tracked_paths, validate_paths,
)

STOP = threading.Event()
COMMANDS = {"run", "batch", "resume", "inspect", "apply", "cleanup", "doctor",
            "submit", "status", "await", "daemon"}
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


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Progress:
    """Best-effort live progress for a running attempt; never fails the worker."""

    def __init__(self, path, attempt):
        self.path = path
        self.last_write = 0.0
        now = utc_now()
        self.data = {"phase": "preparing", "attempt": attempt, "started_at": now,
                     "updated_at": now, "events": 0, "last_event": None,
                     "observed_files": [], "permission_denials": 0}

    def set(self, **fields):
        self.data.update(fields)

    def observe(self, event):
        self.data["events"] += 1
        self.data["last_event"] = event
        path = event.get("file") if isinstance(event, dict) else None
        if path and path not in self.data["observed_files"]:
            self.data["observed_files"].append(path)
        self.write()

    def write(self, force=False):
        try:
            now = time.monotonic()
            if not force and now - self.last_write < 1:
                return
            self.data["updated_at"] = utc_now()
            write_json(self.path, self.data)
            self.last_write = now
        except OSError:
            pass


def event_summary(raw):
    """Summarize one Claude stream-json line for progress reporting."""
    try:
        event = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(event, dict):
        return None
    kind = event.get("type")
    if kind == "system" and event.get("subtype") == "init":
        return {"type": "system", "session_id": event.get("session_id")}
    if kind == "assistant":
        content = event.get("message", {}).get("content", [])
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "tool_use":
                    payload = item.get("input") if isinstance(item.get("input"), dict) else {}
                    return {"type": "assistant", "tool": item.get("name"),
                            "file": payload.get("file_path") or payload.get("path"),
                            "command": payload.get("command")}
    return None


class StreamReader:
    """Incrementally read JSONL written by a running worker."""

    def __init__(self, path, progress):
        self.path = path
        self.progress = progress
        self.offset = 0
        self.buffer = b""

    def poll(self):
        try:
            with self.path.open("rb") as handle:
                handle.seek(self.offset)
                chunk = handle.read()
                self.offset = handle.tell()
        except OSError:
            return
        if not chunk:
            return
        self.buffer += chunk
        *lines, self.buffer = self.buffer.split(b"\n")
        for line in lines:
            event = event_summary(line.decode("utf-8", "replace"))
            if event:
                self.progress.observe(event)

    def final_result(self):
        try:
            data = self.path.read_bytes()
        except OSError:
            return None
        result = None
        for line in data.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("type") == "result":
                result = event
        return result


CREDENTIAL_KEYS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL")
PROXY_VARIABLES = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")
CLOUD_PROVIDER_VARIABLES = ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")


def settings_env():
    """Read names and values of user-level Claude settings env entries; never print values."""
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    path = (Path(config_dir).expanduser() if config_dir else Path.home() / ".claude") / "settings.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    values = data.get("env") if isinstance(data, dict) else None
    if not isinstance(values, dict):
        return []
    return [(str(path), {key: value for key, value in values.items() if isinstance(value, str)})]


def configured_value(key):
    value = os.environ.get(key, "").strip()
    if value:
        return value
    for _, values in settings_env():
        value = values.get(key, "").strip()
        if value:
            return value
    return ""


def base_url():
    value = os.environ.get("ANTHROPIC_BASE_URL", "").strip()
    if value:
        return value, "environment"
    for source, values in settings_env():
        value = values.get("ANTHROPIC_BASE_URL", "").strip()
        if value:
            return value, source
    return "https://api.anthropic.com", "default"


def provider_profile():
    """Describe the configured provider and the compatibility env it needs."""
    value, source = base_url()
    host, _, _, error = parse_endpoint(value)
    model = configured_value("ANTHROPIC_MODEL")
    anthropic = bool(host) and (host == "api.anthropic.com" or host.endswith(".anthropic.com"))
    deepseek = "deepseek" in (host or "").lower() or "deepseek" in model.lower()
    compat = {}
    disabled = os.environ.get("CC_WORKER_PROVIDER_COMPAT", "").strip().lower() in ("0", "false", "no", "off")
    if not error and host and not disabled and (deepseek or not anthropic):
        compat = {"CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1", "ENABLE_TOOL_SEARCH": "false"}
    return {"host": host, "source": source, "model": model, "anthropic": anthropic,
            "deepseek": deepseek, "compat": compat}


def credential_variables():
    present = {key for key in CREDENTIAL_KEYS if os.environ.get(key)}
    for _, values in settings_env():
        present.update(key for key in CREDENTIAL_KEYS if values.get(key))
    return sorted(present)


def proxy_variables():
    present = [key for key in PROXY_VARIABLES if os.environ.get(key)]
    for _, values in settings_env():
        present += [key for key in PROXY_VARIABLES if values.get(key) and key not in present]
    return present


def parse_endpoint(value):
    try:
        parsed = urllib.parse.urlsplit(value if "://" in value else "//" + value)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        return None, None, None, "Invalid ANTHROPIC_BASE_URL: " + str(exc)
    if not host:
        return None, None, None, "Invalid ANTHROPIC_BASE_URL: the host is missing."
    scheme = parsed.scheme or "https"
    return host, port or (443 if scheme == "https" else 80), scheme, None


def api_reachability():
    """Probe the configured model API host without calling a model."""
    value, source = base_url()
    host, port, _, error = parse_endpoint(value)
    result = {"endpoint_source": source, "host": host, "port": port}
    if error:
        result.update({"status": "invalid", "detail": error})
        return result
    if os.environ.get("CC_WORKER_SKIP_API_CHECK", "").strip():
        result.update({"status": "skipped", "detail": "CC_WORKER_SKIP_API_CHECK is set"})
        return result
    providers = [key for key in CLOUD_PROVIDER_VARIABLES
                 if configured_value(key).lower() not in ("", "0", "false", "no", "off")]
    if providers:
        result.update({"status": "skipped", "detail": "cloud provider configured: " + ", ".join(providers)})
        return result
    proxies = proxy_variables()
    if proxies:
        result.update({"status": "skipped", "detail": "proxy configured: " + ", ".join(proxies)})
        return result
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        result.update({"status": "unreachable", "detail": "DNS lookup failed: " + str(exc)})
        return result
    failure = "no address was returned"
    for family, socktype, proto, _, address in addresses:
        try:
            with socket.socket(family, socktype, proto) as connection:
                connection.settimeout(4)
                connection.connect(address)
            result.update({"status": "ok", "detail": "TCP connection succeeded"})
            return result
        except OSError as exc:
            failure = str(exc)
    result.update({"status": "unreachable", "detail": "TCP connection failed: " + failure})
    return result


def unreachable_message(check):
    detail = check.get("detail") or "preflight failed"
    if check.get("status") == "invalid":
        return "Cannot use the configured model API endpoint (" + detail + "). Fix ANTHROPIC_BASE_URL or the Claude settings before dispatching."
    endpoint = str(check.get("host") or "the configured endpoint") + ":" + str(check.get("port"))
    return ("Cannot reach the Claude model API at " + endpoint + " (" + detail + "). The worker "
            "needs outbound network access. If Codex started it in a workspace-write sandbox, "
            "network access is disabled by default: run from the cc-worker daemon "
            "(scripts/install-daemon.sh, then submit), or dispatch with "
            "sandbox_permissions=\"require_escalated\" if your Codex policy allows it. "
            "No worker changes were made and no attempt was consumed.")


def state_root(override=None):
    default = str(Path(tempfile.gettempdir()) / ("cc-worker-" + str(os.getuid())))
    value = override or os.environ.get("CC_WORKER_STATE_DIR", default)
    path = Path(value).expanduser().absolute()
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
            "changed_files", "out_of_scope", "checks", "error", "fallback_reason", "cleaned",
            "metrics", "api_check", "private_objects", "git_metadata_writable",
            "provider", "compat_env", "permission_denials")
    result = {key: state[key] for key in keys if key in state}
    for key in ("changed_files", "out_of_scope"):
        if len(result.get(key, [])) > 50:
            result[key + "_count"] = len(result[key])
            result[key] = result[key][:50]
    if len(result.get("permission_denials", [])) > 20:
        result["permission_denials_count"] = len(result["permission_denials"])
        result["permission_denials"] = result["permission_denials"][:20]
    if result.get("permission_denials"):
        result["warnings"] = ["Some worker commands were denied by the permission policy; declare required commands in checks or instruct the worker to report them."]
    attempt = directory / ("attempt-" + str(state.get("attempts", 0)))
    if (attempt / "progress.json").is_file():
        try:
            result["progress"] = read_json(attempt / "progress.json")
        except WorkerError:
            pass
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


REQUEST_TYPES = {"run", "resume", "apply", "cleanup"}
REQUEST_FIELDS = {"version", "request_id", "type", "task", "workdir", "run_id", "prompt",
                  "isolated", "timeout", "check_timeout", "max_turns", "model", "created_at"}
RUN_ID_PATTERN = re.compile(r"[a-zA-Z0-9_-]+-[a-f0-9]{12}")
REQUEST_ID_PATTERN = re.compile(r"[a-z0-9-]{1,64}")


def queue_root(root):
    return root / "queue"


def ensure_queue(root):
    queue = queue_root(root)
    for name in ("requests", "claimed", "status", "results", "rejected", "logs"):
        path = queue / name
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise WorkerError("Queue directory must be owned by you with mode 700: " + str(path))
    return queue


def daemon_heartbeat(root):
    """Return a fresh daemon heartbeat, or None when the daemon is not running."""
    path = queue_root(root) / "daemon.json"
    try:
        data = read_json(path)
    except WorkerError:
        return None
    if not isinstance(data, dict):
        return None
    try:
        updated = datetime.fromisoformat(str(data.get("updated_at")))
    except (TypeError, ValueError):
        return None
    if (datetime.now(timezone.utc) - updated).total_seconds() > 15:
        return None
    return data


def install_daemon_path():
    return str(Path(__file__).resolve().parent / "install-daemon.sh")


def daemon_not_running_message():
    return ("The cc-worker daemon is not running. Install/start it once in a normal terminal with: "
            + install_daemon_path())


def validate_request(value):
    if not isinstance(value, dict):
        raise WorkerError("Each queue request must be a JSON object.")
    unknown = set(value) - REQUEST_FIELDS
    if unknown:
        raise WorkerError("Unknown request fields: " + ", ".join(sorted(unknown)))
    if value.get("version") != 1:
        raise WorkerError("Unsupported request version.")
    request_id = value.get("request_id")
    if not isinstance(request_id, str) or not REQUEST_ID_PATTERN.fullmatch(request_id):
        raise WorkerError("Invalid request ID.")
    kind = value.get("type")
    if kind not in REQUEST_TYPES:
        raise WorkerError("Invalid request type.")
    for key in ("timeout", "check_timeout"):
        number = value.get(key)
        if not isinstance(number, (int, float)) or not math.isfinite(number) or number <= 0:
            raise WorkerError("Invalid " + key + ".")
    if not isinstance(value.get("max_turns"), int) or value["max_turns"] < 1:
        raise WorkerError("Invalid max_turns.")
    if not isinstance(value.get("isolated"), bool):
        raise WorkerError("Invalid isolated flag.")
    if value.get("model") is not None and not isinstance(value.get("model"), str):
        raise WorkerError("Invalid model.")
    if kind == "run":
        value["task"] = validate_task(value.get("task"))
        workdir = value.get("workdir")
        if not isinstance(workdir, str) or not workdir:
            raise WorkerError("A run request requires workdir.")
        if not os.path.isabs(workdir):
            raise WorkerError("A run request requires an absolute workdir.")
    elif kind == "resume":
        if not isinstance(value.get("run_id"), str) or not RUN_ID_PATTERN.fullmatch(value["run_id"]):
            raise WorkerError("A resume request requires a valid run ID.")
        if not isinstance(value.get("prompt"), str) or not value["prompt"].strip():
            raise WorkerError("A resume request requires a nonempty prompt.")
    else:
        if not isinstance(value.get("run_id"), str) or not RUN_ID_PATTERN.fullmatch(value["run_id"]):
            raise WorkerError("This request requires a valid run ID.")
    return value


def submit_request(root, args):
    kind = "run"
    if args.apply:
        kind = "apply"
    if args.cleanup:
        if kind != "run":
            raise WorkerError("Choose only one of --resume, --apply or --cleanup.")
        kind = "cleanup"
    if args.resume:
        if kind != "run":
            raise WorkerError("Choose only one of --resume, --apply or --cleanup.")
        kind = "resume"
    if kind != "run" and args.task:
        raise WorkerError("--task cannot be combined with --resume, --apply or --cleanup.")
    queue = ensure_queue(root)
    heartbeat = daemon_heartbeat(root)
    if heartbeat is None:
        raise WorkerError(daemon_not_running_message())
    api = heartbeat.get("api") if isinstance(heartbeat.get("api"), dict) else {}
    if api.get("status") in {"unreachable", "invalid"}:
        raise WorkerError("The cc-worker daemon cannot reach the model API: "
                          + str(api.get("detail") or api.get("status"))
                          + " Fix network/provider access, then retry.")
    if not heartbeat.get("claude"):
        raise WorkerError("The cc-worker daemon cannot find Claude Code. Rerun "
                          + install_daemon_path() + " from a terminal where `claude` is in PATH.")
    request = {"version": 1, "request_id": kind + "-" + uuid.uuid4().hex[:12], "type": kind,
               "task": None, "workdir": None, "run_id": None, "prompt": None,
               "isolated": bool(args.isolated), "timeout": args.timeout,
               "check_timeout": args.check_timeout, "max_turns": args.max_turns,
               "model": args.model, "created_at": utc_now()}
    if kind == "run":
        if not args.task:
            raise WorkerError("A run request requires --task.")
        request["task"] = validate_task(read_json(args.task))
        workdir = Path(args.workdir).expanduser().resolve()
        if not workdir.is_dir():
            raise WorkerError("Workdir is not a directory.")
        if root == workdir or root in workdir.parents:
            raise WorkerError("CC_WORKER_STATE_DIR must be outside the target workspace.")
        request["workdir"] = str(workdir)
    else:
        run_id = args.resume or args.apply or args.cleanup
        directory, state = load_run(root, run_id)
        request["run_id"] = run_id
        request["workdir"] = str(Path(state["source"]).resolve())
        request["isolated"] = bool(state.get("isolated"))
        if kind == "resume":
            if not args.prompt_file:
                raise WorkerError("A resume request requires --prompt-file.")
            prompt = Path(args.prompt_file).read_text(encoding="utf-8")
            if not prompt.strip():
                raise WorkerError("The correction prompt must not be empty.")
            request["prompt"] = prompt
    validate_request(request)
    path = queue / "requests" / (request["request_id"] + ".json")
    try:
        write_json(path, request)
    except OSError as exc:
        raise WorkerError("Cannot write the queue under " + str(root) + " (" + str(exc)
                          + "). Use a state directory that the Codex sandbox can write, such as $TMPDIR.") from exc
    return {"status": "ok", "request_id": request["request_id"], "request_status": "queued",
            "state_dir": str(root)}


def request_status(root, request_id):
    if not isinstance(request_id, str) or not REQUEST_ID_PATTERN.fullmatch(request_id):
        raise WorkerError("Invalid request ID.")
    queue = queue_root(root)
    status_path = queue / "status" / (request_id + ".json")
    status = None
    try:
        status = read_json(status_path)
    except WorkerError:
        status = None
    if not isinstance(status, dict):
        for directory, state in (("requests", "queued"), ("claimed", "running")):
            path = queue / directory / (request_id + ".json")
            if path.is_file():
                status = {"version": 1, "request_id": request_id, "state": state,
                          "updated_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")}
                break
    if not isinstance(status, dict):
        raise WorkerError("Request not found in the current state directory.")
    if status.get("state") == "running" and status.get("run_id"):
        run_dir = root / str(status["run_id"])
        attempts = sorted((path for path in run_dir.glob("attempt-*") if path.is_dir()),
                          key=lambda path: int(path.name.rsplit("-", 1)[-1])
                          if path.name.rsplit("-", 1)[-1].isdigit() else 0)
        if attempts:
            progress = attempts[-1] / "progress.json"
            if progress.is_file():
                try:
                    status["progress"] = read_json(progress)
                except WorkerError:
                    pass
    result_path = queue / "results" / (request_id + ".json")
    if result_path.is_file():
        try:
            status["result"] = read_json(result_path)
        except WorkerError:
            pass
    if status.get("state") == "queued":
        queued = sorted((queue / "requests").glob("*.json"))
        ids = [path.stem for path in queued]
        if request_id in ids:
            status["position"] = ids.index(request_id) + 1
    return {"status": "ok", "request": status}


def await_request(root, request_id, timeout):
    deadline = time.monotonic() + timeout
    while True:
        request = request_status(root, request_id)["request"]
        state = request.get("state")
        if state in {"done", "failed", "interrupted", "rejected"}:
            if "result" in request:
                return request["result"]
            return {"status": "worker_failed" if state in {"failed", "interrupted"} else "error",
                    "error": request.get("error") or ("Request " + state)}
        if STOP.is_set():
            return {"status": "cancelled"}
        if time.monotonic() >= deadline:
            return {"status": "timed_out", "error": "Timed out waiting for request " + request_id}
        time.sleep(1)


def request_source(root, request):
    if request.get("type") == "run":
        try:
            workdir = Path(request["workdir"]).resolve()
            return repository(workdir) or workdir
        except (OSError, WorkerError):
            return Path(request["request_id"])
    try:
        _, state = load_run(root, request["run_id"])
        return Path(state["source"]).resolve()
    except WorkerError:
        return Path(request["request_id"])


def probe_directory(value):
    path = Path(value).expanduser().absolute()
    try:
        next(iter(path.iterdir()), None)
        return {"path": str(path), "readable": True}
    except OSError as exc:
        return {"path": str(path), "readable": False, "error": str(exc)}


def reject_request(queue, path, error):
    request_id = path.stem
    try:
        os.replace(path, queue / "rejected" / path.name)
    except OSError:
        pass
    status = {"version": 1, "request_id": request_id, "state": "rejected",
              "updated_at": utc_now(), "error": redact(str(error))}
    try:
        write_json(queue / "status" / (request_id + ".json"), status)
    except OSError:
        pass


def run_locked(root, source, directory, action):
    """Run an operation under the source lock, retrying brief external contention."""
    for attempt in range(3):
        try:
            with source_lock(root, source):
                if directory is None:
                    return action()
                with locked(directory / "run.lock"):
                    return action()
        except WorkerError as exc:
            if "Another operation owns" in str(exc) and attempt < 2:
                time.sleep(2)
                continue
            raise


def run_request(root, claimed, request, active, active_lock, executable, help_text):
    request_id = request["request_id"]
    status_path = queue_root(root) / "status" / (request_id + ".json")
    source = request_source(root, request)
    status = {"version": 1, "request_id": request_id, "state": "running",
              "run_id": request.get("run_id"), "submitted_at": request.get("created_at"),
              "started_at": utc_now(), "updated_at": utc_now()}
    try:
        write_json(status_path, status)

        def mark_running(state):
            status.update({"state": "running", "run_id": state["run_id"], "updated_at": utc_now()})
            write_json(status_path, status)

        if request["type"] == "run":
            options = SimpleNamespace(timeout=request["timeout"], check_timeout=request["check_timeout"],
                                      max_turns=request["max_turns"], model=request["model"],
                                      isolated=request["isolated"])
            workdir = Path(request["workdir"]).resolve()
            workdir = repository(workdir) or workdir

            def prepare_run():
                for attempt in range(3):
                    try:
                        with source_lock(root, source):
                            directory, state = prepare(root, workdir, request["task"],
                                                       options, legacy=False)
                            mark_running(state)
                            if not state["isolated"]:
                                return execute(directory, state, (executable, help_text))
                        # Isolated worktrees do not need the source lock during execution,
                        # matching main()'s batch path.
                        return execute(directory, state, (executable, help_text))
                    except WorkerError as exc:
                        if "Another operation owns" in str(exc) and attempt < 2:
                            time.sleep(2)
                            continue
                        raise

            result = prepare_run()
        elif request["type"] == "resume":
            directory, state = load_run(root, request["run_id"])
            mark_running(state)
            # execute() acquires the run lock itself. Isolated runs only need the
            # source lock during preparation; in-place runs hold it throughout.
            if state["isolated"]:
                result = execute(directory, state, (executable, help_text), request["prompt"])
            else:
                result = run_locked(root, source, None,
                                    lambda: execute(directory, state, (executable, help_text),
                                                    request["prompt"]))
        elif request["type"] == "apply":
            directory, state = load_run(root, request["run_id"])

            def apply_run():
                current_dir, current = load_run(root, request["run_id"])
                apply_patch(current, current_dir)
                save(current_dir, current)
                return public_result(current_dir, current)

            result = run_locked(root, source, directory, apply_run)
        else:
            directory, state = load_run(root, request["run_id"])

            def cleanup_run():
                current_dir, current = load_run(root, request["run_id"])
                remove_worktree(current, current_dir)
                save(current_dir, current)
                return public_result(current_dir, current)

            result = run_locked(root, source, directory, cleanup_run)
        final = "done" if result.get("status") in SUCCESS or result.get("cleaned") else "failed"
        status.update({"state": final, "updated_at": utc_now(),
                       "run_id": result.get("run_id", status.get("run_id")),
                       "result_file": result.get("result_file"), "error": result.get("error")})
        write_json(queue_root(root) / "results" / (request_id + ".json"), result)
    except (WorkerError, OSError) as exc:
        status.update({"state": "failed", "updated_at": utc_now(), "error": redact(str(exc))})
    except Exception as exc:  # Defensive: never leave a request silently running.
        status.update({"state": "failed", "updated_at": utc_now(), "error": redact(repr(exc))})
    finally:
        try:
            write_json(status_path, status)
        except OSError:
            pass
        with active_lock:
            active.pop(source, None)


def daemon_loop(root, args):
    queue = ensure_queue(root)
    # One daemon per state directory; the OS releases the lock when the process exits.
    lock_handle = (queue / "daemon.lock").open("a")
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise WorkerError("Another cc-worker daemon owns this state directory.") from exc
    executable, help_text = capabilities()
    api = api_reachability()
    profile = provider_profile()
    probe = probe_directory(args.probe_dir)
    active = {}
    active_lock = threading.Lock()
    started = utc_now()

    def heartbeat_loop():
        nonlocal api
        last_api = time.monotonic()
        while not STOP.is_set():
            if time.monotonic() - last_api > 30:
                api = api_reachability()
                last_api = time.monotonic()
            with active_lock:
                current = sorted(active.values())
            data = {"version": 1, "pid": os.getpid(), "started_at": started,
                    "updated_at": utc_now(), "state_dir": str(root), "jobs": args.jobs,
                    "active": current, "claude": executable, "git": shutil.which("git"),
                    "python": sys.executable, "api": api,
                    "provider": {"host": profile["host"], "anthropic": profile["anthropic"],
                                 "compat": sorted(profile["compat"])},
                    "probe": probe}
            try:
                write_json(queue / "daemon.json", data)
            except OSError:
                pass
            STOP.wait(5)

    heartbeat_thread = threading.Thread(target=heartbeat_loop, daemon=True)
    heartbeat_thread.start()
    for claimed in sorted((queue / "claimed").glob("*.json")):
        request_id = claimed.stem
        status_path = queue / "status" / (request_id + ".json")
        current = {}
        try:
            current = read_json(status_path)
        except WorkerError:
            pass
        if not isinstance(current, dict) or current.get("state") in {"running", "queued", "preparing"}:
            current = dict(current) if isinstance(current, dict) else {}
            run_id = current.get("run_id")
            if run_id:
                try:
                    run_state = read_json(root / str(run_id) / "result.json")
                except WorkerError:
                    run_state = None
                if isinstance(run_state, dict) and run_state.get("status") not in {
                        "preparing", "prepared", "running", "validating"}:
                    result = public_result(root / str(run_id), run_state)
                    try:
                        write_json(queue / "results" / (request_id + ".json"), result)
                    except OSError:
                        pass
                    current.update({"state": "done" if run_state.get("status") in SUCCESS else "failed",
                                    "updated_at": utc_now(), "result_file": result.get("result_file"),
                                    "error": run_state.get("error")})
                    try:
                        write_json(status_path, current)
                    except OSError:
                        pass
                    continue
            current.update({"version": 1, "request_id": request_id, "state": "interrupted",
                            "updated_at": utc_now(),
                            "error": "The cc-worker daemon restarted while this request was running."})
            try:
                write_json(status_path, current)
            except OSError:
                pass
    executor = ThreadPoolExecutor(max_workers=args.jobs)
    try:
        while not STOP.is_set():
            with active_lock:
                busy = len(active)
            if busy < args.jobs:
                for path in sorted((queue / "requests").glob("*.json")):
                    if STOP.is_set():
                        break
                    with active_lock:
                        if len(active) >= args.jobs:
                            break
                    try:
                        request = validate_request(read_json(path))
                    except (WorkerError, OSError) as exc:
                        reject_request(queue, path, exc)
                        continue
                    source = request_source(root, request)
                    with active_lock:
                        if source in active:
                            continue
                        active[source] = request["request_id"]
                    claimed = queue / "claimed" / path.name
                    try:
                        os.replace(path, claimed)
                    except OSError:
                        with active_lock:
                            active.pop(source, None)
                        continue
                    executor.submit(run_request, root, claimed, request, active, active_lock,
                                    executable, help_text)
            STOP.wait(args.poll)
    finally:
        executor.shutdown(wait=True)
        heartbeat_thread.join(timeout=6)
    return None


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


def process_run(argv, workdir, stdout, stderr, timeout, stdin=None, env=None, on_tick=None):
    started = time.monotonic()
    if STOP.is_set():
        return {"exit_code": None, "status": "cancelled", "seconds": 0}
    try:
        process = subprocess.Popen(argv, cwd=str(workdir), stdin=stdin or subprocess.DEVNULL,
                                   stdout=stdout, stderr=stderr, start_new_session=True, env=env)
    except OSError:
        return {"exit_code": None, "status": "launch_failed", "seconds": 0}
    status = "finished"
    last_tick = started
    try:
        while process.poll() is None:
            if STOP.is_set() or time.monotonic() - started >= timeout:
                status = "cancelled" if STOP.is_set() else "timed_out"
                stop_process(process)
                break
            if on_tick is not None and time.monotonic() - last_tick >= 0.5:
                try:
                    on_tick()
                except Exception:
                    # Progress reporting must never fail the worker.
                    pass
                last_tick = time.monotonic()
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
             "private_objects": False, "git_metadata_writable": True,
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
            writable = git_metadata_writable(source)
            private_objects = not writable
            try:
                state["baseline"] = snapshot(source, directory, extra_files or names,
                                               all_untracked=legacy, private_objects=private_objects)
            except WorkerError as exc:
                markers = ("Operation not permitted", "Permission denied",
                           "unable to create temporary file", "Read-only file system")
                if private_objects or not any(marker in str(exc) for marker in markers):
                    raise
                # The probe can pass where a sandbox still denies object writes.
                private_objects = True
                writable = False
                state["baseline"] = snapshot(source, directory, extra_files or names,
                                               all_untracked=legacy, private_objects=True)
            state["private_objects"] = private_objects
            state["git_metadata_writable"] = writable
        if options.isolated:
            if not state["is_git"] or not head_exists(source):
                state["fallback_reason"] = "Isolation requires a Git repository with an existing commit; running in place."
            elif state["private_objects"]:
                state["fallback_reason"] = "Git metadata is read-only (sandbox); running in place."
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
        state["error"] = redact(str(exc))
    save(directory, state)
    return directory, state


def redact(text):
    secrets = [os.environ.get(key) for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")]
    for _, values in settings_env():
        secrets += [values.get(key) for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")]
    for secret in secrets:
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
                if state["attempts"] == 0:
                    raise WorkerError("This run never started a worker; fix the reported issue and dispatch a new run.")
                raise WorkerError("No resumable Claude session is available; review the result before deciding on takeover.")
            if scan(Path(state["workdir"]), state["is_git"]) != state.get("after"):
                raise WorkerError("Workspace changed after the last attempt; review before redispatching.")
        check = api_reachability()
        state["api_check"] = check
        if check["status"] in {"unreachable", "invalid"}:
            state["status"] = "worker_failed"
            state["error"] = redact(unreachable_message(check))
            save(directory, state)
            return public_result(directory, state)
        state["attempts"] += 1
        state["status"] = "running"
        state.pop("error", None)
        state["checks"] = []
        state["summary"] = ""
        state["permission_denials"] = []
        save(directory, state)
        progress = None
        try:
            task = state["task"]
            rules = (Path(__file__).resolve().parent.parent / "references" / "worker-rules.md").read_text(encoding="utf-8")
            profile = provider_profile()
            worker_env = os.environ.copy()
            worker_env.update(profile["compat"])
            state["provider"] = {key: profile[key] for key in ("host", "source", "anthropic", "deepseek")}
            state["compat_env"] = sorted(profile["compat"])
            allowed = ["Read", "Edit", "Write", "Glob", "Grep"]
            for command in task["checks"]:
                allowed.append("Bash(" + shlex.join(command) + ")")
                if command[0] != "rtk":
                    # The user's RTK hook may rewrite Bash commands to `rtk ...`.
                    allowed.append("Bash(" + shlex.join(["rtk", *command]) + ")")
            allowed += ["Bash(rtk wc *)", "Bash(rtk read *)", "Bash(rtk ls *)",
                        "Bash(rtk tree *)", "Bash(rtk find *)", "Bash(rtk grep *)", "Bash(rtk rg *)"]
            stream_supported = "stream-json" in help_text and "--verbose" in help_text
            argv = [executable, "-p", "--append-system-prompt", rules,
                    "--permission-mode", "acceptEdits", "--allowedTools", *allowed,
                    "--tools", "Read,Edit,Write,Glob,Grep,Bash",
                    "--output-format", "stream-json" if stream_supported else "json"]
            if stream_supported:
                argv.append("--verbose")
            for flag in ("--disable-slash-commands",):
                if flag in help_text:
                    argv.append(flag)
            if "--permission-prompts" in help_text:
                argv += ["--permission-prompts", "none"]
            if "--strict-mcp-config" in help_text:
                # The worker only needs built-in coding tools. Do not start the
                # user's MCP servers: they can create untracked files (for example
                # Serena's .serena/) and turn a clean run into a scope violation.
                argv.append("--strict-mcp-config")
                if "--mcp-config" in help_text:
                    argv += ["--mcp-config", '{"mcpServers":{}}']
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
            progress = Progress(attempt / "progress.json", state["attempts"])
            progress.set(phase="worker")
            progress.write(force=True)
            stream_reader = StreamReader(attempt / "events.jsonl", progress) if stream_supported else None
            output = attempt / ("events.jsonl" if stream_supported else "stdout.json")
            with (attempt / "prompt.json").open("rb") as stdin, output.open("wb") as stdout, (attempt / "stderr.log").open("wb") as stderr:
                outcome = process_run(argv, Path(state["workdir"]), stdout, stderr,
                                      state["options"]["timeout"], stdin, env=worker_env,
                                      on_tick=stream_reader.poll if stream_reader else progress.write)
            if stream_reader is not None:
                stream_reader.poll()
                stream_payload = stream_reader.final_result()
                if stream_payload is not None:
                    write_json(attempt / "stdout.json", stream_payload)
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
                    denials = payload.get("permission_denials")
                    if isinstance(denials, list):
                        state["permission_denials"] = denials
                    if outcome["exit_code"] == 0 and not payload.get("is_error") and payload.get("subtype", "success") == "success":
                        if not session or "result" not in payload:
                            raise WorkerError("Claude result lacks a session ID or final result.")
                        state["status"] = "validating"
                        state["summary"] = redact(payload.get("result", ""))[:1200]
                        state["metrics"] = {key: payload[key] for key in ("num_turns", "duration_ms", "total_cost_usd", "usage") if key in payload}
                    else:
                        detail = payload.get("result")
                        detail = detail.strip() if isinstance(detail, str) else ""
                        notes = []
                        if payload.get("api_error"):
                            notes.append(str(payload["api_error"]))
                        if payload.get("subtype") not in (None, "success"):
                            notes.append("subtype " + str(payload["subtype"]))
                        if notes:
                            detail = (detail + " " if detail else "") + "(" + ", ".join(notes) + ")"
                        if not detail:
                            detail = "Claude exited with code " + str(outcome["exit_code"]) + "."
                        if payload.get("api_error") == "connection_failed" or any(
                                marker in detail for marker in ("ENOTFOUND", "ECONNREFUSED", "EAI_AGAIN")):
                            detail += (" The worker could not reach the model API. If it ran inside a sandbox, "
                                       "enable network access (for example with sandbox_permissions=\"require_escalated\") "
                                       "and dispatch again.")
                        if payload.get("api_error_status") == 422 and "tool_definition" in detail:
                            detail += (" The provider rejected Claude Code's tool-search payload. "
                                       "Keep ENABLE_TOOL_SEARCH=false and CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1 "
                                       "for non-Anthropic endpoints (do not set CC_WORKER_PROVIDER_COMPAT=off).")
                        state["error"] = redact(detail)[:1200]
                except (WorkerError, ValueError, TypeError, AttributeError):
                    state["error"] = "Claude did not return a usable JSON result; inspect the attempt logs."
            if state["status"] == "validating":
                if progress is not None:
                    progress.set(phase="checks", check=0, check_total=len(task["checks"]))
                    progress.write(force=True)
                for index, command in enumerate(task["checks"], 1):
                    if progress is not None:
                        progress.set(check=index, check_total=len(task["checks"]))
                        progress.write(force=True)
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
                    private_objects = state.get("private_objects", False)
                    if not private_objects:
                        try:
                            private_objects = not git_metadata_writable(workdir)
                        except (WorkerError, OSError):
                            private_objects = True
                    markers = ("Operation not permitted", "Permission denied",
                               "unable to create temporary file", "Read-only file system")
                    try:
                        sha = export_patch(workdir, directory, state["baseline"], state["before"],
                                           state["task"]["write_files"],
                                           private_objects=private_objects)
                    except WorkerError as exc:
                        if private_objects or not any(marker in str(exc) for marker in markers):
                            raise
                        private_objects = True
                        sha = export_patch(workdir, directory, state["baseline"], state["before"],
                                           state["task"]["write_files"], private_objects=True)
                    state["private_objects"] = private_objects
                    state["patch_sha256"] = sha
                if state["out_of_scope"]:
                    state["status"] = "scope_violation"
                validate_paths(workdir, state["task"]["write_files"], tracked_paths(workdir) if state["is_git"] else set())
            except (WorkerError, OSError) as exc:
                state["status"] = "scope_violation"
                state["error"] = redact(str(exc))
            if progress is not None:
                progress.set(phase="finished", status=state.get("status"),
                             permission_denials=len(state.get("permission_denials", [])))
                progress.write(force=True)
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
    submit = subs.add_parser("submit")
    submit.add_argument("--task")
    submit.add_argument("--workdir", default=".")
    submit.add_argument("--isolated", action="store_true")
    submit.add_argument("--timeout", type=positive, default=900)
    submit.add_argument("--check-timeout", type=positive, default=300)
    submit.add_argument("--max-turns", type=positive_int, default=30)
    submit.add_argument("--model")
    submit.add_argument("--resume", metavar="RUN")
    submit.add_argument("--apply", metavar="RUN")
    submit.add_argument("--cleanup", metavar="RUN")
    submit.add_argument("--prompt-file")
    status = subs.add_parser("status")
    status.add_argument("--request", required=True)
    await_parser = subs.add_parser("await")
    await_parser.add_argument("--request", required=True)
    await_parser.add_argument("--timeout", type=positive, default=3600)
    daemon = subs.add_parser("daemon")
    daemon.add_argument("--state-dir")
    daemon.add_argument("--poll", type=positive, default=1)
    daemon.add_argument("--jobs", type=positive_int, default=1)
    daemon.add_argument("--probe-dir", default=".")
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
    if args.command == "daemon":
        daemon_loop(state_root(args.state_dir), args)
        return {"_daemon_exit": True}
    root = state_root()
    if args.command == "submit":
        return submit_request(root, args)
    if args.command == "status":
        return request_status(root, args.request)
    if args.command == "await":
        return await_request(root, args.request, args.timeout)
    if args.command == "doctor":
        executable, help_text = capabilities()
        api = api_reachability()
        profile = provider_profile()
        daemon = daemon_heartbeat(root)
        sandbox = {"codex": bool(os.environ.get("CODEX_SANDBOX") or os.environ.get("CODEX_SANDBOX_NETWORK_DISABLED")),
                   "network_disabled_env": os.environ.get("CODEX_SANDBOX_NETWORK_DISABLED") == "1"}
        daemon_info = {"running": daemon is not None,
                       "installed": Path(install_daemon_path()).is_file()}
        if daemon is not None:
            daemon_info.update({key: daemon.get(key) for key in
                                ("pid", "started_at", "updated_at", "state_dir", "jobs",
                                 "active", "claude", "git", "api", "provider", "probe")})
        dispatch = {"status": "ok", "mode": "direct"}
        if daemon is not None:
            daemon_api = daemon.get("api") if isinstance(daemon.get("api"), dict) else {}
            if not daemon.get("claude"):
                dispatch = {"status": "blocked", "mode": None, "reason": "daemon_unusable",
                            "remedy": "Rerun " + install_daemon_path()
                                      + " from a terminal where `claude` and `git` are in PATH."}
            elif daemon_api.get("status") in {"unreachable", "invalid"}:
                dispatch = {"status": "blocked", "mode": None, "reason": "daemon_api_unreachable",
                            "remedy": "The daemon cannot reach the model API: "
                                      + str(daemon_api.get("detail") or daemon_api.get("status"))}
            else:
                dispatch = {"status": "ok", "mode": "daemon"}
        elif api["status"] not in {"ok", "skipped"}:
            dispatch = {"status": "blocked", "mode": None, "reason": "daemon_not_installed",
                        "remedy": "Install/start the daemon once in a normal terminal: "
                                  + install_daemon_path()}
        result = {"status": "ok", "python": sys.version.split()[0], "claude": executable,
                  "git": shutil.which("git"), "state_dir": str(root),
                  "resume_supported": "--resume" in help_text, "max_turns_supported": "--max-turns" in help_text,
                  "credential_variables_present": credential_variables(), "api": api,
                  "provider": {"host": profile["host"], "source": profile["source"],
                               "anthropic": profile["anthropic"], "deepseek": profile["deepseek"],
                               "compat": profile["compat"]},
                  "sandbox": sandbox, "daemon": daemon_info, "dispatch": dispatch}
        if dispatch["status"] == "blocked":
            result["warnings"] = ["Dispatch is blocked: " + dispatch.get("remedy", dispatch.get("reason", ""))]
        return result
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
        if value is not None and value.get("_daemon_exit"):
            # launchd KeepAlive should not treat a clean SIGTERM shutdown as failure.
            sys.exit(0)
        if value is not None:
            print(json.dumps(value, ensure_ascii=True))
        sys.exit(130 if STOP.is_set() else (0 if value is None or value.get("status") in SUCCESS | {"ok"} or value.get("cleaned") else 1))
    except (WorkerError, OSError) as error:
        print(json.dumps({"status": "error", "error": redact(str(error))}), file=sys.stderr)
        sys.exit(2)
