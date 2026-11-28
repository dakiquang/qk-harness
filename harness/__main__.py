"""qk-harness command line. Each capability is one command; `next` in every reply says what to run after it."""

import argparse
from datetime import datetime, timedelta
import json
import os
import re
from pathlib import Path
import sys

from harness import __version__, capabilities, coordinator, delegation, diagnostics, evaluate, gitops, policy, qk_cli, state


LOCK = Path(__file__).resolve().parent.parent / "qk.lock"
DEFAULT_IMPL = "codex:gpt-6.1-terra"
DEFAULT_REVIEW = "claude:"


def positive_int(value):
    try:
        number = int(value)
        if number > 0:
            return number
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("must be a positive integer")


def parser():
    # --lock works before or after the command name; after it wins.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--lock", dest="lock", help="default: qk.lock next to the harness")
    root = argparse.ArgumentParser(prog="qk-harness", description="Run multi-task work on top of qk's CLI contract.")
    root.add_argument("--version", action="version", version=f"qk-harness {__version__}")
    root.add_argument("--lock", dest="root_lock", help="default: qk.lock next to the harness")
    commands = root.add_subparsers(dest="command", required=True)
    add = commands.add_parser

    def command_parser(name, **options):
        return add(name, parents=[common], **options)
    commands.add_parser = command_parser
    commands.add_parser("doctor", help="fetch the qk in qk.lock and check its contract")
    start = commands.add_parser("start", help="create a job; nothing runs yet")
    start.add_argument("--repo", required=True)
    start.add_argument("--task", required=True)
    start.add_argument("--size", choices=state.SIZES, default="medium")
    start.add_argument("--base", help="default: the repository's current branch")
    start.add_argument("--impl", default=DEFAULT_IMPL, help="runtime:model that writes")
    start.add_argument("--review", default=DEFAULT_REVIEW, help="runtime:model that reviews; another family")
    start.add_argument("--no-contract", action="store_true", help="skip writing and checking a contract first")
    start.add_argument("--sequential", action="store_true", help="run task turns one at a time (default: 2 at once)")
    start.add_argument("--no-task-review", action="store_true",
                       help="review only the integrated branch, not each task on its own")
    run = commands.add_parser("run", aliases=["resume"], help="run each next step until a gate, a block or the end")
    run.add_argument("job")
    run.add_argument("--wait-limits", action=argparse.BooleanOptionalAction, default=None,
                     help="sleep until a usage limit resets and carry on (default: on with --until)")
    run.add_argument("--until", metavar="HH:MM", type=clock,
                     help="never wait for a usage limit past this local time")
    authorize = commands.add_parser("authorize", help="grant one job scope, a soft USD budget and PR publication")
    authorize.add_argument("job")
    authorize.add_argument("--path", dest="paths", action="append", required=True,
                           help="allowed repository-relative file/directory; repeat for the task's full scope")
    authorize.add_argument("--budget-usd", type=float, required=True)
    authorize.add_argument("--turn-estimate-usd", type=float, required=True,
                           help="estimated USD for each non-Codex turn; not a hard cap")
    authorize.add_argument("--gh-user", required=True, help="GitHub account allowed to publish this job")
    revoke = commands.add_parser("revoke", help="revoke delegation; does not cancel an in-flight turn")
    revoke.add_argument("job")
    revoke.add_argument("--reason", default="")
    abort = commands.add_parser("abort", help="stop a job for good; branches and worktrees stay for inspection")
    abort.add_argument("job")
    abort.add_argument("--reason", default="")
    measure = commands.add_parser("eval", help="measure the harness on one of qk's eval cases (runs agents)")
    measure.add_argument("case")
    measure.add_argument("--impl", default=DEFAULT_IMPL)
    measure.add_argument("--review", default=DEFAULT_REVIEW)
    measure.add_argument("--repeat", type=positive_int, default=1, help="number of sequential runs (default: 1)")
    measure.add_argument("--no-contract", action="store_true", help="measure without the contract stage")
    measure.add_argument("--no-task-review", action="store_true", help="measure with only the integrated review")
    status = commands.add_parser("status", help="print a job and its next step")
    status.add_argument("job", nargs="?")
    formats = status.add_mutually_exclusive_group()
    formats.add_argument("--line", action="store_true")
    formats.add_argument("--json", action="store_true")
    approve = commands.add_parser("approve", help="record a person's approval of one stage")
    approve.add_argument("job")
    approve.add_argument("stage", choices=("plan", "contract", "build", "integrate"))
    approve.add_argument("--delegated", action="store_true", help="advance under an existing user grant")
    grant = commands.add_parser("grant-fix", help="give a task or the integrated branch one more fix round")
    grant.add_argument("job")
    grant.add_argument("target", help="a task name or integration")
    grant.add_argument("--reason", default="", help="why; kept in the job's log")
    accept = commands.add_parser("accept-contract",
                                 help="go on with a contract whose review still fails after its revision round")
    accept.add_argument("job")
    accept.add_argument("--reason", required=True, help="why the findings are accepted; kept in the job's log")
    for name in policy.CAPABILITIES:
        command = commands.add_parser(name, help="capability: changes " + policy.CAPABILITIES[name])
        command.add_argument("job")
        if name in ("build.run", "verify.run", "review.run"):
            command.add_argument("target", help="a task name" + ("" if name == "build.run" else ", or integration"))
        if name in ("build.run", "contract.write"):
            command.add_argument("--fix", action="store_true", help="the one fix round (contract: from its review)")
        if name == "verify.run":
            command.add_argument("--expect-red", action="store_true",
                                 help="check that the contract tests fail before any build")
    return root


