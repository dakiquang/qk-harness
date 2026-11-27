"""A stand-in qk for tests: answers each contract command with JSON shaped as qk contract 1 documents it.

`make_repo(directory)` writes it as `scripts/qk.py` in a new Git repository, commits it, tags the commit and returns
the commit id. Behaviour comes from environment variables:

- FAKE_QK_CONTRACT: the contract number `qk contract` reports (default 1)
- FAKE_QK_DROP: a contract command to leave out of the command list
- FAKE_QK_SCENARIO: `done`, `blocked` or `error` for agent run, log run and eval grade (default done)
- FAKE_QK_LOG: a file that receives one JSON line per call: argv and stdin
- FAKE_QK_SLEEP: seconds agent run waits before answering; FAKE_QK_PID: file that receives its process id
- FAKE_QK_LIMIT: a file; while it exists, its JSON `{"runtime", "reset_at"}` makes agent run with that runtime
  stop at its usage limit (blocked, with `limit` set as qk reports it); `"partial": true` first writes a file in
  --cwd of a write turn, as an agent stopped mid-turn would
- FAKE_QK_COST_USD: reported USD of each non-Codex turn; absent means the runtime supplies no USD
"""

from pathlib import Path
import subprocess


SCRIPT = r'''#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path

argv = sys.argv[1:]
stdin = "" if sys.stdin.isatty() else sys.stdin.read()
if os.environ.get("FAKE_QK_LOG"):
    with open(os.environ["FAKE_QK_LOG"], "a") as log:
        log.write(json.dumps({"argv": argv, "stdin": stdin}) + "\n")
scenario = os.environ.get("FAKE_QK_SCENARIO", "done")
command = " ".join(argv[:2]) if argv[:1] in (["agent"], ["log"], ["eval"]) else argv[0]

def reply(data, code=0):
    print(json.dumps(data))
    sys.exit(code)

def fail():
    print("qk: fake failure", file=sys.stderr)
    sys.exit(1)

if command == "contract":
    commands = ["contract", "show", "lesson list", "agent run", "agent family", "log run", "eval prepare",
                "eval grade"]
    commands = [name for name in commands if name != os.environ.get("FAKE_QK_DROP")]
    reply({"contract": int(os.environ.get("FAKE_QK_CONTRACT", "1")), "qk": "9.9.9", "commands": commands})
if command == "agent run":
    if os.environ.get("FAKE_QK_PID"):
        Path(os.environ["FAKE_QK_PID"]).write_text(str(os.getpid()))
    time.sleep(float(os.environ.get("FAKE_QK_SLEEP", "0")))
    if scenario == "error":
        fail()
    status = "done" if scenario == "done" else "blocked"
    spec = argv[argv.index("--spec") + 1]
    usage = {"input": 1, "cached": 0, "output": 1}
    if "FAKE_QK_COST_USD" in os.environ and not spec.startswith("codex:"):
        usage["cost_usd"] = float(os.environ["FAKE_QK_COST_USD"])
    held = Path(os.environ.get("FAKE_QK_LIMIT") or "/nonexistent")
    if held.is_file():
        limit = json.loads(held.read_text())
        if spec.split(":", 1)[0] == limit["runtime"]:
            if limit.pop("partial", False) and "--write" in argv:  # the agent edited files before the limit
                Path(argv[argv.index("--cwd") + 1], "partial.txt").write_text("half done\n")
            reason = spec + " exited 1: You've hit your limit"
            reply({"contract": 1, "status": "blocked", "reason": reason,
                   "family": "openai" if spec.startswith("codex") else "anthropic", "spec": spec, "result": None,
                   "text": "", "usage": None, "lessons": [], "events": None, "seconds": 0,
                   "limit": dict(limit, message="You've hit your limit")}, 2)
    if os.environ.get("FAKE_QK_AGENT"):  # a scripted agent acts in --cwd and replies like a real one
        import re, subprocess
        cwd = argv[argv.index("--cwd") + 1]
        mode = "write" if "--write" in argv else "read"
        proc = subprocess.run([sys.executable, os.environ["FAKE_QK_AGENT"], cwd, mode, spec], input=stdin,
                              cwd=cwd, capture_output=True, text=True)
        text = proc.stdout
        blocks = re.findall(r"```qk-result\s*\n(.*?)\n```", text, re.S)
        result = json.loads(blocks[-1]) if blocks else None
        status = result["status"] if result else "blocked"
        reply({"contract": 1, "status": status, "reason": "" if status == "done" else (proc.stderr or "no block"),
               "family": "openai" if spec.startswith("codex") else "anthropic", "spec": spec, "result": result,
               "text": text, "usage": usage, "lessons": [], "events": None,
               "seconds": 0}, 0 if status == "done" else 2)
    reply({"contract": 1, "status": status, "reason": "" if status == "done" else "fake reason",
           "family": "openai" if spec.startswith("codex") else "anthropic", "spec": spec,
           "result": {"status": status, "summary": "fake", "files": [], "evidence": [], "reason": "",
                      "findings": []} if status == "done" else None,
           "text": "fake reply", "usage": usage, "lessons": [], "events": None,
           "seconds": 0}, 0 if status == "done" else 2)
if command == "agent family":
    reply({"contract": 1, "spec": argv[2], "family": "openai" if argv[2].startswith("codex") else "anthropic"})
if command == "log run":
    if scenario == "error":
        fail()
    state = json.loads(stdin)
    reply({"contract": 1, "id": "fake-" + state["id"], "findings": len(state.get("findings", []))})
if command == "eval prepare":
    import subprocess
    root = Path(argv[3])
    workdir = root / "repo"
    (workdir / "tests").mkdir(parents=True)
    if argv[2].endswith("-review"):  # a review case: plan and diff, no task
        (root / "plan.md").write_text("# plan\n")
        reply({"contract": 1, "case": argv[2], "skill": "runner-review", "write": False, "workdir": str(workdir),
               "task": None, "plan": str(root / "plan.md"), "diff": str(root / "review.diff")})
    (workdir / "README.md").write_text("project\n")
    (workdir / "task.md").write_text("Add a greeting API and CLI.\n")
    (workdir / "tests" / "test_base.py").write_text(
        "import unittest\n\n\nclass Base(unittest.TestCase):\n    def test_ok(self):\n        pass\n")
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["-c", "user.name=e", "-c", "user.email=e@example.invalid",
                                                              "commit", "-qm", "base"]):
        subprocess.run(["git", *args], cwd=str(workdir), check=True, capture_output=True)
    reply({"contract": 1, "case": argv[2], "skill": "qk-cook", "write": True, "workdir": str(workdir),
           "task": str(workdir / "task.md"), "plan": None, "diff": None})
if command == "eval grade":
    workdir = Path(argv[3])
    final_path = Path(argv[argv.index("--final") + 1]) if "--final" in argv else None
    final = final_path.read_text() if final_path and final_path.is_file() else ""
    criteria = [{"name": "built", "passed": (workdir / "src" / "api").is_dir() and (workdir / "src" / "cli").is_dir(),
                 "detail": ""},
                {"name": "report", "passed": all(label + ":" in final for label in
                                                 ("Done", "Evidence", "Not verified", "Needs your decision")),
                 "detail": ""}]
    if scenario != "done":
        criteria.append({"name": "fake", "passed": False, "detail": "scenario"})
    reply({"contract": 1, "case": argv[2], "criteria": criteria, "passed": all(c["passed"] for c in criteria)})
if command == "show":
    reply([])
print("qk: unknown command", file=sys.stderr)
sys.exit(2)
'''


def make_repo(directory, tag="v9.9.9"):
    directory = Path(directory)
    (directory / "scripts").mkdir(parents=True)
    (directory / "scripts" / "qk.py").write_text(SCRIPT)
    identity = ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], [*identity, "commit", "-qm", "fake qk"],
                 ["tag", tag]):
        subprocess.run(["git", *args], cwd=str(directory), check=True, capture_output=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(directory), check=True, capture_output=True,
                          text=True).stdout.strip()
