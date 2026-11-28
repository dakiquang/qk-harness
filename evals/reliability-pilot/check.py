"""Grade only the isolated CSV/JSON behavior; importing this file does no work."""

import argparse
import copy
import csv
import io
import json
from pathlib import Path
import types


CASES = (
    ("empty", []),
    ("single", [{"name": "Ada", "note": "hello"}]),
    ("empty fields", [{"name": "", "note": ""}]),
    ("commas and quotes", [{"name": 'Doe, "Jane"', "note": 'say "hello", please'}]),
    ("newlines", [{"name": "first\nlast", "note": "one\r\ntwo\rthree"}]),
    ("Unicode", [{"name": "Quảng 🌿", "note": "日本語 café"}]),
    ("record order", [{"name": "z", "note": "last alphabetically"},
                      {"name": "a", "note": "first alphabetically"},
                      {"name": "z", "note": "a repeated name"}]),
)


def load_render(repo, task):
    path = repo / "evals" / "reliability-pilot" / "solution" / (task + "_export.py")
    if not path.is_file():
        raise ValueError("missing implementation: " + str(path))
    # Compile directly so grading does not create bytecode in the target checkout.
    module = types.ModuleType("reliability_pilot_" + task)
    module.__file__ = str(path)
    exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
    render = getattr(module, "render", None)
    if not callable(render):
        raise ValueError("implementation must expose callable render(records)")
    return render


def check_task(repo, task):
    """Return behavioral failures for one task without loading the other module."""
    try:
        render = load_render(Path(repo), task)
    except (Exception, SystemExit) as error:
        return ["{}: {}: {}".format(task, type(error).__name__, error)]
    failures = []
    for label, expected in CASES:
        records = copy.deepcopy(expected)
        before = copy.deepcopy(records)
        try:
            output = render(records)
            if records != before:
                raise ValueError("input was mutated")
            if not isinstance(output, str):
                raise ValueError("render must return str")
            if task == "csv":
                actual = list(csv.reader(io.StringIO(output, newline=""), strict=True))
                wanted = [["name", "note"]] + [[row["name"], row["note"]] for row in expected]
            else:
                actual = json.loads(output)
                wanted = expected
            if actual != wanted:
                raise ValueError("roundtrip differs from required header, values or record order")
        except (Exception, SystemExit) as error:
            failures.append("{} / {}: {}: {}".format(task, label, type(error).__name__, error))
    return failures


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True, help="target checkout to grade")
    parser.add_argument("--task", choices=("csv", "json", "all"), required=True)
    args = parser.parse_args(argv)
    tasks = ("csv", "json") if args.task == "all" else (args.task,)
    failures = []
    for task in tasks:
        errors = check_task(args.repo.resolve(), task)
        failures.extend(errors)
        print("{}: {}".format(task, "FAIL" if errors else "PASS"))
        for error in errors:
            print("  " + error)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