def clock(text):
    try:
        hour, minute = (int(part) for part in text.split(":"))
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not HH:MM")
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise argparse.ArgumentTypeError(f"{text!r} is not HH:MM")
    return text


def summary(job):
    next_step = policy.next_step(job)
    integration = job["integration"]
    # Only a decision caused by review findings lists them; a changed plan or failing checks has another cause.
    if next_step.startswith("a person decides: the contract review did not pass"):
        targets = [("contract", job["contract"])]
    elif integration["merged"]:
        decides = next_step.startswith("a person decides: the integrated branch has serious review findings")
        targets = [("integration", integration)] if decides else []
    elif not policy.in_task_stage(job, next_step):
        targets = []  # an earlier stage, such as a changed plan, needs the person first
    else:
        targets = [(name, task) for name, task in job["tasks"].items()
                   if (policy.task_step(job, name, task) or "").startswith(
                       f"a person decides: task {name} has serious review findings")]
    findings = [{"target": name, **{key: finding.get(key) for key in ("severity", "file", "line", "claim")}}
                for name, target in targets for finding in policy.serious(target["review"])]
    result = {"id": job["id"], "size": job["size"], "repo": job["repo"], "base": job["base"], "task": job["task"],
            "impl": job["impl"], "review": job["review"], "approvals": sorted(job["approvals"]),
            "plan": job["plan"]["file"] if job["plan"] else None, "tasks": list(job["tasks"]),
            "integration": job["integration"]["branch"], "pr_url": job["pr_url"],
            "limits": {name: {"reset_at": limit.get("reset_at"), "until": policy.until(limit)}
                       for name, limit in (job.get("limits") or {}).items()},
            "next": next_step, "findings": findings}
    if job.get("diagnostic_ref"):
        result["diagnostic_ref"] = job["diagnostic_ref"]
    if job.get("recovery_required"):
        result["recovery_required"] = job["recovery_required"]
    if job.get("ci_check"):
        result["ci_check"] = job["ci_check"]
    if job.get("delegation"):
        result.update(delegation=job["delegation"], budget=delegation.costs(job), ci=job.get("ci"))
    return result


STAGES = ["plan", "contract", "tasks", "integrate", "review", "pr"]


def pid_alive(pid):
    try:
        pid = int(pid)
        if pid <= 0:
            return False
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (ValueError, TypeError, ProcessLookupError, OverflowError):
        return False


def created(job_id):
    """A job's start time from its id (local `YYYYmmdd-HHMMSS-…`), in the job log's time format."""
    try:
        return datetime.strptime(job_id[:15], "%Y%m%d-%H%M%S").astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        return None


