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

A run that claims a fix while only touching tests — the pattern that produces a
false "passed":

```
$ claimcheck --diff run.diff --report report.md --test-log pytest.log
diff: 1 file(s), +1/-1 lines, 1 test file(s)
report: 9 words, 2 sentences
tests:  12 passed

changelog (diff-anchored):
  [test] tests/test_app.py:L1-2  +1/-1

claim audit:
  REFUTED      Fixed the bug in f()
               -> no source file changed — the diff only touches tests/test_app.py
  verified     All tests pass.

findings:
  [CONTRADICTION ] only tests changed
                   The diff touches test files and no source files. Nothing in
                   the product changed, so any 'fixed' or 'passing' claim
                   describes the checks, not the code.
                   · tests/test_app.py
  [SUSPICIOUS    ] test files edited in this run
                   1 test file(s) changed: tests/test_app.py (modified, +1/-1)
                   · tests/test_app.py: +1/-1 assertions

verdict: WORST: CONTRADICTION (1 claim(s) refuted by evidence) | 2 non-info finding(s)
```

A clean run of the same fixtures, where the fix touched source and the log
confirms it:

```
$ claimcheck --diff honest.diff --report honest.md --test-log passing.log
diff: 2 file(s), +4/-1 lines, 1 test file(s)
report: 49 words, 7 sentences
tests:  43 passed

changelog (diff-anchored):
  [fix] src/pagination.py:def paginate(items, page, size=DEFAULT_SIZE): L12-18  +1/-1 (def paginate(items, page, size=DEFAULT_SIZE):)
  [test] tests/test_pagination.py:def test_last_page(): L25-33  +3/-0 (def test_last_page():)

claim audit:
  verified     Fixed the issue in src/pagination.py.
  verified     All tests pass.

findings:
  [INFO          ] test files edited in this run
                   1 test file(s) changed: tests/test_pagination.py (modified, +3/-0)

verdict: CLEAN | 0 non-info finding(s), 1 note(s)
```

## GitHub Action

`action.yml` is a composite action. It resolves the diff itself (the PR base
diff, or `HEAD~1..HEAD`), runs the audit, writes the result to the job summary,
and can post it as a PR comment.

```yaml
- uses: actions/checkout@v4
  with: { fetch-depth: 0 }

- name: Run the tests
  id: tests
  continue-on-error: true
  run: pytest -q 2>&1 | tee pytest.log

- uses: ./claimcheck
  with:
    report: AGENT_REPORT.md
    test-log: pytest.log
    test-exit-code: ${{ steps.tests.outcome == 'success' && '0' || '1' }}
    fail-on: suspicious
    post-comment: "true"
```

Inputs: `report`, `diff`, `test-log`, `test-exit-code`, `fail-on`,
`working-directory`, `python-version`, `post-comment`. Outputs: `worst`,
`clean`. The audit step reports the finding rather than crashing on it, so the
job fails with the audit summary attached instead of an opaque exit code.

## Tests

67 tests, all offline and deterministic. `tests/fixtures/golden/` holds one
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
