"""Behavioral integration tests against real temporary Git repositories."""

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

REPO = Path(__file__).resolve().parents[1]
RUNNER = REPO / "cc-worker" / "scripts" / "run-cc.sh"


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cc-worker-tests-")
        self.base = Path(self.temp.name).resolve()
        self.source = self.base / "project with spaces"
        self.source.mkdir()
        self.bin = self.base / "bin"
        self.bin.mkdir()
        shutil.copyfile(REPO / "tests" / "fake_claude.py", self.bin / "claude")
        (self.bin / "claude").chmod(0o755)
        self.env = os.environ.copy()
        self.env.update({"PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
                         "CC_WORKER_STATE_DIR": str(self.base / "state"), "CC_WORKER_ENV_FILE": "",
                         "CC_WORKER_SKIP_API_CHECK": "1", "PYTHONDONTWRITEBYTECODE": "1",
                         "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0"})
        for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "FAKE_HELP",
                    "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
            self.env.pop(key, None)
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@localhost")
        (self.source / "a.txt").write_text("original a\n")
        (self.source / "b.txt").write_text("original b\n")
        (self.source / ".gitignore").write_text("cache/\n.env\n")
        self.git("add", ".")
        self.git("commit", "-qm", "Fixture")

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *argv):
        return subprocess.run(["git", *argv], cwd=self.source, env=self.env, capture_output=True, check=True).stdout

    def task(self, name="edit", action=None, writes=None, reads=None, checks=None):
        return {"id": name, "prompt": json.dumps(action or {"writes": {"a.txt": "worker a\n"}}),
                "read_files": reads or [], "write_files": ["a.txt"] if writes is None else writes,
                "checks": checks or []}

    def file(self, name, value):
        path = self.base / name
        path.write_text(json.dumps(value))
        return str(path)

    def invoke(self, *argv, code=0):
        result = subprocess.run([str(RUNNER), *map(str, argv)], cwd=self.source, env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return json.loads(result.stdout if result.stdout else result.stderr)

    def run_task(self, task=None, isolated=True, code=0, options=()):
        argv = ["run", "--task", self.file("task.json", task or self.task()), "--workdir", self.source]
        if isolated:
            argv.append("--isolated")
        return self.invoke(*argv, *options, code=code)

    def index(self):
        return (self.source / ".git" / "index").read_bytes()

    def test_dirty_snapshot_preserves_index_and_exports_only_worker_delta(self):
        (self.source / "a.txt").write_text("staged a\n")
        self.git("add", "a.txt")
        (self.source / "a.txt").write_text("user working a\n")
        (self.source / "staged-new.txt").write_text("staged new\n")
        self.git("add", "staged-new.txt")
        (self.source / "reference.txt").write_text("untracked reference\n")
        (self.source / "private.txt").write_text("not selected\n")
        (self.source / ".env").write_text("private secret\n")
        index = self.index()
        head = self.git("rev-parse", "HEAD")
        action = {"assert_files": {"a.txt": "user working a\n", "staged-new.txt": "staged new\n",
                                   "reference.txt": "untracked reference\n", "private.txt": None, ".env": None},
                  "writes": {"a.txt": "worker a\n"}}
        result = self.run_task(self.task(action=action, reads=["reference.txt"]))
        self.assertEqual(result["changed_files"], ["a.txt"])
        self.assertEqual((self.source / "a.txt").read_text(), "user working a\n")
        self.assertEqual(self.index(), index)
        patch = Path(result["patch"]).read_text()
        self.assertIn("-user working a", patch)
        self.assertNotIn("staged a", patch)
        self.invoke("apply", "--run", result["run_id"])
        self.assertEqual((self.source / "a.txt").read_text(), "worker a\n")
        self.assertEqual(self.index(), index)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.invoke("apply", "--run", result["run_id"])

    def test_checks_are_executed_and_logged(self):
        check = [sys.executable, "-c", "from pathlib import Path; assert Path('a.txt').read_text() == 'worker a\\n'; print('verified')"]
        result = self.run_task(self.task(checks=[check]))
        self.assertEqual(result["checks"][0]["exit_code"], 0)
        self.assertIn("verified", Path(result["checks"][0]["log"]).read_text())

    def test_failed_check_resumes_exact_session_and_stops_after_two_attempts(self):
        events = self.base / "events.jsonl"
        check = [sys.executable, "-c", "from pathlib import Path; assert Path('a.txt').read_text() == 'fixed\\n'"]
        result = self.run_task(self.task(action={"record": str(events), "writes": {"a.txt": "broken\n"}}, checks=[check]), code=1)
        self.assertEqual(result["status"], "validation_failed")
        correction = self.base / "repair.txt"
        correction.write_text(json.dumps({"record": str(events), "writes": {"a.txt": "fixed\n"}}))
        fixed = self.invoke("resume", "--run", result["run_id"], "--prompt-file", correction)
        self.assertEqual(fixed["session_id"], result["session_id"])
        self.assertEqual(fixed["attempts"], 2)
        starts = [json.loads(line) for line in events.read_text().splitlines() if json.loads(line)["event"] == "start"]
        self.assertIn("--resume", starts[1]["argv"])
        self.assertEqual(starts[1]["session"], starts[0]["session"])
        self.invoke("resume", "--run", result["run_id"], "--prompt-file", correction, code=2)

    def test_parallel_workers_are_isolated_and_concurrent(self):
        events = self.base / "events.jsonl"
        tasks = [self.task(name=letter, writes=[letter + ".txt"], action={"sleep": 0.4, "record": str(events),
                 "writes": {letter + ".txt": "changed " + letter}}) for letter in ("a", "b")]
        result = self.invoke("batch", "--manifest", self.file("batch.json", {"tasks": tasks}), "--jobs", "2")
        self.assertEqual(result["mode"], "isolated")
        entries = [json.loads(line) for line in events.read_text().splitlines()]
        self.assertEqual([entry["event"] for entry in entries[:2]], ["start", "start"])
        self.assertEqual(len({run["workdir"] for run in result["runs"]}), 2)
        for run in result["runs"]:
            self.invoke("apply", "--run", run["run_id"])
        self.assertEqual((self.source / "a.txt").read_text(), "changed a")
        self.assertEqual((self.source / "b.txt").read_text(), "changed b")

    def test_jobs_one_serializes_independent_workers(self):
        events = self.base / "events.jsonl"
        tasks = [self.task(name=letter, writes=[letter + ".txt"], action={"record": str(events), "sleep": 0.1,
                  "writes": {letter + ".txt": letter}}) for letter in ("a", "b")]
        self.invoke("batch", "--manifest", self.file("batch.json", {"tasks": tasks}), "--jobs", "1")
        self.assertEqual([json.loads(line)["event"] for line in events.read_text().splitlines()], ["start", "end", "start", "end"])

    def test_failed_parallel_sibling_does_not_discard_success(self):
        tasks = [self.task("good"), self.task("bad", writes=["b.txt"], action={"fail": True})]
        result = self.invoke("batch", "--manifest", self.file("batch.json", {"tasks": tasks}), code=1)
        self.assertEqual(result["status"], "batch_failed")
        self.assertEqual(result["runs"][1]["status"], "worker_failed")
        self.invoke("apply", "--run", result["runs"][0]["run_id"])

    def test_overlapping_and_dependent_batches_rejected_before_dispatch(self):
        for other in (self.task("other"), self.task("other", writes=["b.txt"], reads=["a.txt"])):
            result = self.invoke("batch", "--manifest", self.file("batch.json", {"tasks": [self.task(), other]}), code=2)
            self.assertIn("overlapping", result["error"])
        self.assertFalse(any((self.base / "state").glob("edit-*")))

    def test_out_of_scope_changes_block_apply(self):
        result = self.run_task(self.task(action={"writes": {"a.txt": "allowed", "rogue.txt": "not allowed"}}), code=1)
        self.assertEqual(result["status"], "scope_violation")
        self.assertEqual(result["out_of_scope"], ["rogue.txt"])
        self.invoke("apply", "--run", result["run_id"], code=2)
        self.assertFalse((self.source / "rogue.txt").exists())

    def test_check_generated_source_edits_are_scope_checked(self):
        check = [sys.executable, "-c", "from pathlib import Path; Path('rogue.txt').write_text('oops')"]
        result = self.run_task(self.task(checks=[check]), code=1)
        self.assertEqual(result["status"], "scope_violation")

    def test_source_conflict_preserves_all_source_files_and_index(self):
        result = self.run_task()
        (self.source / "a.txt").write_text("new user edit\n")
        index = self.index()
        self.invoke("apply", "--run", result["run_id"], code=2)
        self.assertEqual((self.source / "a.txt").read_text(), "new user edit\n")
        self.assertEqual(self.index(), index)

    def test_changed_reference_blocks_apply(self):
        result = self.run_task(self.task(reads=["b.txt"]))
        (self.source / "b.txt").write_text("changed API\n")
        self.invoke("apply", "--run", result["run_id"], code=2)
        self.assertEqual((self.source / "a.txt").read_text(), "original a\n")

    def test_post_validation_worker_edit_and_patch_tampering_are_rejected(self):
        result = self.run_task()
        path = Path(result["workdir"]) / "a.txt"
        path.write_text("unverified\n")
        self.invoke("apply", "--run", result["run_id"], code=2)
        path.write_text("worker a\n")
        Path(result["patch"]).write_text("tampered patch")
        self.invoke("apply", "--run", result["run_id"], code=2)

    def test_add_delete_and_executable_mode_apply(self):
        action = {"writes": {"new [file].txt": "new\n", "a.txt": "#!/bin/sh\n"}, "delete": ["b.txt"], "chmod": ["a.txt"]}
        result = self.run_task(self.task(action=action, writes=["new [file].txt", "a.txt", "b.txt"]))
        index = self.index()
        self.invoke("apply", "--run", result["run_id"])
        self.assertEqual((self.source / "new [file].txt").read_text(), "new\n")
        self.assertFalse((self.source / "b.txt").exists())
        self.assertTrue((self.source / "a.txt").stat().st_mode & 0o100)
        self.assertEqual(index, self.index())

    def test_timeout_kills_worker_and_descendants(self):
        marker = self.base / "surviving-child.txt"
        result = self.run_task(self.task(action={"child_marker": str(marker), "sleep": 10}), code=1, options=("--timeout", "0.4"))
        self.assertEqual(result["status"], "timed_out")
        time.sleep(1.6)
        self.assertFalse(marker.exists())

    def test_check_timeout_is_recorded_separately(self):
        result = self.run_task(self.task(checks=[[sys.executable, "-c", "import time; time.sleep(10)"]]), code=1, options=("--check-timeout", "0.1"))
        self.assertEqual(result["status"], "validation_failed")
        self.assertEqual(result["checks"][0]["status"], "timed_out")

    def test_malformed_output_and_error_envelopes_fail(self):
        for action in ({"malformed": True}, {"is_error": True}, {"omit_fields": ["result"]}, {"omit_fields": ["session_id"]}, {"omit_fields": ["type"]}):
            result = self.run_task(self.task(action=action), code=1)
            self.assertEqual(result["status"], "worker_failed")

    def test_cleanup_retains_artifacts_and_prevents_apply(self):
        result = self.run_task()
        self.invoke("cleanup", "--run", result["run_id"])
        self.assertFalse(Path(result["workdir"]).exists())
        self.assertTrue(Path(result["patch"]).exists())
        self.invoke("cleanup", "--run", result["run_id"])
        self.invoke("apply", "--run", result["run_id"], code=2)

    def test_in_place_and_legacy_usage(self):
        result = self.run_task(isolated=False)
        self.assertFalse(result["isolated"])
        self.assertEqual((self.source / "a.txt").read_text(), "worker a\n")
        self.invoke("apply", "--run", result["run_id"], code=2)
        output = subprocess.run([str(RUNNER), "legacy contents", str(self.source)], cwd=self.base, env=self.env, capture_output=True, text=True)
        self.assertEqual(output.returncode, 0, output.stderr)
        self.assertIn("Run: legacy-", output.stdout)
        self.assertEqual((self.source / "legacy.txt").read_text(), "legacy contents")

    def test_initial_repository_falls_back_without_creating_head_or_index(self):
        initial = self.base / "initial"
        initial.mkdir()
        subprocess.run(["git", "init", "-q", str(initial)], env=self.env, check=True)
        tasks = [self.task("a", action={"writes": {"a.txt": "a"}}), self.task("b", writes=["b.txt"], action={"writes": {"b.txt": "b"}})]
        result = self.invoke("batch", "--manifest", self.file("initial.json", {"tasks": tasks}), "--workdir", initial)
        self.assertEqual(result["mode"], "in_place_fallback")
        self.assertTrue(all(run.get("fallback_reason") for run in result["runs"]))
        head = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=initial, env=self.env, capture_output=True)
        self.assertNotEqual(head.returncode, 0)
        self.assertFalse((initial / ".git" / "index").exists())

    def test_non_git_directory_falls_back(self):
        plain = self.base / "plain"
        plain.mkdir()
        result = self.invoke("run", "--task", self.file("plain.json", self.task()), "--workdir", plain, "--isolated")
        self.assertFalse(result["isolated"])
        self.assertEqual((plain / "a.txt").read_text(), "worker a\n")

    def test_invalid_task_paths_and_symlink_escapes_rejected(self):
        for path in ("../escape", "/absolute", ".git/config", "./a.txt", "cache/secret.txt"):
            result = self.run_task(self.task(writes=[path]), code=2)
            self.assertEqual(result["status"], "error")
        (self.source / "alias").symlink_to(self.base, target_is_directory=True)
        self.run_task(self.task(writes=["alias/escape"]), code=2)

    def test_output_symlink_escape_is_a_scope_violation(self):
        result = self.run_task(self.task(action={"symlinks": {"a.txt": str(self.base / "outside")}}), code=1)
        self.assertEqual(result["status"], "scope_violation")
        self.invoke("apply", "--run", result["run_id"], code=2)

    def test_missing_cli_capability_does_not_bypass_permissions(self):
        self.env["FAKE_HELP"] = "--output-format"
        result = self.invoke("doctor", code=2)
        self.assertIn("required flags", result["error"])

    def test_doctor_does_not_print_credentials_or_call_worker(self):
        self.env["ANTHROPIC_API_KEY"] = "fake-private-key"
        result = self.invoke("doctor")
        self.assertNotIn("fake-private-key", json.dumps(result))
        self.assertIn("ANTHROPIC_API_KEY", result["credential_variables_present"])
        self.assertEqual(result["api"]["status"], "skipped")

    def test_doctor_reads_endpoint_and_credentials_from_claude_settings(self):
        config = self.base / "claude-config"
        config.mkdir()
        (config / "settings.json").write_text(json.dumps({"env": {
            "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
            "ANTHROPIC_AUTH_TOKEN": "fake-private-token"}}))
        self.env["CLAUDE_CONFIG_DIR"] = str(config)
        self.env.pop("ANTHROPIC_BASE_URL", None)
        result = self.invoke("doctor")
        self.assertEqual(result["api"]["status"], "skipped")
        self.assertEqual(result["api"]["host"], "api.deepseek.com")
        self.assertEqual(result["api"]["endpoint_source"], str(config / "settings.json"))
        self.assertIn("ANTHROPIC_AUTH_TOKEN", result["credential_variables_present"])
        self.assertNotIn("fake-private-token", json.dumps(result))

    def test_doctor_skips_probe_for_cloud_providers(self):
        self.env.pop("CC_WORKER_SKIP_API_CHECK")
        self.env["CLAUDE_CODE_USE_BEDROCK"] = "1"
        result = self.invoke("doctor")
        self.assertEqual(result["api"]["status"], "skipped")
        self.assertIn("CLAUDE_CODE_USE_BEDROCK", result["api"]["detail"])

    def test_unreachable_api_fails_fast_without_consuming_attempt(self):
        events = self.base / "events.jsonl"
        self.env.pop("CC_WORKER_SKIP_API_CHECK")
        self.env["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:1/anthropic"
        result = self.run_task(self.task(action={"record": str(events), "writes": {"a.txt": "should not run\n"}}), code=1)
        self.assertEqual(result["status"], "worker_failed")
        self.assertEqual(result["attempts"], 0)
        self.assertEqual(result["api_check"]["status"], "unreachable")
        self.assertIn("Cannot reach the Claude model API", result["error"])
        self.assertIn("sandbox_permissions", result["error"])
        self.assertFalse(events.exists())
        self.assertEqual((self.source / "a.txt").read_text(), "original a\n")
        self.assertFalse(list((self.base / "state").glob("edit-*/attempt-*")))

    def test_resume_after_preflight_failure_requests_new_run(self):
        self.env.pop("CC_WORKER_SKIP_API_CHECK")
        self.env["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:1/anthropic"
        result = self.run_task(code=1)
        correction = self.base / "correction.txt"
        correction.write_text("Fix it")
        resumed = self.invoke("resume", "--run", result["run_id"], "--prompt-file", correction, code=2)
        self.assertIn("dispatch a new run", resumed["error"])

    def test_worker_api_error_is_surfaced_with_sandbox_hint(self):
        action = {"is_error": True, "api_error": "connection_failed", "terminal_reason": "api_error",
                  "summary": "API Error: Can't reach the API server — check your internet or DNS (ENOTFOUND)"}
        result = self.run_task(self.task(action=action), code=1)
        self.assertEqual(result["status"], "worker_failed")
        self.assertIn("connection_failed", result["error"])
        self.assertIn("ENOTFOUND", result["error"])
        self.assertIn("sandbox_permissions", result["error"])

    def test_installer_is_idempotent_and_protects_existing_paths(self):
        target = self.base / "skills with spaces"
        argv = [str(REPO / "install.sh"), "--target", str(target)]
        for _ in range(2):
            installed = subprocess.run(argv, env=self.env, capture_output=True, text=True)
            self.assertEqual(installed.returncode, 0, installed.stderr)
        self.assertEqual((target / "cc-worker").resolve(), REPO / "cc-worker")
        checkout_alias = self.base / "checkout-alias"
        checkout_alias.symlink_to(REPO, target_is_directory=True)
        installed = subprocess.run([str(checkout_alias / "install.sh"), "--target", str(target)], env=self.env, capture_output=True, text=True)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        (target / "cc-worker").unlink()
        (target / "cc-worker").mkdir()
        (target / "cc-worker" / "keep").write_text("user data")
        refused = subprocess.run(argv, env=self.env, capture_output=True, text=True)
        self.assertNotEqual(refused.returncode, 0)
        self.assertEqual((target / "cc-worker" / "keep").read_text(), "user data")

    def test_binary_patch_round_trip(self):
        (self.source / "asset.bin").write_bytes(bytes(range(256)))
        self.git("add", "asset.bin")
        self.git("commit", "-qm", "Binary fixture")
        result = self.run_task(self.task(action={"binary_writes": {"asset.bin": [0, 255, 128, 0, 10]}}, writes=["asset.bin"]))
        self.assertIn("GIT binary patch", Path(result["patch"]).read_text())
        self.invoke("apply", "--run", result["run_id"])
        self.assertEqual((self.source / "asset.bin").read_bytes(), bytes([0, 255, 128, 0, 10]))

    def test_tracked_ignored_files_are_preserved_and_editable(self):
        (self.source / ".env").write_text("tracked fixture, not a real secret")
        self.git("add", "--force", ".env")
        self.git("commit", "-qm", "Tracked ignored fixture")
        result = self.run_task(self.task(action={"writes": {".env": "updated fixture"}}, writes=[".env"]))
        self.invoke("apply", "--run", result["run_id"])
        self.assertEqual((self.source / ".env").read_text(), "updated fixture")

    def test_preexisting_untracked_output_only_exports_increment(self):
        (self.source / "new.txt").write_text("user's untracked content\n")
        result = self.run_task(self.task(action={"writes": {"new.txt": "worker content\n"}}, writes=["new.txt"]))
        self.assertIn("-user's untracked content", Path(result["patch"]).read_text())
        self.invoke("apply", "--run", result["run_id"])
        self.assertEqual((self.source / "new.txt").read_text(), "worker content\n")
        self.assertIn("?? new.txt", self.git("status", "--short").decode())

    def test_missing_check_executable_is_not_success(self):
        result = self.run_task(self.task(checks=[["cc-worker-nonexistent-check-command"]]), code=1)
        self.assertEqual(result["status"], "validation_failed")
        self.assertEqual(result["checks"][0]["status"], "launch_failed")

    def test_resume_rejects_post_validation_changes(self):
        result = self.run_task()
        (Path(result["workdir"]) / "a.txt").write_text("manual change\n")
        correction = self.base / "repair.txt"
        correction.write_text("Fix the task")
        self.invoke("resume", "--run", result["run_id"], "--prompt-file", correction, code=2)

    def test_scope_violation_can_be_corrected_in_exact_session(self):
        result = self.run_task(self.task(action={"writes": {"a.txt": "good", "rogue.txt": "bad"}}), code=1)
        correction = self.base / "repair.txt"
        correction.write_text(json.dumps({"delete": ["rogue.txt"]}))
        fixed = self.invoke("resume", "--run", result["run_id"], "--prompt-file", correction)
        self.assertEqual(fixed["out_of_scope"], [])
        self.invoke("apply", "--run", result["run_id"])

    def test_interrupt_records_cancellation_and_stops_worker(self):
        events = self.base / "events.jsonl"
        task_file = self.file("interrupt.json", self.task(action={"record": str(events), "sleep": 10}))
        process = subprocess.Popen([str(RUNNER), "run", "--task", task_file, "--isolated"], cwd=self.source,
                                   env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 10
            while not events.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(events.exists(), "Worker did not start")
            process.send_signal(signal.SIGINT)
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 130, stdout + stderr)
            self.assertEqual(json.loads(stdout)["status"], "cancelled")
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_concurrent_operations_cannot_cleanup_running_worker(self):
        events = self.base / "events.jsonl"
        task_file = self.file("locked.json", self.task(action={"record": str(events), "sleep": 0.7}))
        process = subprocess.Popen([str(RUNNER), "run", "--task", task_file, "--isolated"], cwd=self.source,
                                   env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 10
            while not events.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(events.exists())
            run_id = next((self.base / "state").glob("edit-*/result.json")).parent.name
            self.invoke("cleanup", "--run", run_id, code=2)
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stdout + stderr)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_task_validation_rejects_command_strings_and_unknown_fields(self):
        invalid = self.task()
        invalid["checks"] = ["echo unsafe implicit shell"]
        self.run_task(invalid, code=2)
        invalid = self.task()
        invalid["typo"] = True
        self.run_task(invalid, code=2)

    def test_subdirectory_workdir_uses_repository_relative_paths(self):
        subdir = self.source / "src"
        subdir.mkdir()
        result = self.invoke("run", "--task", self.file("nested.json", self.task()), "--workdir", subdir, "--isolated")
        self.assertEqual((Path(result["workdir"]) / "a.txt").read_text(), "worker a\n")

    def test_crlf_worktree_patch_applies(self):
        (self.source / ".gitattributes").write_text("*.txt text eol=crlf\n")
        (self.source / "a.txt").write_bytes(b"original a\r\n")
        self.git("add", ".gitattributes", "a.txt")
        self.git("commit", "-qm", "CRLF fixture")
        result = self.run_task(self.task(action={"writes": {"a.txt": "changed\r\n"}}))
        self.invoke("apply", "--run", result["run_id"])
        self.assertEqual((self.source / "a.txt").read_bytes(), b"changed\r\n")

    def test_finished_check_cannot_leave_background_descendants(self):
        marker = self.base / "background-check.txt"
        child = "import time; from pathlib import Path; time.sleep(1.5); Path(" + repr(str(marker)) + ").write_text('survived')"
        check = [sys.executable, "-c", "import subprocess,sys; subprocess.Popen([sys.executable, '-c', " + repr(child) + "])" ]
        self.run_task(self.task(checks=[check]))
        time.sleep(1.6)
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