def status_item(job):
    tasks = job["tasks"]
    count = sum(policy.task_ok(task, job) for task in tasks.values())
    integration = job["integration"]
    if job["plan"] is None:
        stage = "plan"
    elif not policy.contract_ok(job):
        stage = "contract"
    elif count != len(tasks):
        stage = "tasks"
    elif not integration["merged"]:
        stage = "integrate"
    elif not policy.green(integration["verify"]) or not policy.review_ok(integration["review"]):
        stage = "review"
    else:
        stage = "pr"
    stages = STAGES + (["ci"] if job.get("delegation") else [])
    if job.get("delegation") and job["pr_url"] and (job.get("ci") or {}).get("status") != "success":
        stage = "ci"
    next_step = policy.next_step(job)
    waiting = None
    if policy.limit_wait(next_step):
        match = re.search(r"the (.+?) usage limit ", next_step)
        if match:
            runtime = match.group(1)
            reset = policy.reset_time((job.get("limits") or {}).get(runtime) or {})
            waiting = {"limit": runtime, "until": reset.strftime("%H:%M") if reset else None}
    elif next_step.startswith("a person decides") or (
            next_step.startswith("approve ") and not next_step.endswith(" --delegated")):
        waiting = {"person": next_step}
    running = [f"{task['running']['capability']} {name}" for name, task in tasks.items()
               if task.get("running") and pid_alive(task["running"].get("pid"))]
    if job.get("ci_check") and pid_alive(job["ci_check"].get("pid")):
        running.append("pr.check")
    if not running and gitops.lock_held(state.job_dir(job["id"]) / "job.lock"):
        running = ["step"]
    # A stale PID only says the supervisor is gone, not that its writers or results are safe.
    stale = [name for name, task in tasks.items() if task.get("running") and not pid_alive(task["running"].get("pid"))]
    if stale and not running:
        waiting = {"person": f"task {stale[0]} stopped mid-step"}
        next_step = f"status {job['id']}"
    return {"source": "qk-harness", "id": job["id"], "title": job["task"][:60], "stage": stage,
            "step": stages.index(stage) + 1, "steps": len(stages),
            "since": job["log"][-1].get("at") if job.get("log") else created(job["id"]), "waiting_for": waiting,
            "next": next_step, "running": running, "tasks_ok": count, "tasks_total": len(tasks)}


def status_listing():
    items = []
    try:
        directories = sorted(state.jobs_root().iterdir())
    except OSError:
        return {"contract": 1, "items": items}
    for directory in directories:
        try:
            job = state.load(directory.name)
            if not job.get("aborted") and (not job.get("pr_url") or job.get("delegation")):
                item = status_item(job)
                if not item["next"].startswith("done"):
                    items.append(item)
        except (state.JobError, OSError, ValueError, TypeError, KeyError, IndexError, AttributeError):
            continue
    return {"contract": 1, "items": items}


def recent(item, now=None):
    """Status lines show a job that runs, waits for a usage limit, or changed in the last day; `--json` lists all."""
    if item["running"] or (item["waiting_for"] or {}).get("limit"):
        return True
    try:
        since = datetime.strptime(item["since"], "%Y-%m-%dT%H:%M:%S%z")  # the job log's own format
    except (TypeError, ValueError):
        return False
    return (now or datetime.now(since.tzinfo)) - since < timedelta(hours=24)


def status_line(item):
    prefix = f"harness {item['id'][-6:]} · "
    waiting = item["waiting_for"]
    if item["running"]:
        detail = "running: step" if item["running"] == ["step"] else \
            f"{', '.join(item['running'])} · {item['tasks_ok']}/{item['tasks_total']} tasks"
    elif waiting and "limit" in waiting:
        detail = f"{waiting['limit']} limit" + (f" until {waiting['until']}" if waiting["until"] else "")
    elif waiting:
        person = waiting["person"]
        detail = "waits: " + person.replace(item["id"] + " ", "", 1) if person.startswith("approve ") \
            else f"{person} · inspect evidence" if person.endswith("stopped mid-step") else "person decides"
    else:
        detail = "next: " + item["next"].replace(item["id"] + " ", "", 1).replace(" " + item["id"], "", 1)
    return (prefix + detail.replace("\n", " "))[:80]


