# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

- `qk.lock` pins qk v0.12.0. Agent turns on omp now wait for commands they moved to a background job instead of
  ending without a result, a failure the harness inherited through `qk agent run`.

### Added

- `accept-contract JOB --reason TEXT`: once the contract's revision round is used and its review still does not
  pass, a person can accept the remaining findings and the job goes on to the red check (and, for large jobs, the
  contract gate). The reason, time and accepted findings are kept; a new `contract.review` clears the acceptance.
- Opt-in `authorize JOB --path ... --budget-usd ... --turn-estimate-usd ... --gh-user ...` and `revoke JOB`:
  one durable user grant for a job's scope/models, soft spending checks and PR publication, with a read-only
  publisher preflight before the grant is saved. Stage records distinguish delegated actions from human
  approvals; manual gates remain the default.
- Delegated model turns persist pending usage before invocation, count reported failed-turn costs, reserve
  concurrent estimates, and stop on unresolved interrupted costs or missing non-Codex USD instead of treating
  them as free. Resuming never resets spend; this is not a hard budget cap.
- Delegated publication scopes Git/gh credentials to the approved account/repository/branch, projects a concise
  evidence body, and checks reported CI and registered GitHub Actions runs for the exact reviewed head with
  `pr.check`. CI failures/pending/head drift do not report completion or grant a post-PR fix loop. The runner
  needs no live chat to advance stages and never merges.

## [0.1.0] - 2026-11-25

First release.

### Added

- `qk.lock` pins qk by repository, tag, commit and CLI contract version; `harness/qk_cli.py` is the only place
  that runs qk and refuses a moved tag, a changed checkout or another contract version.
- Job state, policy and eight capabilities (`plan.write`, `contract.write`, `contract.review`, `build.run`,
  `verify.run`, `review.run`, `integrate.run`, `pr.open`), each checking its own preconditions; `start`,
  `status`, `approve`, `run`/`resume` and `abort`.
- Contract first: tests that define done, reviewed by another model family, one revision round, shown red before
  any build; `--no-contract` to skip it.
- Two tasks built and reviewed at once (`--sequential` for one), per-task roles, ownership enforced in code.
- Integration onto `qk/<slug>`, one fix round per task and for the integration (located problems only),
  `--no-task-review`, a push-permission check before `pr.open`.
- Stopped jobs recorded in qk's evidence ledger with `qk log run`; resume that clears stale running tasks and never
  repeats a commit or a pull request.
- `qk-harness eval CASE` measures the harness on qk's building eval cases.
- `bin/qk-harness` command that works through a symlink; guide and measurements in English and Vietnamese.
- Usage limits: a turn qk reports as stopped by a runtime's usage limit waits for the reset instead of failing the
  job or using a fix round; tasks on other runtimes go on. `run --until HH:MM` (or `--wait-limits`) sleeps until
  the reset and carries on, with macOS notifications; `status` and the ledger record show the limit. Needs qk
  v0.10.0, now pinned in `qk.lock`.
- `grant-fix JOB TARGET [--reason TEXT]`: a person gives a task or the integrated branch one more fix round after
  its own is used; refused unless a fix round could take what is still wrong. The job's log keeps the decision.
- `status` without a job lists unfinished jobs as JSON, or with `--line` as one status line each, for runtime
  adapters.
- `eval --no-task-review` measures a case with only the integrated review.
- `status JOB` lists the serious review findings (`target`, `severity`, `file`, `line`, `claim`) of the contract, a
  task or the integrated branch when they are why a person has to decide, so nobody opens `state.json` to read
  them.
- `eval` keeps `dir` on the invocation directory for a single run, and a grade with no criteria no longer counts
  as passed.

[Unreleased]: https://github.com/dakiquang/qk-harness/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/dakiquang/qk-harness/releases/tag/v0.1.0
