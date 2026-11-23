"""Measure the harness on one of qk's eval cases, graded exactly as qk grades its own runs.

`qk eval prepare` builds the case repository, a medium job runs on it, the plan gate is approved by the eval
itself (recorded as such), the job stops after the integrated branch is reviewed instead of opening a pull
request, and `qk eval grade` grades the result. The final reply given to the grader is the harness's own report,
written from the job state; the harness has no other final message.
"""

import json
from pathlib import Path
import time

from harness import capabilities, coordinator, policy, state
from harness.qk_cli import state_root


class EvalError(Exception):
    """The case cannot be measured with the harness."""


def report(job):
    """Four lines a person reads first: what was done, the evidence, what was not verified, what they decide."""
    tasks = job["tasks"]
    built = [name for name, task in tasks.items() if task["commit"]]
    integration = job["integration"]
    exits = [check["exit"] for check in (integration["verify"] or {}).get("results", [])]
    findings = (integration["review"] or {}).get("findings", [])
    step = policy.next_step(job)
    return "\n".join([
        f"Done: {len(built)}/{len(tasks)} tasks built ({', '.join(built) or 'none'}); "
        f"{'merged into ' + integration['branch'] if integration['merged'] else 'not integrated'}.",
        f"Evidence: contract red before build: {(job['contract'] or {}).get('red')}; integrated checks exit "
        f"{exits or 'not run'}; integrated review findings: {len(findings)}.",
        "Not verified: anything outside the plan's checks and the contract tests; "
        f"{len(policy.serious(integration['review']))} serious review findings remain.",
        f"Needs your decision: {step}",
    ]) + "\n"


def totals(job):
    """Token and time totals of every agent turn; cost only from runtimes that report it."""
    total = {"turns": 0, "input": 0, "cached": 0, "output": 0, "cost_usd": 0.0, "seconds": 0, "unreported": 0}
    for entry in job.get("usage", []):
        total["turns"] += 1
        total["seconds"] += entry.get("seconds") or 0
        usage = entry.get("usage")
        if not usage:
            total["unreported"] += 1
            continue
        for key in ("input", "cached", "output"):
            total[key] += usage.get(key, 0)
        total["cost_usd"] += usage.get("cost_usd", 0)
    total["tokens"] = total["input"] + total["output"]
    return total


def baseline(qk, case):
    """The case's entries in the pinned qk's evals/baseline.json, keyed by runtime spec."""
    path = qk.root / "evals" / "baseline.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {spec: cases[case] for spec, cases in data.items() if isinstance(cases, dict) and case in cases}


def run_eval(case, qk, impl, review):
    """Measure one case; the result is also written next to the run as result.json."""
    root = state_root() / "evals" / f"{case}-{time.strftime('%Y%m%d-%H%M%S')}"
    prepared = qk.eval_prepare(case, root)
    data = prepared.data if isinstance(prepared.data, dict) else {}
    if prepared.code != 0 or "workdir" not in data:
        raise EvalError(f"qk eval prepare {case} failed: {prepared.stderr.strip()[-300:]}")
    if not data.get("task"):
        raise EvalError(f"{case} is a review case; the harness measures cases that build something")
    task = Path(data["task"]).read_text(encoding="utf-8").strip()
    job = state.new_job(data["workdir"], task, "medium", "main", impl, review)
    with state.locked(job["id"]) as locked:
        locked["eval"] = {"case": case, "dir": str(root)}
    started = time.monotonic()
    reply = coordinator.run(job["id"], qk)
    if reply.get("next") == f"approve {job['id']} plan":
        with state.locked(job["id"]) as locked:  # the eval stands in for the person at the plan gate
            policy.check_approval(locked, "plan")
            locked["approvals"]["plan"] = {"at": capabilities.now(), "plan_sha256": locked["plan"]["sha256"],
                                           "by": "eval"}
            capabilities.record(locked, "approve plan", "done", "approved by the eval run")
        reply = coordinator.run(job["id"], qk)
    seconds = int(time.monotonic() - started)
    job = state.load(job["id"])
    graded_tree = job["integration"]["worktree"] if job["integration"]["merged"] else data["workdir"]
    final = root / "final.md"
    final.write_text(report(job), encoding="utf-8")
    events = root / "events.jsonl"
    with open(events, "w", encoding="utf-8") as sink:
        for entry in job.get("usage", []):
            path = Path(entry["events"])
            if path.is_file():
                sink.write(path.read_text(encoding="utf-8"))
    graded = qk.eval_grade(case, graded_tree, final=final, events=events)
    grade = graded.data if isinstance(graded.data, dict) else {}
    if graded.code != 0 or "criteria" not in grade:
        raise EvalError(f"qk eval grade {case} failed: {graded.stderr.strip()[-300:]}")
    result = {"case": case, "job": job["id"], "impl": impl, "review": review, "stopped": reply.get("stopped"),
              "reason": reply.get("reason", ""), "next": reply.get("next"), "graded": graded_tree,
              "criteria": {item["name"]: item["passed"] for item in grade["criteria"]},
              "details": {item["name"]: item["detail"] for item in grade["criteria"] if not item["passed"]},
              "passed": grade["passed"], "seconds": seconds, "totals": totals(job),
              "baseline": baseline(qk, case)}
    (root / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    result["dir"] = str(root)
    return result
