import copy
import csv
import importlib.util
import io
import json
from pathlib import Path
import unittest


SOLUTION = Path(__file__).resolve().parent.parent / "solution"
CASES = (
    [],
    [{"name": "", "note": ""}],
    [
        {"name": "first", "note": "comma, and quote \""},
        {"name": "Tiếng Việt 雪", "note": "line one\rline two\nline three\r\nend"},
        {"name": "\r\n,\"", "note": ""},
        {"name": "", "note": "last"},
    ],
)


class RenderContract(unittest.TestCase):
    def load_render(self, module_name):
        path = SOLUTION / (module_name + ".py")
        self.assertTrue(path.is_file(), f"Missing implementation: {path}")
        spec = importlib.util.spec_from_file_location(module_name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertTrue(callable(getattr(module, "render", None)),
                        f"{module_name}.render must be callable")
        return module.render

    def test_csv_roundtrip_and_input_preservation(self):
        render = self.load_render("csv_export")
        for case in CASES:
            with self.subTest(records=case):
                records = copy.deepcopy(case)
                before = copy.deepcopy(records)
                output = render(records)
                self.assertEqual(records, before)
                self.assertIsInstance(output, str)
                rows = list(csv.reader(io.StringIO(output, newline="")))
                self.assertEqual(rows, [["name", "note"]] +
                                 [[record["name"], record["note"]] for record in before])

    def test_json_roundtrip_and_input_preservation(self):
        render = self.load_render("json_export")
        for case in CASES:
            with self.subTest(records=case):
                records = copy.deepcopy(case)
                before = copy.deepcopy(records)
                output = render(records)
                self.assertEqual(records, before)
                self.assertIsInstance(output, str)
                self.assertEqual(json.loads(output), before)


if __name__ == "__main__":
    unittest.main()
