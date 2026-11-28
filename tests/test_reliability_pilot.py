"""Offline oracle proofs using disposable implementations, never real agents."""

import hashlib
import importlib.util
import multiprocessing
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


ORACLE = Path(__file__).resolve().parents[1] / "evals" / "reliability-pilot" / "check.py"
CSV_CORRECT = '''
import csv
import io

def render(records):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(["name", "note"])
    writer.writerows([row["name"], row["note"]] for row in records)
    return stream.getvalue()
'''
JSON_CORRECT = '''
import json

def render(records):
    return json.dumps(records, ensure_ascii=False)
'''


def import_oracle(path):
    spec = importlib.util.spec_from_file_location("pilot_oracle", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def spawn_import(path, sender):
    """A real spawn must import this test module without starting another suite."""
    try:
        module = import_oracle(path)
        sender.send((module.__name__, callable(module.main)))
    finally:
        sender.close()


class ReliabilityPilotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="qk-pilot-oracle-")
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)
        self.solution = self.repo / "evals" / "reliability-pilot" / "solution"
        self.solution.mkdir(parents=True)
        self.oracle = import_oracle(ORACLE)

    def implementation(self, task, source):
        (self.solution / (task + "_export.py")).write_text(textwrap.dedent(source), encoding="utf-8")

    def invoke(self, task):
        return subprocess.run(
            [sys.executable, "-I", str(ORACLE), "--repo", str(self.repo), "--task", task],
            capture_output=True, text=True, timeout=10,
        )

    def assert_rejected(self, task, source, case=None):
        self.implementation(task, source)
        errors = self.oracle.check_task(self.repo, task)
        self.assertTrue(errors, "oracle accepted incorrect " + task)
        if case is not None:
            self.assertTrue(any(case in error for error in errors), errors)

    def test_correct_modules_pass_independently_and_together_without_changing_sources(self):
        self.implementation("csv", CSV_CORRECT)
        self.implementation("json", JSON_CORRECT)
        originals = {path: path.read_bytes() for path in self.solution.iterdir()}
        digest = hashlib.sha256(ORACLE.read_bytes()).hexdigest()
        for task in ("csv", "json", "all"):
            with self.subTest(task=task):
                result = self.invoke(task)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual({path: path.read_bytes() for path in self.solution.iterdir()}, originals)
        self.assertEqual(hashlib.sha256(ORACLE.read_bytes()).hexdigest(), digest)

    def test_seed_is_red_for_missing_behavior_not_checker_setup(self):
        digest = hashlib.sha256(ORACLE.read_bytes()).hexdigest()
        result = self.invoke("all")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("csv: FAIL", result.stdout)
        self.assertIn("json: FAIL", result.stdout)
        self.assertEqual(result.stdout.count("missing implementation:"), 2)
        self.assertEqual(result.stderr, "")
        self.assertEqual(hashlib.sha256(ORACLE.read_bytes()).hexdigest(), digest)

    def test_task_verify_never_imports_the_other_module(self):
        for task, other, correct in (("csv", "json", CSV_CORRECT), ("json", "csv", JSON_CORRECT)):
            with self.subTest(task=task):
                self.implementation(task, correct)
                self.implementation(other, 'raise RuntimeError("other task must not be loaded")')
                result = self.invoke(task)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                result = self.invoke("all")
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(other + ": FAIL", result.stdout)
                self.assertIn("other task must not be loaded", result.stdout)

    def test_csv_requires_header_even_when_empty(self):
        self.assert_rejected("csv", CSV_CORRECT.replace('writer.writerow(["name", "note"])', 'pass'), "empty")
        self.assert_rejected("csv", CSV_CORRECT.replace('return stream.getvalue()',
                             'return stream.getvalue() if records else ""'), "empty")

    def test_csv_rejects_naive_escaping_and_wrong_column_order(self):
        self.assert_rejected("csv", '''
            def render(records):
                return "name,note\\n" + "".join(row["name"] + "," + row["note"] + "\\n" for row in records)
        ''', "commas and quotes")
        self.assert_rejected("csv", CSV_CORRECT.replace('[row["name"], row["note"]]',
                                                        '[row["note"], row["name"]]'), "single")

    def test_json_requires_roundtrip_and_empty_list(self):
        self.assert_rejected("json", 'def render(records): return str(records)', "single")
        self.assert_rejected("json", JSON_CORRECT.replace('json.dumps(records, ensure_ascii=False)',
                             'json.dumps(records or None)'), "empty")
        self.assert_rejected("json", JSON_CORRECT.replace('json.dumps(records, ensure_ascii=False)',
                             'json.dumps(list(reversed(records)))'), "record order")

    def test_both_formats_preserve_unicode_newlines_and_all_records(self):
        for task, correct in (("csv", CSV_CORRECT), ("json", JSON_CORRECT)):
            with self.subTest(task=task):
                wrapped = correct.replace('def render(records):', 'def correct(records):')
                self.assert_rejected(task, wrapped + '''
def render(records):
    return correct(records).encode("ascii", "replace").decode("ascii")
''', "Unicode")
                self.assert_rejected(task, wrapped + '''
def render(records):
    altered = [{"name": row["name"].replace("\\n", " "),
                "note": row["note"].replace("\\r", " ").replace("\\n", " ")} for row in records]
    return correct(altered)
''', "newlines")
                self.assert_rejected(task, wrapped + '''
def render(records):
    return correct(records[:1])
''', "record order")

    def test_both_formats_require_string_and_unchanged_input(self):
        for task, correct in (("csv", CSV_CORRECT), ("json", JSON_CORRECT)):
            with self.subTest(task=task):
                self.assert_rejected(task, "def render(records): return []", "render must return str")
                wrapped = correct.replace('def render(records):', 'def correct(records):')
                self.assert_rejected(task, wrapped + '''
def render(records):
    output = correct(records)
    records.reverse()
    return output
''', "input was mutated")
                self.assert_rejected(task, wrapped + '''
def render(records):
    output = correct(records)
    if records:
        records[0]["note"] = "changed"
    return output
''', "input was mutated")

    def test_missing_callable_syntax_error_and_render_exception_are_failures(self):
        for source in ("render = None", "this is invalid python!",
                       'def render(records): raise RuntimeError("broken")',
                       "raise SystemExit(0)", "def render(records): raise SystemExit(0)"):
            with self.subTest(source=source):
                self.assert_rejected("csv", source)
                result = self.invoke("csv")
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("csv: FAIL", result.stdout)
                self.assertEqual(result.stderr, "")

    def test_oracle_and_test_module_are_safe_to_import_under_spawn(self):
        context = multiprocessing.get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        child = context.Process(target=spawn_import, args=(str(ORACLE), sender))
        try:
            child.start()
            sender.close()
            self.assertTrue(receiver.poll(10), "spawn import did not return a handshake")
            self.assertEqual(receiver.recv(), ("pilot_oracle", True))
            child.join(10)
            self.assertEqual(child.exitcode, 0)
        finally:
            sender.close()
            receiver.close()
            if child.pid is not None:
                if child.is_alive():
                    child.terminate()
                    child.join(2)
                if child.is_alive():
                    child.kill()
                    child.join(2)
                child.close()


if __name__ == "__main__":
    unittest.main()
