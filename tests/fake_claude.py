#!/usr/bin/env python3
"""Deterministic CLI double. Never calls an API."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid


if "--help" in sys.argv:
    print(os.environ.get("FAKE_HELP", "--append-system-prompt --output-format --permission-mode --allowedTools --tools --resume --max-turns --model --permission-prompts --disable-slash-commands"))
    sys.exit(0)

request = json.load(sys.stdin)
try:
    action = json.loads(request["instructions"])
except ValueError:
    action = {"writes": {"legacy.txt": request["instructions"]}}
if "--resume" in sys.argv and "on_resume" in action:
    action = action["on_resume"]
session = sys.argv[sys.argv.index("--resume") + 1] if "--resume" in sys.argv else str(uuid.uuid4())


def record(event):
    if action.get("record"):
        entry = {"event": event, "task": request["task_id"], "session": session, "argv": sys.argv[1:], "time": time.monotonic()}
        fd = os.open(action["record"], os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (json.dumps(entry) + "\n").encode())
        finally:
            os.close(fd)


record("start")
for name, expected in action.get("assert_files", {}).items():
    if expected is None:
        assert not Path(name).exists(), name
    else:
        assert Path(name).read_text() == expected, name
for name, content in action.get("writes", {}).items():
    path = Path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
for name, values in action.get("binary_writes", {}).items():
    Path(name).write_bytes(bytes(values))
for name in action.get("delete", []):
    Path(name).unlink()
for name in action.get("chmod", []):
    Path(name).chmod(0o755)
for name, target in action.get("symlinks", {}).items():
    Path(name).unlink(missing_ok=True)
    Path(name).symlink_to(target)
if action.get("child_marker"):
    code = "import signal,time; from pathlib import Path; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(1.5); Path(" + repr(action["child_marker"]) + ").write_text('survived')"
    subprocess.Popen([sys.executable, "-c", code])
time.sleep(action.get("sleep", 0))
record("end")
if action.get("malformed"):
    print("not JSON")
else:
    payload = {"type": "result", "subtype": "success", "is_error": action.get("is_error", False),
                      "session_id": session, "result": action.get("summary", "Implemented requested change."),
                      "num_turns": 2, "usage": {"input_tokens": 100, "output_tokens": 20}}
    for key in action.get("omit_fields", []):
        payload.pop(key, None)
    for key in ("api_error", "terminal_reason"):
        if action.get(key):
            payload[key] = action[key]
    print(json.dumps(payload))
sys.exit(1 if action.get("fail") else 0)
