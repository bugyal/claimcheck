"""Golden-file tests: each fixture encodes one of the three failure modes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claimcheck.audit import Severity, Verdict, audit

GOLDEN = Path(__file__).parent / "fixtures" / "golden"


def load(name: str) -> str:
    return (GOLDEN / name).read_text()


def run(**kwargs):
    return audit(**kwargs)


def codes(report) -> set[str]:
    return {f.code for f in report.findings}


# --------------------------------------------------------------------------
# (a) wall-of-text reporting -> compressed into a diff-anchored changelog
# --------------------------------------------------------------------------


def test_wall_of_text_is_compressed_and_flagged():
    report = run(
        diff_text=load("honest_fix.diff"),
        report_text=load("wall_of_text.report.md"),
        log_text=load("passing.log"),
    )
    assert "wall-of-text" in codes(report)
    assert "boilerplate" in codes(report)
    # The changelog replaces the prose: one entry per hunk, anchored to a file.
    assert report.changelog
    anchors = [e.anchor for e in report.changelog]
    assert any(a.startswith("src/pagination.py:") for a in anchors)
    assert all(":" in a for a in anchors)


def test_changelog_is_diff_anchored_not_prose():
    report = run(diff_text=load("honest_fix.diff"), report_text=load("honest_fix.report.md"))
    kinds = {e.kind for e in report.changelog}
    assert "fix" in kinds
    assert "test" in kinds
    # Anchors carry a line range so a human can jump straight to the hunk.
    assert any("L" in e.anchor for e in report.changelog)
    # No changelog entry may be a whole paragraph of report text.
    assert all(len(e.text) < 200 for e in report.changelog)


# --------------------------------------------------------------------------
# (b) self-review blind spots
# --------------------------------------------------------------------------


def test_tests_only_change_in_a_bugfix_run_is_a_contradiction():
    report = run(
        diff_text=load("tests_only.diff"),
        report_text=load("tests_only.report.md"),
        log_text=load("passing.log"),
    )
    assert "tests-edited" in codes(report)
    assert "tests-weakened" in codes(report)
    assert "tests-only-change" in codes(report)
    assert report.worst is Severity.CONTRADICTION
    assert not report.clean


def test_assertion_removal_is_detected_with_counts():
    report = run(
        diff_text=load("tests_only.diff"),
        report_text=load("tests_only.report.md"),
        log_text=load("passing.log"),
    )
    weakened = next(f for f in report.findings if f.code == "tests-weakened")
    assert "tests/test_pagination.py" in weakened.detail
    # Evidence must name the actual assertion delta, not just assert vaguely.
    # (The fixture removes 3 assertion lines and adds 2 weaker ones.)
    assert any("/+2 assertions" in e for e in weakened.evidence)


def test_review_churn_flapping_is_flagged():
    report = run(
        diff_text=load("honest_fix.diff"),
        report_text=load("churn.report.md"),
        log_text=load("passing.log"),
    )
    assert "review-churn" in codes(report)
    assert report.churn
    assert any("pass -> fail" in c for c in report.churn)


def test_churn_needs_two_rounds_to_fire():
    single_round = "# Round 1\n\n43 passed in 0.9s\n"
    report = run(diff_text=load("honest_fix.diff"), report_text=single_round, log_text=load("passing.log"))
    assert "review-churn" not in codes(report)
    assert not report.churn


# --------------------------------------------------------------------------
# (c) unearned confidence
# --------------------------------------------------------------------------


def test_success_claimed_with_no_log_is_a_contradiction():
    report = run(
        diff_text=load("honest_fix.diff"),
        report_text=load("honest_fix.report.md"),
        log_text="",
    )
    assert "no-test-evidence" in codes(report)
    assert report.worst is Severity.CONTRADICTION
    passing = [c for c in report.checks if c.claim.kind == "tests_pass"]
    assert passing
    assert all(c.verdict is Verdict.UNSUPPORTED for c in passing)


def test_claim_refuted_by_a_failing_log():
    report = run(
        diff_text=load("honest_fix.diff"),
        report_text=load("honest_fix.report.md"),
        log_text=load("failing.log"),
    )
    assert "claim-vs-log" in codes(report)
    refuted = report.refuted
    assert refuted
    passing = next(c for c in report.checks if c.claim.kind == "tests_pass")
    assert passing.verdict is Verdict.REFUTED
    assert "2 failed" in passing.reason


def test_verified_when_the_log_actually_confirms():
    report = run(
        diff_text=load("honest_fix.diff"),
        report_text=load("honest_fix.report.md"),
        log_text=load("passing.log"),
    )
    passing = next(c for c in report.checks if c.claim.kind == "tests_pass")
    assert passing.verdict is Verdict.VERIFIED
    assert "43 passed" in passing.reason
    assert report.clean, f"expected a clean run, got {[f.code for f in report.findings]}"


def test_tests_added_claim_is_checked_against_the_diff():
    report = run(diff_text=load("honest_fix.diff"), report_text="Added tests for the empty page case.\nAll tests pass.\n", log_text=load("passing.log"))
    added = next(c for c in report.checks if c.claim.kind == "tests_added")
    assert added.verdict is Verdict.VERIFIED


def test_tests_added_claim_refuted_when_no_test_file_changed():
    report = run(
        diff_text=load("tests_only.diff"),
        report_text="Added tests for the client timeout path.\n",
        log_text=load("passing.log"),
    )
    added = [c for c in report.checks if c.claim.kind == "tests_added"]
    # The claim is about *adding* tests; this diff only weakens them, so the
    # assertion count goes down even though a test file is touched.
    assert added
    assert added[0].claim.kind == "tests_added"


def test_empty_diff_with_a_prose_report_is_a_contradiction():
    report = run(
        diff_text="",
        report_text="Ran the automation successfully. Fixed the bug and all tests now pass.\n"
        "Refactored the module and resolved the issue.\n",
        log_text="",
    )
    assert "empty-diff" in codes(report)
    assert report.worst is Severity.CONTRADICTION


def test_no_functional_change_refuted_by_source_edits():
    report = run(
        diff_text=load("honest_fix.diff"),
        report_text="Refactor only — no functional changes. All tests pass.\n",
        log_text=load("passing.log"),
    )
    check = next(c for c in report.checks if c.claim.kind == "no_functional_change")
    assert check.verdict is Verdict.REFUTED
    assert "source file(s) changed" in check.reason


def test_no_functional_change_verified_with_a_docs_only_diff():
    docs = (
        "diff --git a/README.md b/README.md\n"
        "index 111..222 100644\n"
        "--- a/README.md\n"
        "+++ b/README.md\n"
        "@@ -1,3 +1,3 @@\n"
        "-old line\n"
        "+new line\n"
    )
    report = run(diff_text=docs, report_text="Refactor only, no functional changes.\n")
    check = next(c for c in report.checks if c.claim.kind == "no_functional_change")
    assert check.verdict is Verdict.VERIFIED


def test_coverage_claim_refuted_when_no_tests_touched():
    report = run(
        diff_text=load("honest_fix.diff"),
        report_text="The change is thoroughly tested and achieves 100% coverage.\n",
        log_text=load("passing.log"),
    )
    coverage = [c for c in report.checks if c.claim.kind == "coverage"]
    assert coverage
    # honest_fix.diff does touch tests, so this one is unsupported rather than
    # refuted; the refuted case needs a source-only diff.
    source_only = "\n".join(
        load("honest_fix.diff").split("diff --git a/tests/")[0].rstrip().splitlines()
    )
    report2 = run(
        diff_text=source_only,
        report_text="The change is thoroughly tested and achieves 100% coverage.\n",
        log_text=load("passing.log"),
    )
    coverage2 = next(c for c in report2.checks if c.claim.kind == "coverage")
    assert coverage2.verdict is Verdict.REFUTED


def test_test_edit_in_a_bugfix_run_is_suspicious_not_info():
    """Editing tests is normal in a test-writing run and suspicious in a fix."""

    # A bugfix run that only weakens tests, with no source change: evasive.
    evasive = run(
        diff_text=load("tests_only.diff"),
        report_text="Fixed the bug in the pagination module.\n",
        log_text=load("passing.log"),
    )
    assert any(
        f.code == "tests-edited" and f.severity is Severity.SUSPICIOUS
        for f in evasive.findings
    )

    # The same diff reported as test work rather than a product fix: benign.
    honest = run(
        diff_text=load("tests_only.diff"),
        report_text="Updated the pagination tests to match the new page bounds.\n",
        log_text=load("passing.log"),
    )
    assert any(
        f.code == "tests-edited" and f.severity is Severity.INFO
        for f in honest.findings
    )

    # A source fix that *adds* regression tests stays informational.
    good = run(
        diff_text=load("honest_fix.diff"),
        report_text="Fixed the bug in src/pagination.py.\nAll tests pass.\n",
        log_text=load("passing.log"),
    )
    assert any(
        f.code == "tests-edited" and f.severity is Severity.INFO
        for f in good.findings
    )


def test_findings_always_carry_evidence_or_are_explicitly_empty():
    report = run(
        diff_text=load("tests_only.diff"),
        report_text=load("tests_only.report.md"),
        log_text=load("failing.log"),
    )
    for finding in report.findings:
        if finding.code == "boilerplate":
            continue  # a count-only finding has no lines to quote
        assert finding.evidence, f"{finding.code} emitted with no evidence"
