"""Behavioral contract for the isolated export modules."""

import copy
import csv
import importlib.util
import io
import json
from pathlib import Path
import unittest


SOLUTION = Path(__file__).resolve().parent.parent / "solution"
CASES = (
    ("no records", []),
    ("empty values", [{"name": "", "note": ""}]),
    ("special characters", [
        {"name": 'Lê, "An"', "note": '日本語, "café"'},
        {"name": "line\nbreak", "note": "carriage\rreturn"},
        {"name": "both\r\nlines", "note": "\n\r"},
    ]),
    ("ordered records", [
        {"name": "Z", "note": "first"},
        {"name": "A", "note": "second"},
        {"name": "Z", "note": "third"},
    ]),
)


class RenderContract(unittest.TestCase):
    def load_render(self, task):
        path = SOLUTION / (task + "_export.py")
        self.assertTrue(path.is_file(), "missing implementation: {}".format(path))
        spec = importlib.util.spec_from_file_location("contract_" + task, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        render = getattr(module, "render", None)
        self.assertTrue(callable(render), "{} must expose render(records)".format(path))
        return render

    def check_cases(self, task):
        render = self.load_render(task)
        for label, expected in CASES:
            with self.subTest(case=label):
                records = copy.deepcopy(expected)
                original_records = list(records)
                output = render(records)
                self.assertEqual(records, expected, "render must not mutate its input")
                for record, original in zip(records, original_records):
                    self.assertIs(record, original, "render must not replace input records")
                self.assertIsInstance(output, str)
                if task == "csv":
                    actual = list(csv.reader(io.StringIO(output, newline="")))
                    wanted = [["name", "note"]] + [
                        [record["name"], record["note"]] for record in expected
                    ]
                else:
                    actual = json.loads(output)
                    wanted = expected
                self.assertEqual(actual, wanted)

    def test_csv(self):
        self.check_cases("csv")

    def test_json(self):
        self.check_cases("json")


if __name__ == "__main__":
    unittest.main()
