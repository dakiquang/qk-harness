"""qk-harness command line. Each capability is one command; `next` in every reply says what to run after it."""

import argparse
import json
from pathlib import Path
import sys

from harness import __version__, capabilities, coordinator, evaluate, gitops, policy, qk_cli, state


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
    abort = commands.add_parser("abort", help="stop a job for good; branches and worktrees stay for inspection")
    abort.add_argument("job")
    abort.add_argument("--reason", default="")
    measure = commands.add_parser("eval", help="measure the harness on one of qk's eval cases (runs agents)")
    measure.add_argument("case")
    measure.add_argument("--impl", default=DEFAULT_IMPL)
    measure.add_argument("--review", default=DEFAULT_REVIEW)
    measure.add_argument("--repeat", type=positive_int, default=1, help="number of sequential runs (default: 1)")
    measure.add_argument("--no-contract", action="store_true", help="measure without the contract stage")
    status = commands.add_parser("status", help="print a job and its next step")
    status.add_argument("job")
    approve = commands.add_parser("approve", help="record a person's approval of one stage")
    approve.add_argument("job")
    approve.add_argument("stage", choices=("plan", "contract", "build", "integrate"))
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
    return {"id": job["id"], "size": job["size"], "repo": job["repo"], "base": job["base"], "task": job["task"],
            "impl": job["impl"], "review": job["review"], "approvals": sorted(job["approvals"]),
            "plan": job["plan"]["file"] if job["plan"] else None, "tasks": list(job["tasks"]),
            "integration": job["integration"]["branch"], "pr_url": job["pr_url"],
            "limits": {name: {"reset_at": limit.get("reset_at"), "until": policy.until(limit)}
                       for name, limit in (job.get("limits") or {}).items()},
            "next": policy.next_step(job)}


def main(argv=None):
    args = parser().parse_args(argv)
    args.lock = args.lock or args.root_lock or str(LOCK)
    try:
        if args.command == "status":
            reply = summary(state.load(args.job))
        elif args.command == "approve":
            reply = capabilities.approve(args.job, args.stage)
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
                                           contract=not args.no_contract)
            elif args.command in ("run", "resume"):
                deadline = coordinator.deadline_at(args.until) if args.until else None
                wait = args.wait_limits if args.wait_limits is not None else deadline is not None
                reply = coordinator.run(args.job, qk, wait_limits=wait, deadline=deadline)
            else:
                reply = capabilities.run(args.job, args.command, qk, getattr(args, "target", None),
                                         expect_red=getattr(args, "expect_red", False), fix=getattr(args, "fix", False))
    except (qk_cli.QkError, state.JobError, capabilities.Blocked, gitops.GitError, evaluate.EvalError,
            policy.Refused) as error:
        print(f"qk-harness: {error}", file=sys.stderr)
        return 2
    print(json.dumps(reply, ensure_ascii=False))
    if args.command == "eval":
        return 0 if all(run["stopped"] in ("waiting", "done") for run in reply["runs"]) else 2
    if "stopped" in reply:  # the loop: waiting at a gate and done are normal ends
        return 0 if reply["stopped"] in ("waiting", "done") else 2
    return 0 if reply.get("status", "done") == "done" else 2


if __name__ == "__main__":
    sys.exit(main())
