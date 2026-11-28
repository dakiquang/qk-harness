"""Usage limits: a turn a runtime's usage limit stopped waits for the reset instead of failing the job."""

import contextlib
import io
import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
import subprocess
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_capabilities import Sandbox  # noqa: E402
from harness import __main__ as cli, capabilities, coordinator, ledger, policy, state  # noqa: E402


def sh_status(path):
    return subprocess.run(["git", "status", "--porcelain"], cwd=str(path), capture_output=True, text=True,
                          check=True).stdout.strip()


class LimitTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.limit = self.tmp / "limit.json"
        self.qk_log = self.tmp / "qk-calls.jsonl"
        settings = mock.patch.dict(os.environ, {"FAKE_QK_LIMIT": str(self.limit), "FAKE_QK_LOG": str(self.qk_log)})
        settings.start()
        self.addCleanup(settings.stop)
        self.notices = []
        notify = mock.patch.object(coordinator, "notify", self.notices.append)
        notify.start()
        self.addCleanup(notify.stop)

    def hold(self, runtime, reset_at, **extra):
        self.limit.write_text(json.dumps(dict({"runtime": runtime, "reset_at": reset_at}, **extra)))

    def approved_job(self):
        job_id = self.new()
        coordinator.run(job_id, self.qk)
        capabilities.approve(job_id, "plan")
        return job_id

    def logged_runs(self):
        return [json.loads(json.loads(line)["stdin"]) for line in self.qk_log.read_text().splitlines()
                if json.loads(line)["argv"][:2] == ["log", "run"]]

    def test_a_limited_turn_waits_for_its_runtime_and_runs_again_when_the_job_runs_again(self):
        job_id = self.approved_job()
        today = datetime.now().astimezone().date()
        reset = (datetime.now().astimezone() + timedelta(hours=1)).replace(microsecond=0)
        self.hold("claude", reset.isoformat())
        reply = coordinator.run(job_id, self.qk)
        self.assertEqual((reply["stopped"], reply["capability"]), ("limited", "contract.review"))
        # Within an hour of midnight the reset falls tomorrow, and the wait names its date; if midnight passes
        # while the test runs, either form is right (test_a_reset_on_another_day_names_its_date pins the rule).
        forms = {f"until {reset:%H:%M}" if reset.date() == day else f"until {reset:%Y-%m-%d %H:%M}"
                 for day in (today, datetime.now().astimezone().date())}
        self.assertIn(reply["next"], {f"wait: the claude usage limit {until}, then contract.review {job_id}"
                                      for until in forms})
        job = state.load(job_id)
        self.assertEqual(job["limits"]["claude"]["reset_at"], reset.isoformat())
        self.assertEqual(job["log"][-1]["status"], "limited")
        self.assertEqual(job["usage"][-1]["status"], "limited")
        self.assertIsNone(job["contract"]["review"])  # not a failed review: no revision round is used
        summary = cli.summary(job)
        self.assertIn(summary["limits"]["claude"]["until"], forms)
        logged = self.logged_runs()[-1]
        self.assertEqual((logged["stage"], logged["limited"]), ("blocked", {"runtime": "claude",
                                                                           "reset_at": reset.isoformat()}))
        self.limit.unlink()
        done = coordinator.run(job_id, self.qk)
        self.assertEqual(done["stopped"], "done")
        job = state.load(job_id)
        self.assertNotIn("limits", job)
        self.assertEqual(job["contract"].get("revisions", 0), 0)

    def test_a_write_turn_stopped_mid_turn_leaves_no_edits_and_runs_again_after_the_reset(self):
        job_id = self.approved_job()
        self.hold("codex", None, partial=True)
        reply = coordinator.run(job_id, self.qk)
        self.assertEqual((reply["stopped"], reply["capability"]), ("limited", "contract.write"))
        worktree = Path(state.load(job_id)["integration"]["worktree"])
        self.assertFalse((worktree / "partial.txt").exists())
        self.assertEqual(sh_status(worktree), "")
        self.limit.unlink()
        self.assertEqual(coordinator.run(job_id, self.qk)["stopped"], "done")

    def test_a_decision_on_one_task_is_not_hidden_behind_another_tasks_limit(self):
        job_id = self.approved_job()
        with state.locked(job_id) as job:
            job["tasks"]["api"]["review_spec"] = "gemini:x"
        self.hold("gemini", None)
        self.assertEqual(coordinator.run(job_id, self.qk)["stopped"], "limited")
        with state.locked(job_id) as job:  # the other task used its fix round and still fails its checks
            job["tasks"]["cli"].update(fix_rounds=1, review=None,
                                       verify={"results": [{"cmd": "test", "exit": 1, "tail": "failed"}]})
            job["limits"] = {"gemini": {"reset_at": None}}
            step = policy.next_step(job)
            self.assertTrue(step.startswith("a person decides: task cli still fails its checks"), step)
            self.assertNotIn("limited", ledger.run_state(job, "blocked"))
            self.assertIn("limited", ledger.run_state(job, "blocked", limited=True))

    def test_waiting_sleeps_until_the_reset_and_carries_on_to_the_pull_request(self):
        job_id = self.approved_job()
        reset = (datetime.now().astimezone() + timedelta(minutes=50)).replace(microsecond=0)
        self.hold("claude", reset.isoformat())
        woke = []

        def sleep_until(moment):
            woke.append(moment)
            self.limit.unlink()  # the reset has come

        with mock.patch.object(coordinator, "sleep_until", sleep_until):
            reply = coordinator.run(job_id, self.qk, wait_limits=True,
                                    deadline=datetime.now().astimezone() + timedelta(hours=3))
        self.assertEqual(reply["stopped"], "done")
        self.assertEqual(woke, [reset + coordinator.LIMIT_GRACE])
        self.assertEqual(len(self.notices), 2, self.notices)
        self.assertIn(f"waits until {(reset + coordinator.LIMIT_GRACE):%H:%M}", self.notices[0])
        self.assertIn("Retrying", self.notices[1])
        self.assertEqual(state.load(job_id)["pr_url"], "https://github.com/example/project/pull/7")

    def test_a_task_on_another_runtime_goes_on_while_the_limited_one_waits(self):
        job_id = self.approved_job()
        with state.locked(job_id) as job:
            job["tasks"]["cli"]["review_spec"] = "gemini:x"
        self.hold("gemini", None)
        reply = coordinator.run(job_id, self.qk)
        self.assertEqual(reply["stopped"], "limited")
        self.assertTrue(reply["next"].startswith("wait: task cli needs the gemini usage limit to reset "
                                                 "(reset time unknown), then review.run"), reply["next"])
        tasks = state.load(job_id)["tasks"]
        self.assertTrue(policy.review_ok(tasks["api"]["review"]))
        self.assertIsNone(tasks["cli"]["review"])
        self.assertIsNotNone(tasks["cli"]["commit"])
        self.limit.unlink()
        self.assertEqual(coordinator.run(job_id, self.qk)["stopped"], "done")
        tasks = state.load(job_id)["tasks"]
        self.assertEqual((tasks["api"]["fix_rounds"], tasks["cli"]["fix_rounds"]), (0, 0))

    def test_a_reset_after_the_deadline_stops_with_a_notice_and_no_wait(self):
        job_id = self.approved_job()
        self.hold("claude", (datetime.now().astimezone() + timedelta(hours=3)).isoformat())
        with mock.patch.object(coordinator, "sleep_until", side_effect=AssertionError("waited past the deadline")):
            reply = coordinator.run(job_id, self.qk, wait_limits=True,
                                    deadline=datetime.now().astimezone() + timedelta(hours=1))
        self.assertEqual(reply["stopped"], "limited")
        self.assertEqual(len(self.notices), 1)
        self.assertIn("Gave up", self.notices[0])
        self.assertIn("claude", self.notices[0])

    def test_a_limit_that_never_lifts_ends_after_the_last_wait(self):
        job_id = self.approved_job()
        self.hold("claude", None)
        waits = []
        with mock.patch.object(coordinator, "sleep_until", waits.append):
            reply = coordinator.run(job_id, self.qk, wait_limits=True)
        self.assertEqual(reply["stopped"], "limited")
        self.assertEqual(len(waits), coordinator.LIMIT_WAITS)
        self.assertIn(f"after {coordinator.LIMIT_WAITS} waits", self.notices[-1])

    def test_no_reset_time_retries_after_the_gap(self):
        before = datetime.now().astimezone()
        wake = coordinator.wake_time({"claude": {"reset_at": None}})
        self.assertGreaterEqual(wake, before + coordinator.LIMIT_RETRY_GAP)
        self.assertLess(wake, datetime.now().astimezone() + coordinator.LIMIT_RETRY_GAP + timedelta(seconds=5))

    def test_a_reset_on_another_day_names_its_date(self):
        tomorrow = (datetime.now().astimezone() + timedelta(days=1)).replace(microsecond=0)
        self.assertEqual(policy.until({"reset_at": tomorrow.isoformat()}), f"until {tomorrow:%Y-%m-%d %H:%M}")

    def test_until_is_the_next_such_local_time(self):
        now = datetime(2026, 11, 24, 5, 0).astimezone()
        self.assertEqual(coordinator.deadline_at("06:30", now), now.replace(hour=6, minute=30))
        self.assertEqual(coordinator.deadline_at("04:00", now), now.replace(hour=4) + timedelta(days=1))

    def test_until_uses_the_offset_of_the_day_it_falls_on(self):
        zone = os.environ.get("TZ")
        self.addCleanup(lambda: (os.environ.update(TZ=zone) if zone else os.environ.pop("TZ", None), time.tzset()))
        os.environ["TZ"] = "America/New_York"
        time.tzset()
        before = datetime(2026, 3, 7, 23, 0).astimezone()  # clocks move forward at 02:00 on 8 March
        deadline = coordinator.deadline_at("04:00", before)
        self.assertEqual((deadline.day, deadline.hour, deadline.utcoffset()), (8, 4, timedelta(hours=-4)))
        self.assertEqual(before.utcoffset(), timedelta(hours=-5))
        repeated = datetime(2026, 11, 1, 1, 15, fold=1).astimezone()  # the second 01:15, after clocks go back
        deadline = coordinator.deadline_at("01:30", repeated)
        self.assertEqual((deadline.day, deadline.hour, deadline.minute), (1, 1, 30))
        self.assertEqual(deadline - repeated, timedelta(minutes=15))

    def test_partial_edits_are_dropped_only_inside_the_jobs_own_directory(self):
        job_id = self.approved_job()
        outside = self.tmp / "elsewhere"
        outside.mkdir()
        (outside / "keep.txt").write_text("mine\n")
        reply = mock.Mock(code=2, stderr="", data={"contract": 1, "status": "blocked", "reason": "limit", "usage": None,
                                                    "limit": {"runtime": "codex", "reset_at": None}})
        discarded = []
        with mock.patch.object(self.qk, "agent_run", return_value=reply), \
                mock.patch.object(capabilities.gitops, "discard", discarded.append), state.locked(job_id) as job:
            for cwd in (outside, self.repo, Path(job["integration"]["worktree"])):
                with self.assertRaises(capabilities.Limited):
                    capabilities.turn(self.qk, job, "codex:", cwd, "prompt", write=True)
        self.assertEqual(discarded, [Path(state.load(job_id)["integration"]["worktree"])])

    def test_run_takes_wait_limits_and_until(self):
        job_id = self.approved_job()
        calls = []
        with mock.patch.object(coordinator, "run", lambda *args, **kwargs: calls.append(kwargs) or {"stopped": "done"}), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            for argv in ([], ["--until", "06:30"], ["--until", "06:30", "--no-wait-limits"], ["--wait-limits"]):
                self.assertEqual(cli.main(["--lock", str(self.lock), "run", job_id, *argv]), 0)
        self.assertEqual([(call["wait_limits"], call["deadline"] is not None) for call in calls],
                         [(False, False), (True, True), (False, True), (True, False)])
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.main(["--lock", str(self.lock), "run", job_id, "--until", "25:00"])
