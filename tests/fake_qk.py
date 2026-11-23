"""A stand-in qk for tests: answers each contract command with JSON shaped as qk contract 1 documents it.

`make_repo(directory)` writes it as `scripts/qk.py` in a new Git repository, commits it, tags the commit and returns
the commit id. Behaviour comes from environment variables:

- FAKE_QK_CONTRACT: the contract number `qk contract` reports (default 1)
- FAKE_QK_DROP: a contract command to leave out of the command list
- FAKE_QK_SCENARIO: `done`, `blocked` or `error` for agent run, log run and eval grade (default done)
- FAKE_QK_LOG: a file that receives one JSON line per call: argv and stdin
- FAKE_QK_SLEEP: seconds agent run waits before answering; FAKE_QK_PID: file that receives its process id
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
    reply({"contract": 1, "status": status, "reason": "" if status == "done" else "fake reason",
           "family": "openai" if spec.startswith("codex") else "anthropic", "spec": spec,
           "result": {"status": status, "summary": "fake", "files": [], "evidence": [], "reason": "",
                      "findings": []} if status == "done" else None,
           "text": "fake reply", "usage": {"input": 1, "cached": 0, "output": 1}, "lessons": [], "events": None,
           "seconds": 0}, 0 if status == "done" else 2)
if command == "agent family":
    reply({"contract": 1, "spec": argv[2], "family": "openai" if argv[2].startswith("codex") else "anthropic"})
if command == "log run":
    if scenario == "error":
        fail()
    state = json.loads(stdin)
    reply({"contract": 1, "id": "fake-" + state["id"], "findings": len(state.get("findings", []))})
if command == "eval prepare":
    workdir = Path(argv[3]) / "repo"
    workdir.mkdir(parents=True)
    reply({"contract": 1, "case": argv[2], "skill": "qk-cook", "write": True, "workdir": str(workdir),
           "task": str(workdir / "task.md"), "plan": None, "diff": None})
if command == "eval grade":
    passed = scenario == "done"
    reply({"contract": 1, "case": argv[2], "criteria": [{"name": "fake", "passed": passed, "detail": ""}],
           "passed": passed})
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
