# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

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
