"""CLI behaviour: rendering, exit codes, JSON output, stdin."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claimcheck import cli
from claimcheck.claims import BOILERPLATE

GOLDEN = Path(__file__).parent / "fixtures" / "golden"


def run_cli(capsys, *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def golden(name: str) -> str:
    return str(GOLDEN / name)


# --------------------------------------------------------------------------
# exit codes
# --------------------------------------------------------------------------


def test_clean_run_exits_zero(capsys):
    code, out, _ = run_cli(
        capsys,
        "--diff", golden("honest_fix.diff"),
        "--report", golden("honest_fix.report.md"),
        "--test-log", golden("passing.log"),
    )
    assert code == cli.EXIT_CLEAN
    assert "verdict: CLEAN" in out


def test_refuted_claim_exits_with_the_contradiction_code(capsys):
    code, out, _ = run_cli(
        capsys,
        "--diff", golden("honest_fix.diff"),
        "--report", golden("honest_fix.report.md"),
        "--test-log", golden("failing.log"),
    )
    assert code == cli.EXIT_CONTRADICTION
    assert "REFUTED" in out
    assert "WORST: CONTRADICTION" in out


def test_fail_on_suspicious_catches_a_weaker_signal(capsys):
    code, _, _ = run_cli(
        capsys,
        "--diff", golden("tests_only.diff"),
        "--report", golden("tests_only.report.md"),
        "--test-log", golden("passing.log"),
        "--fail-on", "suspicious",
    )
    assert code != cli.EXIT_CLEAN


def test_fail_on_never_suppresses_the_gate(capsys):
    code, _, _ = run_cli(
        capsys,
        "--diff", golden("tests_only.diff"),
        "--report", golden("tests_only.report.md"),
        "--test-log", golden("passing.log"),
        "--fail-on", "never",
    )
    assert code == cli.EXIT_CLEAN


def test_missing_file_is_a_usage_error(capsys):
    code, _, err = run_cli(capsys, "--diff", "/nonexistent/path.diff", "--report", golden("honest_fix.report.md"))
    assert code == cli.EXIT_USAGE
    assert "not found" in err


def test_no_inputs_is_a_usage_error(capsys):
    code, _, err = run_cli(capsys)
    assert code == cli.EXIT_USAGE
    assert "at least one" in err


# --------------------------------------------------------------------------
# output shape
# --------------------------------------------------------------------------


def test_json_output_is_machine_readable(capsys):
    code, out, _ = run_cli(
        capsys,
        "--diff", golden("honest_fix.diff"),
        "--report", golden("honest_fix.report.md"),
        "--test-log", golden("passing.log"),
        "--json",
    )
    payload = json.loads(out)
    assert payload["clean"] is True
    assert payload["worst"] == "info"
    assert payload["tests"]["passed"] == 43
    assert payload["diff"]["test_files"] == ["tests/test_pagination.py"]
    # The changelog is diff-anchored, so anchors carry a file and a line range.
    assert payload["changelog"]
    assert all(":" in entry["anchor"] for entry in payload["changelog"])
    assert all({"kind", "verdict", "reason"} <= set(c) for c in payload["claims"])


def test_json_reports_refutation_details(capsys):
    _, out, _ = run_cli(
        capsys,
        "--diff", golden("honest_fix.diff"),
        "--report", golden("honest_fix.report.md"),
        "--test-log", golden("failing.log"),
        "--json",
    )
    payload = json.loads(out)
    assert payload["refuted_count"] >= 1
    refuted = [c for c in payload["claims"] if c["verdict"] == "refuted"]
    assert refuted
    assert any("2 failed" in c["reason"] for c in refuted)


def test_clean_run_still_prints_its_notes(capsys):
    """A clean verdict must not hide informational findings."""

    _, out, _ = run_cli(
        capsys,
        "--diff", golden("honest_fix.diff"),
        "--report", golden("honest_fix.report.md"),
        "--test-log", golden("passing.log"),
    )
    assert "verdict: CLEAN" in out
    assert "note(s)" in out
    assert "INFO" in out


def test_no_findings_prints_none(capsys):
    _, out, _ = run_cli(capsys, "--diff", "", "--report", "", "--test-log", golden("passing.log"))
    assert "findings:" in out
    assert "none" in out


def test_verbose_shows_reasons_for_verified_claims(capsys):
    _, quiet, _ = run_cli(
        capsys,
        "--diff", golden("honest_fix.diff"),
        "--report", golden("honest_fix.report.md"),
        "--test-log", golden("passing.log"),
    )
    _, loud, _ = run_cli(
        capsys,
        "--diff", golden("honest_fix.diff"),
        "--report", golden("honest_fix.report.md"),
        "--test-log", golden("passing.log"),
        "-v",
    )
    assert len(loud) > len(quiet)


def test_stdin_dash_is_accepted(capsys, monkeypatch):
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("All tests pass.\n"))
    code, out, _ = run_cli(capsys, "--report", "-", "--diff", golden("honest_fix.diff"), "--test-log", golden("passing.log"))
    assert code in {cli.EXIT_CLEAN, cli.EXIT_CONTRADICTION}
    assert "claim audit" in out


def test_test_exit_code_alone_can_refute(capsys):
    """A terse log plus a failing exit status is enough to refute the claim."""

    code, out, _ = run_cli(
        capsys,
        "--diff", golden("honest_fix.diff"),
        "--report", golden("honest_fix.report.md"),
        "--test-exit-code", "1",
    )
    assert code == cli.EXIT_CONTRADICTION
    assert "REFUTED" in out


def test_test_exit_code_zero_supports_the_claim(capsys):
    code, out, _ = run_cli(
        capsys,
        "--diff", golden("honest_fix.diff"),
        "--report", golden("honest_fix.report.md"),
        "--test-exit-code", "0",
    )
    assert code == cli.EXIT_CLEAN


# --------------------------------------------------------------------------
# regression: re.VERBOSE turned the heading alternative into a bare ^
# --------------------------------------------------------------------------


def test_boilerplate_pattern_does_not_match_every_line():
    """An unescaped # in a VERBOSE pattern comments out the rest of the line."""

    assert BOILERPLATE.match("# Task: fix the pagination off-by-one") is None
    assert BOILERPLATE.match("Fixed the issue in src/pagination.py. All tests pass.") is None
    assert BOILERPLATE.match("bounds with the slice. Added a regression test.") is None
    # It must still catch real filler.
    assert BOILERPLATE.match("To summarize what I did:") is not None
    assert BOILERPLATE.match("Overall this should resolve the issue.") is not None
    assert BOILERPLATE.match("## Summary") is not None
