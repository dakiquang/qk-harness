"""Read-only status listings for unfinished jobs."""

import contextlib
from datetime import datetime, timedelta
import io
import json
import multiprocessing
import os
from pathlib import Path
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_capabilities import Sandbox  # noqa: E402
from harness import __main__ as cli, capabilities, gitops, state  # noqa: E402


def hold_lock(path, ready, release):
    with gitops.file_lock(path):
        ready.set()
        release.wait(10)


class StatusTests(Sandbox):
    def listing(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(['status', *args]), 0)
        return output.getvalue()

    def test_empty_and_single_job_compatibility(self):
        self.assertEqual(self.listing('--line'), '')
        self.assertEqual(json.loads(self.listing('--json')), {'contract': 1, 'items': []})
        job_id = self.new()
        self.assertEqual(json.loads(self.listing(job_id)), cli.summary(state.load(job_id)))
        self.assertEqual(json.loads(self.listing()), json.loads(self.listing('--json')))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.main(['status', job_id, '--json'])

    def test_approval_and_limit(self):
        job_id = self.new()
        self.advance(job_id)
        item = json.loads(self.listing('--json'))['items'][0]
        self.assertEqual(item['waiting_for'], {'person': f'approve {job_id} plan'})
        self.assertIn('waits: approve plan', self.listing('--line'))
        capabilities.approve(job_id, 'plan')
        self.advance(job_id, until='contract.review')
        reset = (datetime.now().astimezone() + timedelta(hours=1)).replace(microsecond=0)
        with state.locked(job_id) as job:
            job['limits'] = {'claude': {'reset_at': reset.isoformat()}}
        self.assertIn(f'claude limit until {reset:%H:%M}', self.listing('--line'))
        self.assertEqual(json.loads(self.listing('--json'))['items'][0]['waiting_for'],
                         {'limit': 'claude', 'until': reset.strftime('%H:%M')})

    def test_lock_and_task_pids(self):
        job_id = self.new()
        self.advance(job_id)
        capabilities.approve(job_id, 'plan')
        self.advance(job_id, until='build.run')
        ready, release = multiprocessing.Event(), multiprocessing.Event()
        process = multiprocessing.Process(target=hold_lock, args=(state.job_dir(job_id) / 'job.lock', ready, release))
        process.start()
        try:
            self.assertTrue(ready.wait(5))
            self.assertEqual(json.loads(self.listing('--json'))['items'][0]['running'], ['step'])
        finally:
            release.set()
            process.join(5)
        with state.locked(job_id) as job:
            job['tasks']['api']['running'] = {'capability': 'review.run', 'pid': os.getpid()}
        self.assertEqual(json.loads(self.listing('--json'))['items'][0]['running'], ['review.run api'])
        self.assertIn('review.run api', self.listing('--line'))
        with state.locked(job_id) as job:
            job['tasks']['api']['running']['pid'] = 99999999
        self.assertEqual(json.loads(self.listing('--json'))['items'][0]['running'], [])
        self.assertIn('stopped mid-step · qk-harness run', self.listing('--line'))
        self.assertEqual(json.loads(self.listing('--json'))['items'][0]['waiting_for'],
                         {'person': 'task api stopped mid-step'})

    def test_excludes_finished_and_unreadable(self):
        first, second, third = self.new(), self.new(), self.new()
        with state.locked(first) as job:
            job['pr_url'] = 'https://example.com/pr/1'
        with state.locked(second) as job:
            job['aborted'] = {'reason': 'stop'}
        (state.job_dir(third) / 'state.json').write_text('{')
        self.assertEqual(json.loads(self.listing('--json'))['items'], [])

    def test_status_lines_leave_out_jobs_untouched_for_a_day(self):
        old, fresh = self.new(), self.new()
        with state.locked(old) as job:
            job['log'].append({'at': '2026-01-01T09:00:00+0700', 'capability': 'plan.write', 'status': 'done',
                               'reason': ''})
        self.assertEqual([item['id'] for item in json.loads(self.listing('--json'))['items']], sorted([old, fresh]))
        lines = self.listing('--line').splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn(fresh[-6:], lines[0])

    def test_listing_speed(self):
        for _ in range(20):
            self.new()
        timings = []
        for _ in range(5):
            start = time.perf_counter()
            lines = self.listing('--line').splitlines()
            timings.append(time.perf_counter() - start)
        self.assertEqual(lines, ["harness 20 jobs not running (qk-harness status)"])  # idle jobs share one line
        self.assertEqual(len(json.loads(self.listing('--json'))['items']), 20)
        self.assertLess(statistics.median(timings), 0.05)


if __name__ == '__main__':
    import unittest
    unittest.main()
