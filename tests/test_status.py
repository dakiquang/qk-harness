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

    def test_findings_for_each_task_waiting_for_a_person(self):
        job_id = self.new()
        self.advance(job_id)
        capabilities.approve(job_id, 'plan')
        self.advance(job_id, until='build.run')
        with state.locked(job_id) as job:
            for task in job['tasks'].values():
                task.update(commit='abc', verify={'results': [{'exit': 0}]}, fix_rounds=1)
            job['tasks']['api']['review'] = {'status': 'done', 'findings': [
                {'severity': 'BLOCKER', 'file': 'api.py', 'line': 4, 'claim': 'broken'},
                {'severity': 'minor', 'file': 'api.py', 'line': 5, 'claim': 'style'}]}
            job['tasks']['cli']['review'] = {'status': 'done', 'findings': [
                {'severity': 'Important', 'claim': 'no location'}]}
        result = json.loads(self.listing(job_id))
        self.assertEqual(result['findings'], [
            {'target': 'api', 'severity': 'BLOCKER', 'file': 'api.py', 'line': 4, 'claim': 'broken'},
            {'target': 'cli', 'severity': 'Important', 'file': None, 'line': None, 'claim': 'no location'}])
        with state.locked(job_id) as job:
            job['tasks']['api']['fix_rounds'] = 0
            job['tasks']['cli']['review'] = {'status': 'done', 'findings': []}
        self.assertEqual(json.loads(self.listing(job_id))['findings'], [])

    def test_integrated_findings_and_empty_decisions(self):
        job_id = self.new()
        self.advance(job_id)
        capabilities.approve(job_id, 'plan')
        self.advance(job_id, until='build.run')
        with state.locked(job_id) as job:
            job['tasks']['api']['review'] = {'status': 'done', 'findings': [
                {'severity': 'blocker', 'claim': 'stale'}]}
            job['integration'].update(merged=True, commit='abc', verify={'results': [{'exit': 0}]}, fix_rounds=1,
                                      review={'status': 'done', 'findings': [
                                          {'severity': 'important', 'file': None, 'claim': 'integrated'},
                                          {'severity': 'minor', 'claim': 'ignored'}]})
        result = json.loads(self.listing(job_id))
        self.assertTrue(result['next'].startswith('a person decides'))
        self.assertEqual(result['findings'], [
            {'target': 'integration', 'severity': 'important', 'file': None, 'line': None, 'claim': 'integrated'}])
        with state.locked(job_id) as job:
            job['integration']['fix_rounds'] = 0
            job['integration']['review']['findings'][0].update(file='app.py', line=3)
        self.assertEqual(json.loads(self.listing(job_id))['findings'], [])
        with state.locked(job_id) as job:
            job['integration']['fix_rounds'] = 1
            job['integration']['verify'] = {'results': [{'exit': 1}]}
            job['integration']['review'] = None
        result = json.loads(self.listing(job_id))
        self.assertTrue(result['next'].startswith('a person decides'))
        self.assertEqual(result['findings'], [])
        # A decision with another cause does not list review findings that are still recorded.
        with state.locked(job_id) as job:
            job['integration']['review'] = {'status': 'done', 'findings': [{'severity': 'blocker', 'claim': 'old'}]}
        result = json.loads(self.listing(job_id))
        self.assertIn('fails its checks', result['next'])
        self.assertEqual(result['findings'], [])
        job = state.load(job_id)
        with open(job['plan']['file'], 'a', encoding='utf-8') as plan:
            plan.write('\nedited by hand\n')
        result = json.loads(self.listing(job_id))
        self.assertIn('changed after it was written', result['next'])
        self.assertEqual(result['findings'], [])

    def test_contract_review_findings_when_its_revision_is_used(self):
        job_id = self.new()
        self.advance(job_id)
        capabilities.approve(job_id, 'plan')
        self.advance(job_id, until='build.run')
        with state.locked(job_id) as job:
            job['contract'].update(revisions=1, review={'status': 'done', 'findings': [
                {'severity': 'important', 'file': 'tests/test_api.py', 'line': 7, 'claim': 'stricter than the plan'},
                {'severity': 'nit', 'claim': 'naming'}]})
        result = json.loads(self.listing(job_id))
        self.assertIn('the contract review did not pass', result['next'])
        self.assertEqual(result['findings'], [{'target': 'contract', 'severity': 'important',
                                               'file': 'tests/test_api.py', 'line': 7,
                                               'claim': 'stricter than the plan'}])
        with state.locked(job_id) as job:
            job['contract']['revisions'] = 0
        result = json.loads(self.listing(job_id))
        self.assertFalse(result['next'].startswith('a person decides'))
        self.assertEqual(result['findings'], [])

    def test_task_failing_checks_does_not_list_its_old_findings(self):
        job_id = self.new()
        self.advance(job_id)
        capabilities.approve(job_id, 'plan')
        self.advance(job_id, until='build.run')
        with state.locked(job_id) as job:
            job['tasks']['api'].update(commit='abc', verify={'results': [{'exit': 1}]}, fix_rounds=1,
                                       review={'status': 'done', 'findings': [{'severity': 'blocker', 'claim': 'old'}]})
        result = json.loads(self.listing(job_id))
        self.assertIn('task api still fails its checks', result['next'])
        self.assertEqual(result['findings'], [])
        with state.locked(job_id) as job:
            job['tasks']['api']['verify'] = {'results': [{'exit': 0}]}
        self.assertEqual(json.loads(self.listing(job_id))['findings'][0]['claim'], 'old')
        job = state.load(job_id)
        with open(job['plan']['file'], 'a', encoding='utf-8') as plan:
            plan.write('\nedited by hand\n')
        result = json.loads(self.listing(job_id))
        self.assertIn('changed after it was written', result['next'])
        self.assertEqual(result['findings'], [])

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
        item = json.loads(self.listing('--json'))['items'][0]
        self.assertEqual(item['running'], [])
        self.assertIn('person', item['waiting_for'])
        self.assertEqual(item['next'].split()[0], 'status')
        self.assertIn('running', state.load(job_id)['tasks']['api'])

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
