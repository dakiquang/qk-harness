"""A scripted agent for capability tests: `fake_agent.py CWD read|write SPEC < prompt`.

It recognises which capability sent the prompt and acts like a cooperative agent on the sample project in
`test_capabilities.py`. FAKE_AGENT holds comma-separated deviations:

- `weak-contract`: the contract tests already pass
- `outside`: a build also edits a file outside the task's paths
- `commit`: a build commits on its own
- `broken`: the first build of `api` returns the wrong value (its checks fail)
- `findings`: reviews report one important finding
- `no-verify`, `overlap`: the plan lacks a qk-verify block, or its tasks own the same path
- `contract-outside`: the contract also writes a file outside its test paths
- `contract-once`, `contract-twice`: the contract review finds an important problem until the contract is revised,
  or every time
"""

import json
from pathlib import Path
import re
import subprocess
import sys


cwd, mode, spec = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
prompt = sys.stdin.read()
flags = set(filter(None, __import__("os").environ.get("FAKE_AGENT", "").split(",")))


def done(summary="ok", findings=()):
    print("```qk-result\n" + json.dumps({"status": "done", "summary": summary, "files": [], "evidence": [],
                                          "reason": "", "findings": list(findings)}) + "\n```")
    sys.exit(0)


if "`create` mode" in prompt:
    tasks = [{"name": "api", "owns": ["src/api/"],
              "verify": ["python3 -c \"from src.api import greet; assert greet() == 'hello'\""]},
             {"name": "cli", "owns": ["src/api/" if "overlap" in flags else "src/cli/"],
              "verify": ["python3 -c \"from src.cli import name; assert name() == 'cli'\""]}]
    verify = "" if "no-verify" in flags else "```qk-verify\npython3 -m unittest discover -s tests\n```\n"
    layout = {"contract": ["tests/"], "tasks": tasks}
    if "no separate test-writing step" in prompt:  # without a contract, each task owns its own tests
        layout = {"tasks": tasks}
        for task in tasks:
            task["owns"].append(f"tests/test_{task['name']}.py")
    print("<<<QK-PLAN\n# Add greeting API and CLI\n\nDone when both tests pass.\n\n" + verify
          + "```qk-harness\n" + json.dumps(layout) + "\n```\nQK-PLAN>>>")
    done("planned")
if prompt.startswith("Write the contract"):
    tests = cwd / "tests"
    tests.mkdir(exist_ok=True)
    expected = "('hello', 'cli')" if "weak-contract" not in flags else "None"
    (tests / "test_contract.py").write_text(
        "import unittest\n\n\nclass Contract(unittest.TestCase):\n"
        "    def test_both(self):\n"
        "        try:\n"
        "            from src.api import greet\n"
        "            from src.cli import name\n"
        "            value = (greet(), name())\n"
        "        except ImportError:\n"
        "            value = None\n"
        f"        self.assertEqual(value, {expected})\n")
    if "contract-outside" in flags:
        (cwd / "src").mkdir(exist_ok=True)
        (cwd / "src" / "helper.py").write_text("HELPER = 1\n")
    done("contract written")
if "build task `" in prompt:
    task = re.search(r"build task `([a-z-]+)`", prompt).group(1)
    package = cwd / "src" / task
    package.mkdir(parents=True, exist_ok=True)
    value = {"api": "'hello'", "cli": "'cli'"}[task]
    if task == "api" and "broken" in flags and "Fix these problems" not in prompt:
        value = "'wrong'"
    function = "greet" if task == "api" else "name"
    (package / "__init__.py").write_text(f"def {function}():\n    return {value}\n")
    owned_test = re.search(r"change only these paths: ([^\n]*)", prompt)
    if owned_test and f"tests/test_{task}.py" in owned_test.group(1):  # the task writes its own test
        (cwd / "tests" / f"test_{task}.py").write_text("import unittest\n")
    if "outside" in flags:
        (cwd / "README.md").write_text("changed by a worker\n")
    if "commit" in flags:
        subprocess.run(["git", "add", "-A"], cwd=str(cwd), check=True)
        subprocess.run(["git", "-c", "user.name=w", "-c", "user.email=w@example.invalid", "commit", "-qm", "worker"],
                       cwd=str(cwd), check=True)
    done(f"built {task}")
if prompt.startswith("Revise the contract"):
    contract = cwd / "tests" / "test_contract.py"
    contract.write_text(contract.read_text() + "# revised\n")
    done("contract revised")
if "review of a test contract" in prompt and ("contract-twice" in flags or "contract-once" in flags and
                                              "# revised" not in (cwd / "tests" / "test_contract.py").read_text()):
    done("reviewed", [{"severity": "important", "file": "tests/test_contract.py", "line": 1,
                       "claim": "tests a behaviour the plan does not state", "fix": "drop it"}])
if "Independent read-only review" in prompt:
    findings = [{"severity": "important", "file": "src/api/__init__.py", "line": 2, "claim": "fake finding"}] \
        if "findings" in flags else []
    done("reviewed", findings)
print("unrecognised prompt", file=sys.stderr)
sys.exit(1)