def main(argv=None):
    argument_parser = parser()
    args = argument_parser.parse_args(argv)
    if args.command == "status" and args.job and (args.line or args.json):
        argument_parser.error("status --line/--json cannot be used with a job id")
    args.lock = args.lock or args.root_lock or str(LOCK)
    try:
        if args.command == "status":
            if args.job:
                reply = summary(state.load(args.job))
            else:
                reply = status_listing()
                if args.line:
                    shown = [item for item in reply["items"] if recent(item)]
                    idle = [item for item in shown if not item["running"] and not item["waiting_for"]]
                    for item in shown:
                        if item not in idle or len(idle) == 1:
                            print(status_line(item))
                    if len(idle) > 1:  # jobs not running (stopped, or never started): one line; details in --json
                        print(f"harness {len(idle)} jobs not running (qk-harness status)")
                    return 0
        elif args.command == "approve":
            reply = capabilities.approve(args.job, args.stage, delegated=args.delegated)
        elif args.command == "authorize":
            reply = delegation.authorize(args.job, args.paths, args.budget_usd, args.turn_estimate_usd, args.gh_user)
        elif args.command == "revoke":
            reply = delegation.revoke(args.job, args.reason)
        elif args.command == "grant-fix":
            reply = capabilities.grant_fix(args.job, args.target, args.reason)
        elif args.command == "accept-contract":
            reply = capabilities.accept_contract(args.job, args.reason)
        elif args.command == "abort":
            reply = coordinator.abort(args.job, args.reason)
            if reply["status"] == "done":  # the abort stands even when qk cannot record it
                try:
                    lock = qk_cli.load_lock(args.lock)
                    reply = coordinator.finish(args.job, qk_cli.Qk(qk_cli.fetch(lock), lock), reply)
                except (qk_cli.QkError, gitops.GitError, OSError) as error:
                    reply["ledger"] = {"error": str(error)}
        else:
            lock = qk_cli.load_lock(args.lock)
            qk = qk_cli.Qk(qk_cli.fetch(lock), lock)
            if args.command == "doctor":
                reply = {"qk": qk.version, "contract": qk_cli.CONTRACT, "tag": lock["tag"], "sha": lock["sha"],
                         "root": str(qk.root)}
            elif args.command == "start":
                if capabilities.family(qk, args.impl) == capabilities.family(qk, args.review):
                    raise capabilities.Blocked(f"{args.impl} and {args.review} are the same model family; the "
                                               "reviewer must come from another one")
                base = args.base or gitops.git(args.repo, "symbolic-ref", "--short", "HEAD")
                job = state.new_job(args.repo, args.task, args.size, base, args.impl, args.review,
                                    contract=not args.no_contract, parallel=1 if args.sequential else 2,
                                    task_review=not args.no_task_review)
                reply = dict(summary(job), status="done")
            elif args.command == "eval":
                if capabilities.family(qk, args.impl) == capabilities.family(qk, args.review):
                    raise capabilities.Blocked(f"{args.impl} and {args.review} are the same model family")
                reply = evaluate.run_evals(args.case, qk, args.impl, args.review, repeat=args.repeat,
                                           contract=not args.no_contract, task_review=not args.no_task_review)
            elif args.command in ("run", "resume"):
                deadline = coordinator.deadline_at(args.until) if args.until else None
                wait = args.wait_limits if args.wait_limits is not None else deadline is not None
                reply = coordinator.run(args.job, qk, wait_limits=wait, deadline=deadline)
            else:
                reply = capabilities.run(args.job, args.command, qk, getattr(args, "target", None),
                                         expect_red=getattr(args, "expect_red", False), fix=getattr(args, "fix", False))
    except (qk_cli.QkError, state.JobError, capabilities.Blocked, gitops.GitError, evaluate.EvalError,
            policy.Refused, delegation.GrantError) as error:
        print(f"qk-harness: {diagnostics.safe_text(error)}", file=sys.stderr)
        return 2
    print(json.dumps(reply, ensure_ascii=False))
    if args.command == "eval":
        return 0 if all(run["stopped"] in ("waiting", "done") for run in reply["runs"]) else 2
    if "stopped" in reply:  # the loop: waiting at a gate and done are normal ends
        return 0 if reply["stopped"] in ("waiting", "done") else 2
    return 0 if reply.get("status", "done") == "done" else 2


if __name__ == "__main__":
    sys.exit(main())
