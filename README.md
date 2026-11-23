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

## How a job moves

A plan lays out the work in a ```` ```qk-harness ```` block: `contract` names the test paths the contract may write, and each task owns repository paths no other task (and not the contract) owns and lists the commands that check it alone; the plan's ```` ```qk-verify ```` block checks the whole result. Agents see only their own worktree and a shared directory with the plan and diffs, never another worktree. Then:

1. `plan.write`, then a person runs `approve <job> plan`.
2. `contract.write` writes tests that define done and commits them on `qk/<slug>`; `contract.review` has a model from another family review them; `verify.run <job> integration --expect-red` must see them fail before any build.
3. `build.run <job> <task>` builds each task on its own branch `qk/<slug>--<task>`. A worker that commits, switches branch or changes a path it does not own is refused. `verify.run` and `review.run` check each task; a task gets at most one `--fix` round.
4. `integrate.run` merges into `qk/<slug>` exactly the task commits that were checked and reviewed; a task branch that moved, or a conflict, stops the job. The integrated branch is checked and reviewed again.
5. `pr.open` pushes `qk/<slug>` and opens one pull request. Nothing merges it.

A medium job has one gate, the plan. A large job also stops after the contract, the build and the integration, and `pr.open` waits for the last approval. `status <job>` prints the next step.

## Use

```bash
python3 -m harness doctor
python3 -m harness start --repo PATH --task "..." [--size medium|large]
python3 -m harness status JOB
```

The state directory is `$QK_HARNESS_STATE_DIR`, else `$XDG_STATE_HOME/qk-harness`, else `~/.local/state/qk-harness`. Python 3.9+ and `git`; no other dependencies.

## Test

```bash
python3 -m unittest discover -s tests
```

The tests use a stand-in qk (`tests/fake_qk.py`) that answers each contract command; they need no network and run no agent.
