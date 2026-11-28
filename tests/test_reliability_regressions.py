"""Durability, billing and interruption regressions across real job boundaries."""

import contextlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from unittest import mock

from test_delegation import DelegationSandbox
from test_capabilities import sh
from harness import __main__ as cli, capabilities, coordinator, delegation, ledger, policy, qk_cli, state


class ReliabilityRegressions(DelegationSandbox):
    def built_ready(self):
        job_id = self.new()
        self.run_cap(job_id, "plan.write")
        capabilities.approve(job_id, "plan")
        self.advance(job_id, until=f"build.run {job_id} api")
        return job_id

    def test_valid_blocked_and_needs_input_are_terminal_and_keep_known_cost(self):
        for status in ("blocked", "needs_input"):
            with self.subTest(status=status):
                job_id = self.new()
                result = qk_cli.Result(2, {"contract": 1, "status": status, "usage": {"cost_usd": 0.25}}, "stopped")
                with mock.patch.object(self.qk, "agent_run", return_value=result):
                    reply = self.run_cap(job_id, "plan.write")
                job = state.load(job_id)
                self.assertEqual(reply["status"], "blocked")
                self.assertTrue(job["usage"][-1]["terminal"])
                self.assertFalse(state.recovery_reason(job))
                self.assertEqual(delegation.costs(job)["known_usd"], 0.25)

    def test_integration_ref_lock_preserves_native_failure_metadata(self):
        job_id = self.new("medium")
        self.advance(job_id)
        capabilities.approve(job_id, "plan")
        step, _ = self.advance(job_id, until="integrate.run")
        self.assertTrue(step.startswith("integrate.run "), step)
        worktree = Path(state.load(job_id)["integration"]["worktree"])
        head = sh(worktree, "git", "rev-parse", "HEAD")
        ref = sh(worktree, "git", "symbolic-ref", "HEAD")
        refpath = Path(sh(worktree, "git", "rev-parse", "--git-path", ref))
        if not refpath.is_absolute():
            refpath = worktree / refpath
        ref_lock = Path(str(refpath) + ".lock")
        ref_lock.write_text("held by this test\n")
        reply = self.run_cap(job_id, "integrate.run")
        self.assertEqual(reply["status"], "blocked")
        self.assertEqual(sh(worktree, "git", "rev-parse", "HEAD"), head)
        self.assertTrue(ref_lock.exists())
        saved = state.load(job_id)
        capsule = json.loads((state.job_dir(job_id) / saved["diagnostic_ref"]).read_text())
        self.assertEqual(capsule["operation"], "git merge")
        self.assertIsInstance(capsule["returncode"], int)
        self.assertNotEqual(capsule["returncode"], 0)
        self.assertIn(ref_lock.name, capsule["stderr"])

    def test_unknown_execution_does_not_erase_reported_spend(self):
        job_id = self.new()
        bad = qk_cli.Result(0, {"contract": 1, "status": "done", "result": None,
                              "usage": {"cost_usd": 0.25}}, "invalid result")
        with mock.patch.object(self.qk, "agent_run", return_value=bad):
            self.assertEqual(self.run_cap(job_id, "plan.write")["status"], "blocked")
        cost = delegation.costs(state.load(job_id))
        self.assertEqual(cost["known_usd"], 0.25)
        self.assertEqual(len(cost["unresolved_turns"]), 1)

    def test_terminal_receipt_releases_its_reservation_before_worker_finalize(self):
        job_id = self.new()
        self.grant(job_id, budget=0.25)
        with state.locked(job_id) as job:
            job["tasks"] = {"a": {"running": {"events": "completed", "estimated_usd": 0.2}}}
            job["usage"] = [{"spec": "claude:", "events": "completed", "status": "done", "terminal": True,
                              "usage": {"cost_usd": 0.1}}]
            self.assertEqual(delegation.costs(job)["reserved_usd"], 0)
            delegation.check_turn(job, "claude:")  # .1 spent + .1 next fits .25; stale .2 reserve would reject
            job["tasks"]["b"] = {"running": {"estimated_usd": 0.2}}
            self.assertEqual(delegation.costs(job)["reserved_usd"], 0.2)
            with self.assertRaisesRegex(delegation.GrantError, "soft budget"):
                delegation.check_turn(job, "claude:")

    def test_real_completed_save_survives_crash_without_replaying_build(self):
        job_id = self.built_ready()
        original = state.save
        crashed = [False]
        class StopAfterSave(BaseException):
            pass
        def save(job):
            original(job)
            marker = job["tasks"].get("api", {}).get("running", {})
            if marker.get("completed") and not crashed[0]:
                crashed[0] = True
                raise StopAfterSave()
        with mock.patch.object(state, "save", side_effect=save), self.assertRaises(StopAfterSave):
            self.run_cap(job_id, "build.run", "api")
        before = state.load(job_id)
        self.assertTrue(before["tasks"]["api"]["running"]["completed"])
        with mock.patch.object(state, "owner_alive", return_value=False), \
                mock.patch.object(self.qk, "agent_run") as agent:
            self.assertEqual(ledger.recover(job_id), ["api"])
            agent.assert_not_called()
        self.assertEqual(state.load(job_id)["tasks"]["api"]["commit"], before["tasks"]["api"]["commit"])
        self.assertTrue(policy.next_step(state.load(job_id)).startswith(f"verify.run {job_id} api"))

    def test_wrong_branch_same_sha_does_not_recover_a_completed_marker(self):
        job_id = self.built_ready()
        self.run_cap(job_id, "build.run", "api")
        with state.locked(job_id) as job:
            task = job["tasks"]["api"]
            receipt = next(turn for turn in reversed(job["usage"]) if turn.get("target") == "api")
            task["running"] = {"pid": 999999, "completed": True, "events": receipt["events"]}
        sh(task["worktree"], "git", "switch", "-c", "wrong-recovery-branch")
        self.assertEqual(ledger.recover(job_id), [])
        with mock.patch.object(self.qk, "agent_run") as agent:
            self.assertEqual(coordinator.run(job_id, self.qk)["stopped"], "blocked")
            agent.assert_not_called()
        self.assertIn("running", state.load(job_id)["tasks"]["api"])

    def test_serial_and_task_interruptions_preserve_reason_and_capsule(self):
        for target in (None, "api"):
            with self.subTest(target=target):
                job_id = self.new() if target is None else self.built_ready()
                patch = mock.patch.dict(capabilities.ACTIONS, {"plan.write": mock.Mock(side_effect=KeyboardInterrupt("stop"))}) \
                    if target is None else mock.patch.object(capabilities, "build_run", side_effect=KeyboardInterrupt("stop"))
                with patch, self.assertRaises(KeyboardInterrupt):
                    self.run_cap(job_id, "plan.write" if target is None else "build.run", target)
                job = state.load(job_id)
                self.assertIn("stop", job["log"][-1]["reason"])
                self.assertEqual(job["log"][-1]["status"], "unknown")
                self.assertTrue((state.job_dir(job_id) / job["diagnostic_ref"]).is_file())
                self.assertIn("recovery_required", job)

    def test_ledger_json_error_and_revoke_log_redact_credentials(self):
        job_id = self.new()
        self.grant(job_id)
        with state.locked(job_id) as job:
            job["recovery_required"] = {"reason": "inspect"}
        secret = "private-ledger-inherited-credential"
        out = io.StringIO()
        with mock.patch.dict(os.environ, GH_TOKEN=secret), \
                mock.patch.object(self.qk, "log_run", return_value=qk_cli.Result(2, None, secret)), \
                mock.patch.object(qk_cli, "Qk", return_value=self.qk), contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--lock", str(self.lock), "run", job_id]), 2)
            delegation.revoke(job_id, secret)
        self.assertNotIn(secret, out.getvalue())
        self.assertIn("REDACTED", json.loads(out.getvalue())["ledger"]["error"])
        self.assertNotIn(secret, state.load(job_id)["log"][-1]["reason"])

    def test_parent_death_does_not_start_another_writer_while_child_is_alive(self):
        for delegated in (False, True):
            with self.subTest(delegated=delegated):
                job_id = self.new()
                if delegated:
                    self.grant(job_id)
                self.run_cap(job_id, "plan.write")
                capabilities.approve(job_id, "plan", delegated=delegated)
                self.advance(job_id, until=f"build.run {job_id} api")
                writer_pid = self.tmp / (job_id + ".writer")
                release = self.tmp / (job_id + ".release")
                calls = self.tmp / (job_id + ".calls")
                script = self.tmp / (job_id + ".agent.py")
                script.write_text("import os,time\nfrom pathlib import Path\n"
                    + "Path(" + repr(str(writer_pid)) + ").write_text(str(os.getpid()))\n"
                    + "end=time.monotonic()+20\n"
                    + "while not Path(" + repr(str(release)) + ").exists():\n"
                    + " if time.monotonic()>end: raise SystemExit(3)\n time.sleep(.02)\n"
                    + "Path('partial.txt').write_text('old writer')\n")
                env = dict(os.environ, FAKE_QK_AGENT=str(script), FAKE_QK_LOG=str(calls))
                worker = subprocess.Popen([sys.executable, "-m", "harness", "--lock", str(self.lock),
                                           "build.run", job_id, "api"], cwd=Path(__file__).resolve().parents[1],
                                          env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                          text=True, start_new_session=True)
                try:
                    end = time.monotonic() + 10
                    while not writer_pid.exists():
                        if worker.poll() is not None:
                            self.fail("runner exited before writer barrier: " + worker.communicate()[1])
                        self.assertLess(time.monotonic(), end, "writer did not reach barrier")
                        time.sleep(.02)
                    child = int(writer_pid.read_text())
                    worker.kill()  # parent only; child in the owned group is deliberately still alive
                    worker.wait(timeout=5)
                    os.kill(child, 0)
                    with mock.patch.dict(os.environ, FAKE_QK_LOG=str(calls)):
                        resumed = self.invoke("run", job_id)
                    self.assertEqual(resumed.returncode, 2, resumed.stdout + resumed.stderr)
                    self.assertEqual(json.loads(resumed.stdout)["stopped"], "blocked")
                    entries = [json.loads(line) for line in calls.read_text().splitlines()]
                    self.assertEqual(sum(entry["argv"][:2] == ["agent", "run"] for entry in entries), 1)
                    self.assertFalse(release.exists())
                    self.assertIn("running", state.load(job_id)["tasks"]["api"])
                finally:
                    release.touch()
                    try:
                        os.killpg(worker.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    if worker.poll() is None:
                        worker.wait(timeout=5)
                    worker.communicate(timeout=5)
