# qk-harness

A personal agent harness: it runs one piece of work that has several tasks (a contract that must fail first, workers on separate worktrees, integration, a review by a different model family, at most one fix round, one pull request) on top of [qk](https://github.com/dakiquang/qk). It never merges.

qk-harness depends on qk one way. It uses only qk's versioned [CLI contract](https://github.com/dakiquang/qk/blob/v0.9.0/docs/en/cli-contract.md), never imports qk's Python modules, and runs the qk pinned in [`qk.lock`](qk.lock), not the one installed on the machine.

## Status

Early work. What exists today:

- `qk.lock`: the qk repository, tag, commit and contract version this harness runs.
- `harness/qk_cli.py`: the only place that runs qk. It fetches the locked tag into the harness state directory, refuses to run when the tag points at another commit, when the checkout moved or changed, or when `qk contract --json` does not report contract 1 with the commands the harness needs. A call past its timeout sends qk SIGTERM so qk stops its agent.
- `python3 -m harness doctor`: fetch and check the locked qk, and print what was found.
- Job state (`harness/state.py`): one JSON file per job in the state directory, locked while a step runs and saved by atomic replace.
- Policy (`harness/policy.py`): the preconditions of every capability and the gates of each job size, read from the job state alone. A capability called too early is refused with the reason and the next command, whether a person or a coordinator calls it.
- Eight capabilities (`harness/capabilities.py`), each one command: `plan.write`, `contract.write`, `contract.review`, `build.run`, `verify.run`, `review.run`, `integrate.run`, `pr.open`. Plus `start`, `status` and `approve`.
- The coordinator (`harness/coordinator.py`, `run`/`resume`): runs each next step until a gate, a blocked step or the end. It has no rules of its own; every step is the capability a person could call by hand. `abort` stops a job for good and lists the branches and worktrees it leaves.

## How a job moves

A plan lays out the work in a ```` ```qk-harness ```` block: `contract` names the test paths the contract may write, and each task owns repository paths no other task (and not the contract) owns and lists the commands that check it alone; the plan's ```` ```qk-verify ```` block checks the whole result. Agents see only their own worktree and a shared directory with the plan and diffs, never another worktree. Then:

1. `plan.write`, then a person runs `approve <job> plan`.
2. `contract.write` writes tests that define done and commits them on `qk/<slug>`; `contract.review` has a model from another family review them; a failed review gets one `contract.write --fix` round from the review's findings, then a person decides; `verify.run <job> integration --expect-red` must see them fail before any build. A test that asks for behaviour the plan does not state counts as an important finding.
3. `build.run <job> <task>` builds each task on its own branch `qk/<slug>--<task>`; `run` builds and reviews up to two tasks at once (`start --sequential` for one at a time), each turn in its own qk process, while checks still run one at a time. A task may name its own `impl` and `review` in the plan; every reviewer must come from another model family than the implementer it checks, and this is checked before any worker runs. A worker that commits, switches branch or changes a path it does not own is refused. `verify.run` and `review.run` check each task; a task gets at most one `--fix` round.
4. `integrate.run` merges into `qk/<slug>` exactly the task commits that were checked and reviewed; a task branch that moved, or a conflict, stops the job. The integrated branch is checked and reviewed again, and gets one `build.run <job> integration --fix` round of its own. A fix round, for a task or the integration, only takes failed checks and review findings that name a file and a line; anything else waits for a person. `start --no-task-review` reviews only the integrated branch.
5. `pr.open` checks that the active `gh` account can push to the repository (`GH_TOKEN` selects another account), pushes `qk/<slug>` and opens one pull request. Nothing merges it.

`start --no-contract` (and `eval --no-contract`) skips step 2: tasks branch from the job's base and the plan's checks define done. When a job stops (done, blocked, aborted, or waiting for a person's decision, but not at an approval gate), `run` records it in qk's evidence ledger with `qk log run --producer "qk-harness <version>"`, so it shows in `qk show`, `qk stats` and `qk brief`; eval jobs are not recorded. `resume` first clears tasks left running by a process that is gone, refuses to build over a commit the job never recorded, and reuses a pull request that already exists. A medium job has one gate, the plan. A large job also stops after the contract, the build and the integration, and `pr.open` waits for the last approval. `status <job>` prints the next step.

## Measuring the harness

`python3 -m harness eval CASE [--impl SPEC] [--review SPEC]` runs one of qk's eval cases that builds something (not a review case): `qk eval prepare` builds the case repository, a medium job runs on it, the eval approves the plan gate itself (recorded as `"by": "eval"`), the job stops after the integrated branch is reviewed instead of opening a pull request, and `qk eval grade` grades the integrated branch. The grader's final reply is the harness's own four-line report (Done, Evidence, Not verified, Needs your decision), written from the job state. `result.json` in the eval directory holds the criteria, token and time totals of every agent turn, and the case's baseline from the pinned qk. This runs real agents and costs money.

## Use

```bash
python3 -m harness doctor
python3 -m harness start --repo PATH --task "..." [--size medium|large]
python3 -m harness run JOB          # or resume; stops at each gate
python3 -m harness approve JOB plan
python3 -m harness status JOB
python3 -m harness abort JOB --reason "..."
```

The state directory is `$QK_HARNESS_STATE_DIR`, else `$XDG_STATE_HOME/qk-harness`, else `~/.local/state/qk-harness`. Python 3.9+ and `git`; no other dependencies.

## Test

```bash
python3 -m unittest discover -s tests
```

The tests use a stand-in qk (`tests/fake_qk.py`) that answers each contract command; they need no network and run no agent.
