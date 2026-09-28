"""claimcheck CLI.

    claimcheck --diff run.diff --report report.md --test-log pytest.log
    claimcheck --diff a.diff --report r.md --test-log l.txt --json
    claimcheck --diff a.diff --report r.md --fail-on suspicious   # CI gate
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .audit import Severity, Verdict, audit

EXIT_CLEAN = 0
EXIT_SUSPICIOUS = 1
EXIT_CONTRADICTION = 2
EXIT_USAGE = 4

LEVELS = {"info": Severity.INFO, "suspicious": Severity.SUSPICIOUS, "contradiction": Severity.CONTRADICTION}


class UsageError(Exception):
    """Raised for bad input, to be turned into an exit code by ``main``."""


def _read(path: str | None, label: str) -> str:
    if not path:
        return ""
    if path == "-":
        return sys.stdin.read()
    file_path = Path(path)
    if not file_path.is_file():
        # Raise rather than exit: main() owns the process exit code, which keeps
        # the whole CLI callable and assertable from a test.
        raise UsageError(f"{label} file not found: {path}")
    return file_path.read_text(errors="replace")


def _render(report, *, verbose: bool) -> str:
    lines: list[str] = []
    d = report.diff

    lines.append(
        f"diff: {len(d.files)} file(s), +{d.added}/-{d.removed} lines"
        + (f", {len(d.test_files)} test file(s)" if d.test_files else "")
    )
    lines.append(f"report: {report.claims.words} words, {report.claims.sentences} sentences")
    lines.append(f"tests:  {report.tests.summary()}")

    lines.append("")
    lines.append("changelog (diff-anchored):")
    if not report.changelog:
        lines.append("  (no changes)")
    for entry in report.changelog:
        lines.append(f"  [{entry.kind}] {entry.anchor}  {entry.text}")

    if report.checks:
        lines.append("")
        lines.append("claim audit:")
        for check in report.checks:
            lines.append(f"  {check.verdict.label:<12} {check.claim.sentence[:110]}")
            if verbose or check.verdict is not Verdict.VERIFIED:
                lines.append(f"               -> {check.reason}")

    real_findings = report.suspicious_findings
    lines.append("")
    lines.append("findings:")
    shown = sorted(report.findings, key=lambda f: -f.severity.rank)
    if not shown:
        lines.append("  none")
    for finding in shown:
        lines.append(f"  [{finding.severity.value.upper():<14}] {finding.title}")
        lines.append(f"                   {finding.detail}")
        for item in finding.evidence[:4]:
            lines.append(f"                   · {item}")

    lines.append("")
    notes = [f for f in report.findings if f.severity is Severity.INFO]
    verdict = "CLEAN" if report.clean else f"WORST: {report.worst.value.upper()}"
    refuted = len(report.refuted)
    lines.append(
        f"verdict: {verdict}"
        + (f" ({refuted} claim(s) refuted by evidence)" if refuted else "")
        + f" | {len(real_findings)} non-info finding(s)"
        + (f", {len(notes)} note(s)" if notes else "")
    )
    return "\n".join(lines)


def _to_json(report) -> dict:
    return {
        "diff": {
            "files": len(report.diff.files),
            "added": report.diff.added,
            "removed": report.diff.removed,
            "test_files": [f.path for f in report.diff.test_files],
            "source_files": [f.path for f in report.diff.source_files],
        },
        "report": {
            "words": report.claims.words,
            "sentences": report.claims.sentences,
            "boilerplate_lines": report.claims.boilerplate_lines,
        },
        "tests": {
            "runner": report.tests.runner,
            "passed": report.tests.passed,
            "failed": report.tests.failed,
            "errors": report.tests.errors,
            "skipped": report.tests.skipped,
            "exit_code": report.tests.exit_code,
            "summary": report.tests.summary(),
        },
        "changelog": [
            {"anchor": e.anchor, "text": e.text, "kind": e.kind} for e in report.changelog
        ],
        "claims": [
            {
                "kind": c.claim.kind,
                "sentence": c.claim.sentence,
                "verdict": c.verdict.value,
                "reason": c.reason,
            }
            for c in report.checks
        ],
        "findings": [
            {
                "code": f.code,
                "severity": f.severity.value,
                "title": f.title,
                "detail": f.detail,
                "evidence": f.evidence,
            }
            for f in sorted(report.findings, key=lambda f: -f.severity.rank)
        ],
        "churn": report.churn,
        "clean": report.clean,
        "worst": report.worst.value,
        "refuted_count": len(report.refuted),
    }


def _read_checked(args, label: str) -> str:
    """Read one input, letting UsageError propagate to main's handler."""

    return _read(getattr(args, label), label.replace("_", " "))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claimcheck",
        description="Audit an agent run's report against its own diff and test log.",
    )
    parser.add_argument("--version", action="version", version=f"claimcheck {__version__}")
    parser.add_argument("--diff", help="unified diff file ('-' for stdin)")
    parser.add_argument("--report", help="the agent's final report")
    parser.add_argument("--test-log", dest="test_log", help="captured test-run output")
    parser.add_argument("--test-exit-code", dest="test_exit_code", type=int, default=None)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("-v", "--verbose", action="store_true", help="show reasons for verified claims too")
    parser.add_argument(
        "--fail-on",
        choices=["info", "suspicious", "contradiction", "never"],
        default=None,
        help="exit non-zero at or above this severity (CI gate)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.diff and not args.report and not args.test_log:
        print("claimcheck: give at least one of --diff/--report/--test-log", file=sys.stderr)
        return EXIT_USAGE

    try:
        report = audit(
            diff_text=_read_checked(args, "diff"),
            report_text=_read_checked(args, "report"),
            log_text=_read_checked(args, "test_log"),
            log_exit_code=args.test_exit_code,
        )
    except UsageError as exc:
        print(f"claimcheck: {exc}", file=sys.stderr)
        return EXIT_USAGE

    if args.json:
        print(json.dumps(_to_json(report), indent=2))
    else:
        print(_render(report, verbose=args.verbose))

    if args.fail_on == "never":
        return EXIT_CLEAN
    if args.fail_on:
        threshold = LEVELS[args.fail_on]
        if report.worst.rank >= threshold.rank:
            return EXIT_CONTRADICTION if report.worst is Severity.CONTRADICTION else EXIT_SUSPICIOUS
        return EXIT_CLEAN
    # No explicit gate: a contradiction is still a failed check on its own.
    return EXIT_CONTRADICTION if report.worst is Severity.CONTRADICTION else EXIT_CLEAN


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
