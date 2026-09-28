# claimcheck

Audit what an AI coding agent **claims** against what its diff and test log
**show**.

An agent's final report is not evidence. `claimcheck` takes the three artifacts a
run actually produces — the diff, the written report, and the captured test log —
and points at the places where they disagree.

No runtime dependencies. Python 3.11+.

## Install

```sh
/opt/homebrew/bin/python3.13 -m venv .venv   # or any python3.11+
.venv/bin/pip install -e .
.venv/bin/claimcheck --help
```

`python3` on macOS is often the 3.9 system build, which has no `tomllib` and a
pip too old for editable installs — name a 3.11+ interpreter explicitly.

## Use

```sh
claimcheck --diff run.diff --report report.md --test-log pytest.log
```

```sh
claimcheck --diff run.diff --report report.md --json
```

```sh
claimcheck --diff run.diff --report report.md --fail-on suspicious   # CI gate
```

Any of the three inputs can be `-` for stdin. `--test-exit-code N` supplies the
runner's exit status when the log itself is terse; an exit code alone is enough
to refute a "tests pass" claim.

Exit codes: `0` clean, `1` suspicious, `2` contradiction (which also fires
without `--fail-on`, because a refuted claim is a failed check), `4` usage.

## What it looks for

The three failure modes below are the ones an agent's own summary cannot catch,
because in each case the summary is the thing that is wrong.

### 1. Wall-of-text reporting

A long report is not a thorough one. Prose is replaced by a per-hunk changelog
anchored to `file:line`, and the report is flagged when it runs long relative to
the size of the change.

```
changelog (diff-anchored):
  [fix] src/pagination.py:L12  +1/-1
  [test] tests/test_pagination.py:L25  +3/-0
```

### 2. Self-review blind spots

An agent reviewing its own work will not notice that it fixed the test rather
than the code. `claimcheck` reads the diff for what it changes about the tests:

- test files edited at all — informational, unless the run claims a *bugfix*
- test assertions removed — a file that loses more assertions than it gains can
  pass without the bug being fixed
- test files changed and *no* source files — nothing in the product changed, so
  a "fixed" claim describes the checks, not the code
- pass/fail flapping between rounds inside one report — the shape of a fix that
  introduced the next failure

### 3. Unearned confidence

Every checkable sentence in the report gets a verdict:

- `verified` — the log actually confirms it
- `unsupported` — no log was supplied, so it is an assertion, not a result
- `refuted` — the evidence contradicts it
- `uncheckable` — no rule can decide this (e.g. working-tree cleanliness)

A success claim with no test log is a contradiction, not a warning. So is a diff
that contradicts the stated story — the "automation ran successfully, nothing
got done" pattern of a prose report over an empty diff.

## Example

```
$ claimcheck --diff run.diff --report report.md --test-log pytest.log
diff: 2 file(s), +5/-3 lines, 1 test file(s)
report: 41 words, 4 sentences
tests:  43 passed (pytest)

changelog (diff-anchored):
  [fix] src/pagination.py:L12  +1/-1
  [test] tests/test_pagination.py:L25  +3/-0

claim audit:
  verified     Fixed the issue in src/pagination.py.
  verified     All tests pass.
  verified     Added a regression test for the empty-page case.

findings:
  [INFO          ] test files edited in this run
                   1 test file(s) changed: tests/test_pagination.py +3/-0 assertions

verdict: CLEAN | 0 non-info finding(s)
```

## Tests

56 tests, all offline and deterministic. `tests/fixtures/golden/` holds one
fixture set per failure mode, so each detection rule is pinned to a concrete
input rather than to a described behaviour.

```sh
.venv/bin/python -m pytest
```

## Design notes

- **A finding with no evidence is not emitted.** Every finding carries the file,
  line, assertion delta, or log line that produced it, so a human can check the
  checker.
- **Absence of evidence is a verdict, not a pass.** A missing test log yields
  `unsupported` — never silently `verified`.
- **Test edits are context-dependent.** Normal when the run's job is writing
  tests, suspicious when the run claims to fix a bug.
- **Pure function core.** `claimcheck.audit.audit(...)` takes strings and returns
  a `Report`; the CLI only handles I/O and exit codes.

## License

Apache-2.0 — see `LICENSE`.
