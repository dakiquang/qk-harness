"""The harness runs only the locked qk, checked before use, and calls it through the CLI contract."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fake_qk  # noqa: E402
from harness import qk_cli  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]


class QkCliTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.tmp = Path(directory.name).resolve()
        self.upstream = self.tmp / "upstream"
        self.sha = fake_qk.make_repo(self.upstream)
        self.lock = {"repo": str(self.upstream), "tag": "v9.9.9", "sha": self.sha, "contract": 1}
        self.state = self.tmp / "state"
        self.log = self.tmp / "calls.jsonl"
        settings = mock.patch.dict(os.environ, {"FAKE_QK_LOG": str(self.log)})
        settings.start()
        self.addCleanup(settings.stop)

    def write_lock(self, **changes):
        path = self.tmp / "qk.lock"
        path.write_text(json.dumps(dict(self.lock, **changes)))
        return path

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def open(self, **settings):
        with mock.patch.dict(os.environ, settings):
            return qk_cli.open_locked(self.write_lock(), self.state)

    # --- qk.lock

    def test_lock_needs_exact_fields_a_full_sha_and_contract_1(self):
        self.assertEqual(qk_cli.load_lock(self.write_lock()), self.lock)
        for changes in ({"sha": "abc123"}, {"contract": 2}, {"contract": "1"}, {"tag": ""}, {"extra": 1}):
            with self.subTest(changes=changes):
                with self.assertRaises(qk_cli.QkError):
                    qk_cli.load_lock(self.write_lock(**changes))
        (self.tmp / "broken.lock").write_text("{")
        with self.assertRaises(qk_cli.QkError):
            qk_cli.load_lock(self.tmp / "broken.lock")

    def test_repository_lock_pins_a_full_sha_with_contract_1(self):
        lock = qk_cli.load_lock(ROOT / "qk.lock")
        self.assertEqual((lock["repo"], lock["contract"]), ("https://github.com/dakiquang/qk.git", 1))

    # --- fetching the locked qk

    def test_fetch_checks_out_the_locked_tag_once(self):
        target = qk_cli.fetch(self.lock, self.state)
        self.assertEqual(target, self.state / "qk" / self.sha)
        self.assertEqual(qk_cli._git(target, "rev-parse", "HEAD"), self.sha)
        self.assertEqual(qk_cli.fetch(self.lock, self.state), target)
        self.assertEqual(sorted(path.name for path in target.parent.iterdir()), [self.sha])

    def test_fetch_refuses_a_tag_that_points_elsewhere(self):
        identity = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]
        (self.upstream / "scripts" / "extra.txt").write_text("moved\n")
        subprocess.run(["git", "add", "-A"], cwd=str(self.upstream), check=True)
        subprocess.run(["git", *identity, "commit", "-qm", "later"], cwd=str(self.upstream), check=True)
        subprocess.run(["git", "tag", "-f", "v9.9.9"], cwd=str(self.upstream), check=True, capture_output=True)
        with self.assertRaisesRegex(qk_cli.QkError, "tag v9.9.9 .* qk.lock says"):
            qk_cli.fetch(self.lock, self.state)
        self.assertEqual(list((self.state / "qk").iterdir()), [])

    def test_fetch_refuses_a_checkout_that_moved_or_changed(self):
        target = qk_cli.fetch(self.lock, self.state)
        (target / "scripts" / "qk.py").write_text("print('changed')\n")
        with self.assertRaisesRegex(qk_cli.QkError, "local changes"):
            qk_cli.fetch(self.lock, self.state)

    def test_fetch_reports_a_missing_tag(self):
        with self.assertRaisesRegex(qk_cli.QkError, "git fetch"):
            qk_cli.fetch(dict(self.lock, tag="v0.0.0"), self.state)

    # --- the contract check at start-up

    def test_open_checks_the_contract_once(self):
        qk = self.open()
        self.assertEqual(qk.version, "9.9.9")
        self.assertEqual([call["argv"] for call in self.calls()], [["contract", "--json"]])

    def test_other_contract_versions_are_refused(self):
        for version in ("0", "2"):
            with self.subTest(version=version):
                with self.assertRaisesRegex(qk_cli.QkError, "reports contract %s" % version):
                    self.open(FAKE_QK_CONTRACT=version)

    def test_a_missing_contract_command_is_refused(self):
        with self.assertRaisesRegex(qk_cli.QkError, "lacks contract commands: log run"):
            self.open(FAKE_QK_DROP="log run")

    def test_a_checkout_at_another_commit_is_refused(self):
        target = qk_cli.fetch(self.lock, self.state)
        with self.assertRaisesRegex(qk_cli.QkError, "qk.lock says " + "0" * 12):
            qk_cli.Qk(target, dict(self.lock, sha="0" * 40))
        self.assertEqual(self.calls(), [])  # refused before qk ran

    # --- calls through the contract

    def test_agent_run_passes_the_documented_flags_and_the_prompt(self):
        qk = self.open()
        result = qk.agent_run("codex:", self.tmp, "Do it.", write=False, add_dirs=[self.tmp / "extra"],
                              timeout=60, read_only_mcp=["tracker"], lessons="review", repo="project",
                              events=self.tmp / "events.jsonl")
        self.assertEqual((result.code, result.data["status"], result.data["family"]), (0, "done", "openai"))
        call = self.calls()[-1]
        self.assertEqual(call["stdin"], "Do it.")
        self.assertEqual(call["argv"], ["agent", "run", "--spec", "codex:", "--cwd", str(self.tmp), "--read-only",
                                        "--timeout", "60", "--json", "--add-dir", str(self.tmp / "extra"),
                                        "--read-only-mcp", "tracker", "--lessons", "review", "--repo", "project",
                                        "--events", str(self.tmp / "events.jsonl")])
        qk.agent_run("claude:", self.tmp, "Edit.", write=True, result_contract=False, git_rules=False)
        argv = self.calls()[-1]["argv"]
        self.assertIn("--write", argv)
        self.assertEqual(argv[-2:], ["--no-result-contract", "--no-git-rules"])

    def test_results_keep_exit_codes_and_json(self):
        qk = self.open()
        for scenario, code, status in (("done", 0, "done"), ("blocked", 2, "blocked")):
            with self.subTest(scenario=scenario), mock.patch.dict(os.environ, {"FAKE_QK_SCENARIO": scenario}):
                result = qk.agent_run("codex:", self.tmp, "x", write=False)
                self.assertEqual((result.code, result.data["status"]), (code, status))
        with mock.patch.dict(os.environ, {"FAKE_QK_SCENARIO": "error"}):
            result = qk.agent_run("codex:", self.tmp, "x", write=False)
        self.assertEqual((result.code, result.data), (1, None))
        self.assertIn("fake failure", result.stderr)

    def test_log_run_sends_the_state_as_json(self):
        qk = self.open()
        result = qk.log_run({"id": "job-1", "stage": "done", "repo": "/work/project", "findings": [{}]},
                            "qk-harness 0.0.0", self.tmp)
        self.assertEqual(result.data, {"contract": 1, "id": "fake-job-1", "findings": 1})
        call = self.calls()[-1]
        self.assertEqual(call["argv"], ["log", "run", "--producer", "qk-harness 0.0.0", "--state-dir",
                                        str(self.tmp), "--json"])
        self.assertEqual(json.loads(call["stdin"])["id"], "job-1")

    def test_family_eval_and_show_calls(self):
        qk = self.open()
        self.assertEqual(qk.agent_family("claude:").data["family"], "anthropic")
        prepared = qk.eval_prepare("csv-export", self.tmp / "case").data
        self.assertTrue(Path(prepared["workdir"]).is_dir())
        graded = qk.eval_grade("csv-export", prepared["workdir"], final=self.tmp / "f.md", events=self.tmp / "e")
        self.assertEqual((graded.code, graded.data["passed"]), (0, False))  # nothing was built in the case repo
        self.assertEqual(self.calls()[-1]["argv"][-4:], ["--final", str(self.tmp / "f.md"), "--events",
                                                         str(self.tmp / "e")])
        self.assertEqual(qk.show(kind="run").data, [])

    def test_a_call_past_its_timeout_gets_sigterm(self):
        qk = self.open()
        pid_file = self.tmp / "qk.pid"
        with mock.patch.object(qk_cli, "EXTRA_SECONDS", 0), \
                mock.patch.dict(os.environ, {"FAKE_QK_SLEEP": "30", "FAKE_QK_PID": str(pid_file)}):
            started = time.monotonic()
            with self.assertRaisesRegex(qk_cli.QkError, "did not finish within 1s"):
                qk.agent_run("codex:", self.tmp, "x", write=False, timeout=1)
        self.assertLess(time.monotonic() - started, 15)
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)

    # --- the doctor command

    def test_doctor_reports_the_locked_qk_or_why_it_is_refused(self):
        lock = self.write_lock()
        env = dict(os.environ, QK_HARNESS_STATE_DIR=str(self.state))
        ok = subprocess.run([sys.executable, "-m", "harness", "--lock", str(lock), "doctor"], cwd=str(ROOT),
                            capture_output=True, text=True, env=env)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(json.loads(ok.stdout)["qk"], "9.9.9")
        after = subprocess.run([sys.executable, "-m", "harness", "doctor", "--lock", str(lock)], cwd=str(ROOT),
                               capture_output=True, text=True, env=env)
        self.assertEqual(after.returncode, 0, after.stderr)  # --lock also works after the command
        refused = subprocess.run([sys.executable, "-m", "harness", "--lock", str(lock), "doctor"], cwd=str(ROOT),
                                 capture_output=True, text=True, env=dict(env, FAKE_QK_CONTRACT="2"))
        self.assertEqual((refused.returncode, refused.stdout), (2, ""))
        self.assertIn("reports contract 2", refused.stderr)


class BoundaryTests(unittest.TestCase):
    def test_only_qk_cli_runs_qk_and_only_gitops_runs_other_processes(self):
        for path in sorted((ROOT / "harness").glob("*.py")):
            if path.name == "qk_cli.py":
                continue
            with self.subTest(path=path.name):
                text = path.read_text()
                # Nothing else names qk's script or imports qk's own modules (qk, qk_run, qk_ledger, ...).
                self.assertIsNone(re.search(r"scripts/qk\.py|\bimport (qk|qk_(?!cli\b)\w+)\b", text))
                if path.name != "gitops.py":  # git, gh and the plan's checks run from gitops only
                    self.assertIsNone(re.search(r"\bsubprocess\b", text))


if __name__ == "__main__":
    unittest.main()
