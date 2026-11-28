"""Unknown execution cannot acquire fresh model/writer authority, including manual jobs."""

from unittest import mock

from test_capabilities import Sandbox
from harness import capabilities, coordinator, gitops, qk_cli, state


class RecoveryTests(Sandbox):
    def test_manual_pending_receipt_is_on_disk_before_model_and_unknown_is_not_retried(self):
        job_id = self.new()
        def interrupted(*args, **kwargs):
            pending = state.load(job_id)
            self.assertEqual(pending["usage"][-1]["status"], "running")
            self.assertIn("active_step", pending)
            raise qk_cli.QkError("supervisor disappeared")
        with mock.patch.object(self.qk, "agent_run", side_effect=interrupted) as agent:
            reply = self.run_cap(job_id, "plan.write")
        self.assertEqual(reply["status"], "blocked")
        self.assertEqual(agent.call_count, 1)
        job = state.load(job_id)
        self.assertEqual(job["usage"][-1]["status"], "unknown")
        self.assertIn("recovery_required", job)
        self.assertTrue((state.job_dir(job_id) / job["diagnostic_ref"]).is_file())
        with mock.patch.object(self.qk, "agent_run") as agent:
            self.assertEqual(coordinator.run(job_id, self.qk)["stopped"], "blocked")
            self.assertEqual(self.run_cap(job_id, "plan.write")["status"], "refused")
            agent.assert_not_called()

    def test_missing_or_malformed_done_reply_is_unknown_not_free_failed(self):
        for data in (None, {"status": "done"}, {"status": "nonsense"},
                     {"contract": 2, "status": "done", "result": {"status": "done"}},
                     {"contract": True, "status": "done", "result": {"status": "done"}}):
            with self.subTest(data=data):
                job_id = self.new()
                with mock.patch.object(self.qk, "agent_run", return_value=qk_cli.Result(0, data, "")):
                    reply = self.run_cap(job_id, "plan.write")
                self.assertEqual(reply["status"], "blocked")
                self.assertEqual(state.load(job_id)["usage"][-1]["status"], "unknown")
                self.assertIn("recovery_required", state.load(job_id))

    def test_native_write_timeout_keeps_evidence_and_blocks_direct_retry(self):
        job_id = self.new()
        timeout = gitops.GitError("write outcome unknown", operation="git push", timed_out=True, effect="write")
        with mock.patch.dict(capabilities.ACTIONS, {"plan.write": mock.Mock(side_effect=timeout)}):
            self.assertEqual(self.run_cap(job_id, "plan.write")["status"], "blocked")
        job = state.load(job_id)
        self.assertEqual(job["recovery_required"]["operation"], "git push")
        self.assertTrue((state.job_dir(job_id) / job["diagnostic_ref"]).exists())
        with mock.patch.object(self.qk, "agent_run") as agent:
            self.assertEqual(self.run_cap(job_id, "plan.write")["status"], "refused")
            agent.assert_not_called()

    def test_legacy_failed_turn_and_interrupted_serial_step_are_not_cleared_by_pid(self):
        for kind in ("failed", "step"):
            with self.subTest(kind=kind):
                job_id = self.new()
                with state.locked(job_id) as job:
                    if kind == "failed":
                        job["usage"] = [{"spec": "codex:", "status": "failed", "usage": None}]
                    else:
                        job["active_step"] = {"pid": 999999, "capability": "plan.write"}
                with mock.patch.object(self.qk, "agent_run") as agent:
                    self.assertEqual(coordinator.run(job_id, self.qk)["stopped"], "blocked")
                    agent.assert_not_called()
                if kind == "step":
                    self.assertIn("active_step", state.load(job_id))

    def test_completed_codex_unknown_usd_is_not_unknown_execution(self):
        job_id = self.new()
        reply = self.run_cap(job_id, "plan.write")
        self.assertEqual(reply["status"], "done", reply)
        job = state.load(job_id)
        self.assertTrue(job["usage"][-1]["terminal"])
        self.assertFalse(state.recovery_reason(job))
        self.assertNotIn("active_step", job)

    def test_blocked_reply_and_capsule_redact_inherited_credentials(self):
        job_id = self.new()
        value = "private-inherited-test-credential"
        with mock.patch.dict("os.environ", GH_TOKEN=value), \
                mock.patch.dict(capabilities.ACTIONS, {"plan.write": mock.Mock(side_effect=capabilities.Blocked(value))}):
            reply = self.run_cap(job_id, "plan.write")
        self.assertNotIn(value, reply["reason"])
        job = state.load(job_id)
        self.assertNotIn(value, job["log"][-1]["reason"])
        self.assertNotIn(value, (state.job_dir(job_id) / job["diagnostic_ref"]).read_text())
