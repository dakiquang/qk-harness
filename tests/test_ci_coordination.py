"""CI retry/deadline/claim contracts; only GitHub transport is a fixture."""

import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest import mock

from test_delegation import DelegationSandbox
from harness import capabilities, coordinator, delegation, gitops, publication, state


class CICoordinationTests(DelegationSandbox):
    def ready(self):
        job_id = self.new("large")
        self.grant(job_id)
        with mock.patch.object(publication, "CI_TIMEOUT", 0):
            reply = coordinator.run(job_id, self.qk)
        self.assertEqual(reply["stopped"], "blocked", reply)
        self.assertTrue(state.load(job_id)["pr_url"])
        return job_id

    def test_one_read_retries_once_then_accepts_fresh_head_without_model_work(self):
        job_id = self.ready()
        turns = len(state.load(job_id)["usage"])
        original, calls = gitops.gh, []
        def transport(cwd, *args, **kwargs):
            if args[:2] == ("pr", "view"):
                calls.append(args)
                if len(calls) == 1:
                    raise gitops.GitError("read timeout", operation="gh pr", timed_out=True, effect="read")
            return original(cwd, *args, **kwargs)
        with mock.patch.object(gitops, "gh", side_effect=transport):
            reply = self.run_cap(job_id, "pr.check")
        self.assertEqual(reply["status"], "done", reply)
        attempts = reply["ci"]["attempts"]
        first = [a for a in attempts if a["request_id"] == attempts[0]["request_id"]]
        self.assertEqual([(a["attempt"], a["status"]) for a in first], [(1, "timeout"), (2, "done")])
        self.assertEqual(len({a["deadline_monotonic"] for a in attempts}), 1)
        self.assertGreaterEqual(first[0]["remaining_s"], first[1]["remaining_s"])
        self.assertEqual(len(calls), 3)  # logical retry plus a distinct final head/check confirmation
        self.assertEqual(len(state.load(job_id)["usage"]), turns)
        self.assertTrue(list((state.job_dir(job_id) / "diagnostics").glob("*.json")))

    def test_two_timeouts_block_without_a_third_attempt(self):
        job_id = self.ready()
        original, calls = gitops.gh, []
        def transport(cwd, *args, **kwargs):
            if args[:2] == ("pr", "view"):
                calls.append(args)
                raise gitops.GitError("read timeout", timed_out=True, effect="read")
            return original(cwd, *args, **kwargs)
        with mock.patch.object(gitops, "gh", side_effect=transport):
            reply = self.run_cap(job_id, "pr.check")
        self.assertEqual((reply["status"], len(calls)), ("blocked", 2))
        self.assertEqual([a["attempt"] for a in state.load(job_id)["ci"]["attempts"]], [1, 2])
        self.assertNotIn("ci_check", state.load(job_id))

    def test_auth_failure_and_unconfirmed_cleanup_never_retry(self):
        for operation in ("auth", "cleanup"):
            with self.subTest(operation=operation):
                job_id = self.ready()
                original, calls = gitops.gh, []
                def transport(cwd, *args, **kwargs):
                    if args[:2] == (("auth", "token") if operation == "auth" else ("pr", "view")):
                        calls.append(args)
                        raise gitops.GitError("unavailable", effect="read", timed_out=operation == "cleanup",
                                              cleanup_known=operation != "cleanup")
                    return original(cwd, *args, **kwargs)
                with mock.patch.object(gitops, "gh", side_effect=transport):
                    reply = self.run_cap(job_id, "pr.check")
                self.assertEqual((reply["status"], len(calls)), ("blocked", 1))
                if operation == "cleanup":
                    self.assertIn("recovery_required", state.load(job_id))
                    self.assertEqual(self.run_cap(job_id, "pr.check")["status"], "refused")

    def test_deadline_is_not_reset_to_retry_after_first_timeout(self):
        job_id = self.ready()
        job = state.load(job_id)
        clock = [0]
        def read(*args, **kwargs):
            clock[0] = 100
            raise gitops.GitError("timeout", effect="read", timed_out=True)
        fake_time = SimpleNamespace(monotonic=lambda: clock[0], strftime=time.strftime, sleep=lambda _: None)
        with mock.patch.object(publication, "environment", return_value={}), \
                mock.patch.object(publication, "time", fake_time), \
                mock.patch.object(gitops, "gh", side_effect=read) as call:
            with self.assertRaisesRegex(delegation.GrantError, "deadline"):
                publication.check_ci(job, deadline=100)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(job["ci"]["attempts"][0]["attempt"], 1)

    def test_revoke_and_concurrent_run_finish_while_native_ci_read_is_pending(self):
        job_id = self.ready()
        entered, release = self.tmp / "entered", self.tmp / "release"
        gh = self.tmp / "bin" / "gh"
        source = gh.read_text().replace("elif command == 'pr view':", """elif command == 'pr view':
    import time
    if os.environ.get('CI_ENTERED'):
        Path(os.environ['CI_ENTERED']).touch()
        end = time.monotonic() + 15
        while not Path(os.environ['CI_RELEASE']).exists():
            if time.monotonic() >= end:
                sys.exit(3)
            time.sleep(0.02)
""")
        gh.write_text(source)
        env = dict(os.environ, CI_ENTERED=str(entered), CI_RELEASE=str(release))
        worker = subprocess.Popen([sys.executable, "-m", "harness", "--lock", str(self.lock), "pr.check", job_id],
                                  cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, env=env, start_new_session=True)
        try:
            end = time.monotonic() + 10
            while not entered.exists():
                if worker.poll() is not None:
                    self.fail("worker ended before entering the CI read: " + worker.communicate()[1])
                self.assertLess(time.monotonic(), end, "CI worker did not reach the barrier")
                time.sleep(0.02)
            claim = state.load(job_id)["ci_check"]
            listing = json.loads(self.invoke("status", "--json").stdout)
            self.assertIn("pr.check", listing["items"][0]["running"])
            concurrent = self.invoke("run", job_id)
            self.assertEqual(json.loads(concurrent.stdout)["stopped"], "waiting", concurrent.stderr)
            self.assertEqual(state.load(job_id)["ci_check"], claim)
            revoked = self.invoke("revoke", job_id, "--reason", "stop")
            self.assertEqual(revoked.returncode, 0, revoked.stderr)
            self.assertFalse(release.exists())
            self.assertTrue(state.load(job_id)["delegation"]["revoked"])
            release.touch()
            out, err = worker.communicate(timeout=10)
            self.assertEqual(worker.returncode, 2, out + err)
            self.assertEqual(json.loads(out)["status"], "blocked")
            self.assertNotEqual(state.load(job_id)["ci"]["status"], "success")
        finally:
            release.touch()
            if worker.poll() is None:
                worker.terminate()
                try:
                    worker.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    worker.kill()
                    worker.communicate(timeout=5)

    def test_stale_result_cannot_overwrite_changed_head_abort_or_claim(self):
        for change in ("head", "abort", "claim"):
            with self.subTest(change=change):
                job_id = self.ready()
                def delayed(work, **kwargs):
                    with state.locked(job_id) as live:
                        if change == "head":
                            live["integration"]["commit"] = "f" * 40
                        elif change == "abort":
                            live["aborted"] = {"reason": "stop"}
                        else:
                            live["ci_check"]["id"] = "another-owner"
                    work["ci"] = {"status": "success", "head": work["integration"]["commit"]}
                    return {"ci": work["ci"]}
                with mock.patch.object(publication, "check_ci", side_effect=delayed):
                    reply = self.run_cap(job_id, "pr.check")
                self.assertIn(reply["status"], ("refused", "blocked"))
                self.assertNotEqual(state.load(job_id)["ci"]["status"], "success")
                if change == "claim":
                    self.assertEqual(state.load(job_id)["ci_check"]["id"], "another-owner")
