"""Record a stopped job in qk's evidence ledger through `qk log run`, so it shows in `qk show`, `qk stats` and
`qk brief` like a qk-run run. The ledger is qk's; the harness only hands it a stopped run in qk's documented shape.
"""

import os

from harness import __version__, capabilities, gitops, state


# Capability -> the qk-run stage name qk's ledger keeps usage under (other names are dropped by qk).
STAGES = {"plan.write": "planning", "contract.write": "implementing", "contract.review": "reviewing",
          "build.run": "implementing", "verify.run": "verifying", "review.run": "reviewing",
          "integrate.run": "publishing", "pr.open": "publishing"}


def outcome(job, step):
    """`done`, `blocked` or `aborted` for a job that has stopped, or None while it only waits at a gate."""
    if job.get("aborted"):
        return "aborted"
    if step.startswith("done:"):
        return "done"
    if step.startswith("a person decides") or job["log"] and job["log"][-1]["status"] == "blocked":
        return "blocked"
    return None


def run_state(job, stage):
    """The job in the shape `qk log run` reads (qk's CLI contract, version 1)."""
    integration = job["integration"]
    tasks = job["tasks"].values()
    impl = sorted({job["impl"]} | {task.get("impl_spec", job["impl"]) for task in tasks})
    review = sorted({job["review"]} | {task.get("review_spec", job["review"]) for task in tasks})
    usage = {}
    for entry in job.get("usage", []):
        name = STAGES.get(entry.get("capability"), "implementing" if entry.get("write") else "reviewing")
        numbers = entry.get("usage") or {}
        usage.setdefault(name, []).append(dict({"spec": entry["spec"]},
                                               **{key: numbers[key] for key in ("input", "cached", "output",
                                                                                "cost_usd") if key in numbers}))
    findings = [finding for finding in (integration.get("review") or {}).get("findings", [])
                if isinstance(finding, dict)]
    data = {
        "id": job["id"], "stage": stage, "repo": job["repo"], "task": job["task"], "task_kind": "",
        "impl": ", ".join(impl), "review": ", ".join(review),
        "commit": integration.get("commit") or "", "pr_url": job["pr_url"] or "",
        "verify_results": [{"exit": check["exit"]} for check in (integration["verify"] or {}).get("results", [])],
        "usage": usage, "findings": findings, "addressed": [],
        "fix_rounds": sum(task.get("fix_rounds", 0) for task in tasks) + integration.get("fix_rounds", 0),
        "ledger_revision": job.get("ledger_revision", 0) + 1,
    }
    if integration.get("commit"):
        files = gitops.git(job["repo"], "diff", "--name-only", job["base_sha"], integration["commit"]).split()
        added = deleted = 0
        for line in gitops.git(job["repo"], "diff", "--numstat", job["base_sha"], integration["commit"]).splitlines():
            parts = line.split("\t")
            if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
                added, deleted = added + int(parts[0]), deleted + int(parts[1])
        data["diff_stat"] = {"files": files, "added": added, "deleted": deleted}
    if stage == "blocked":
        last = next((entry for entry in reversed(job["log"]) if entry["status"] in ("blocked", "refused")), None)
        data["blocked_at"] = STAGES.get(last["capability"], "reviewing") if last else "reviewing"
    return data


def record(job_id, qk, step):
    """Log the job if it has stopped and this state was not logged yet; {"id"} or {"error"}, or None."""
    with state.locked(job_id) as job:
        stage = None if job.get("eval") else outcome(job, step)
        if stage is None:
            return None
        data = run_state(job, stage)
        if job.get("logged") == [stage, len(job["log"])]:
            return None  # nothing happened since the last record
        reply = qk.log_run(data, f"qk-harness {__version__}", state.job_dir(job_id))
        if reply.code != 0 or not isinstance(reply.data, dict):
            return {"error": reply.stderr.strip()[-300:] or "qk log run failed"}
        job["ledger_revision"] = data["ledger_revision"]
        job["logged"] = [stage, len(job["log"])]
        return {"id": reply.data["id"], "stage": stage}


def recover(job_id):
    """Before a resume: clear tasks left `running` by a process that is gone (or by this process before the
    loop started, so no turn of it is still running), and say which."""
    cleared = []
    with state.locked(job_id) as job:
        for name, task in job["tasks"].items():
            running = task.get("running")
            if not running:
                continue
            try:
                pid = int(running.get("pid", 0))
                os.kill(pid, 0)
                alive = pid > 0 and pid != os.getpid()
            except (OSError, ValueError):
                alive = False
            if not alive:
                del task["running"]
                cleared.append(name)
        if cleared:
            capabilities.record(job, "resume", "done",
                                "cleared tasks left running by a stopped process: " + ", ".join(cleared))
    return cleared
