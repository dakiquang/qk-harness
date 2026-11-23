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
