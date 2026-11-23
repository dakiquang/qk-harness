# qk-harness guide

[Tiếng Việt](../vi/guide.md) · [README](../../README.md) · [Measurements](measurements.md)

## The idea

The durable part of an agent harness is its commitments: who may do what, what must be true before each step, and what evidence each step leaves. The order of steps is the part that ages fastest. So qk-harness is two layers:

- **Eight capabilities**, each one command that checks its own preconditions from the job state, does one step, records it, and replies with `status`, `reason` and `next`.
- **A thin coordinator** (`run`) that only follows `next`. Calling a capability by hand gets exactly the same checks and refusals.

Three roles stay separate: agents make and check, the harness runs git, and a person decides at the gates and merges.

## A job

`qk-harness start --repo PATH --task TEXT` creates a job; nothing runs yet. Options:

| Option | Meaning |
|---|---|
| `--size medium` (default) | One gate: the plan |
| `--size large` | Gates after the plan, the contract, the build and the integration |
| `--base BRANCH` | Default: the repository's current branch |
| `--impl SPEC`, `--review SPEC` | `runtime:model` that builds and reviews; default `codex:gpt-6.1-terra` and `claude:`. They must be different model families |
| `--no-contract` | Skip the contract stage; each task writes the tests that check it |
| `--no-task-review` | Review only the integrated branch |
| `--sequential` | One task at a time (default: two at once) |

Job state lives in `$QK_HARNESS_STATE_DIR`, else `$XDG_STATE_HOME/qk-harness`, else `~/.local/state/qk-harness`: one directory per job with `state.json`, the plan, diffs, the agent turns and every worktree. Nothing is written into your repository except the branches `qk/<slug>` and `qk/<slug>--<task>`, their worktrees' registration, and the upstream of `qk/<slug>` once `pr.open` pushes it.

## The plan

`plan.write` asks the implementer to follow qk's `qk-plan` skill read-only and return a plan with two blocks:

- ```` ```qk-verify ````: shell commands that prove the whole result. A plan without one is refused: the harness only runs work it can check.
- ```` ```qk-harness ````: `{"contract": ["tests/..."], "tasks": [{"name", "owns", "verify", "impl"?, "review"?}]}`. Each task owns repository paths no other task owns and lists the commands that check it alone. Without a contract stage there is no `contract` key and each task owns its own tests.

Ownership overlaps and role families are checked when the plan is written, before any worker runs. An approval records the plan's hash; editing the plan afterwards makes every later step refuse.

## The capabilities

| Capability | What it may change | Main preconditions |
|---|---|---|
| `plan.write` | nothing | clean checkout; plan not approved |
| `contract.write [--fix]` | the integration worktree, only under the contract paths | plan approved; `--fix` once after a failed contract review |
| `contract.review` | nothing | a contract; reviewer from another family |
| `build.run TASK [--fix]` | the task's own worktree, only its paths | contract reviewed and shown red (and approved for large jobs); `--fix` once |
| `build.run integration --fix` | the integration worktree, only the tasks' paths | integrated branch failed its checks or review; once |
| `verify.run TARGET [--expect-red]` | nothing; runs one verification at a time across jobs | a built task, or the integrated branch |
| `review.run TARGET` | nothing | target passed its checks; reviewer from another family than every implementer it checks |
| `integrate.run` | the local branch `qk/<slug>` | every task built, checked and reviewed; nothing running |
| `pr.open` | the remote: pushes `qk/<slug>`, opens one pull request | integrated branch checked and reviewed; last gate approved; the active `gh` account can push |

Rules the code enforces, not the prompt:

- Workers never run git. A worker that commits, switches branch, or changes a path it does not own is refused and its changes are not committed.
- Agents see only their own worktree and a shared directory holding the plan and diffs, never another worktree.
- The contract must fail before any build (`--expect-red`); a contract that already passes stops the job.
- Integration merges exactly the commits that were checked and reviewed; a branch that moved since stops the job.
- A fix round takes only failed checks and findings that name a file and a line. Anything else waits for a person.
- A test that asks for behaviour the plan does not state is an important finding.

## Running and resuming

`qk-harness run JOB` (also `resume`) runs each next step until a gate, a blocked step, a person's decision or the end; two task turns run at once, each in its own `qk agent run` process. Before it starts, it clears tasks left `running` by a process that is gone. It never builds over a branch commit the job did not record, and `pr.open` reuses a pull request that already exists.

`approve JOB STAGE` records a person's approval. `abort JOB --reason TEXT` stops a job for good and lists the branches and worktrees it leaves for you to inspect or remove.

When a job stops (done, blocked, aborted, or waiting for a decision; not at an approval gate) it is recorded in qk's evidence ledger with `qk log run --producer "qk-harness <version>"`, so it shows in `qk show`, `qk stats` and `qk brief`. A blocked card names qk-harness as the tool that resumes it.

## Measuring

`qk-harness eval CASE [--impl SPEC] [--review SPEC] [--no-contract]` runs one of qk's eval cases that builds something: `qk eval prepare`, a medium job whose plan gate the eval approves (recorded as such), no pull request, then `qk eval grade` on the integrated branch. The grader reads the harness's own four-line report. `result.json` keeps the criteria, every turn's tokens and time, and the case's baseline. It runs real agents and costs money. Results so far: [measurements](measurements.md).

## Limits

- Real agents have been measured only on single-task cases; multi-task work has run only against the stand-in qk.
- One job runs at a time per coordinator; there is no daemon or queue.
- The harness does not merge, release, tag, delete branches or force-push, and has no capability that could.
- Runtimes: `codex`, `claude` and `omp`, as qk supports them.
