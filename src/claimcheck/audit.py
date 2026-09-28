"""The audit: three failure modes, each answered from evidence.

(a) verbosity        — the report is compressed into a diff-anchored changelog
(b) self-review holes — test edits inside a bugfix run; assertion removals;
                        pass/fail churn across review rounds
(c) unearned confidence — a claim of success with no log, a refuted claim, or a
                        diff that contradicts the stated story

Every finding carries the evidence that produced it, so a human can check the
checker. A finding with no evidence is not emitted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from . import claims as claims_mod
from . import diff as diff_mod
from . import testlog as testlog_mod


class Severity(str, Enum):
    INFO = "info"
    SUSPICIOUS = "suspicious"
    CONTRADICTION = "contradiction"

    @property
    def rank(self) -> int:
        return {"info": 0, "suspicious": 1, "contradiction": 2}[self.value]


class Verdict(str, Enum):
    VERIFIED = "verified"
    UNSUPPORTED = "unsupported"
    REFUTED = "refuted"
    UNCHECKABLE = "uncheckable"

    @property
    def label(self) -> str:
        return {
            "verified": "verified",
            "unsupported": "unsupported",
            "refuted": "REFUTED",
            "uncheckable": "uncheckable",
        }[self.value]


@dataclass
class Finding:
    code: str
    severity: Severity
    title: str
    detail: str
    evidence: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return f"[{self.severity.value.upper()}] {self.title}: {self.detail}"


@dataclass
class ClaimCheck:
    claim: claims_mod.Claim
    verdict: Verdict
    reason: str

    def __str__(self) -> str:
        return f"{self.verdict.label}: {self.claim.sentence} — {self.reason}"


@dataclass
class ChangelogEntry:
    anchor: str
    text: str
    kind: str  # add | fix | test | remove | rename | doc


@dataclass
class Report:
    diff: diff_mod.Diff
    claims: claims_mod.ClaimSet
    tests: testlog_mod.TestRun
    findings: list[Finding] = field(default_factory=list)
    checks: list[ClaimCheck] = field(default_factory=list)
    changelog: list[ChangelogEntry] = field(default_factory=list)
    prior_rounds: list[str] = field(default_factory=list)
    churn: list[str] = field(default_factory=list)

    @property
    def worst(self) -> Severity:
        return max(
            (f.severity for f in self.findings),
            key=lambda s: s.rank,
            default=Severity.INFO,
        )

    @property
    def refuted(self) -> list[ClaimCheck]:
        return [c for c in self.checks if c.verdict is Verdict.REFUTED]

    @property
    def suspicious_findings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is not Severity.INFO]

    @property
    def clean(self) -> bool:
        return not self.suspicious_findings and not self.refuted


# --------------------------------------------------------------------------
# (a) diff-anchored changelog
# --------------------------------------------------------------------------

def build_changelog(d: diff_mod.Diff, limit_per_file: int = 6) -> list[ChangelogEntry]:
    """Compress a diff into per-file, per-anchor lines. No prose, no preamble."""

    entries: list[ChangelogEntry] = []
    for change in d.files:
        if change.is_new:
            kind = "add"
        elif change.is_deleted:
            kind = "remove"
        elif change.is_renamed:
            kind = "rename"
        elif change.is_test:
            kind = "test"
        elif change.path.lower().endswith(diff_mod.DOC_EXT) or diff_mod.is_doc_file(change.path):
            kind = "doc"
        elif change.assertions_removed > 0:
            kind = "fix"
        else:
            kind = "fix"

        for hunk in change.hunks[:limit_per_file]:
            # Name only the symbol this hunk is actually inside — using every
            # symbol in the file would attribute unrelated code to the hunk.
            symbol = hunk.section.strip()
            entries.append(
                ChangelogEntry(
                    anchor=f"{change.path}:{hunk.anchor()}",
                    text=f"+{len(hunk.added)}/-{len(hunk.removed)}"
                    + (f" ({symbol})" if symbol else ""),
                    kind=kind,
                )
            )
        if not change.hunks:
            entries.append(ChangelogEntry(anchor=change.path, text=change.describe(), kind=kind))
    return entries


# --------------------------------------------------------------------------
# (b) self-review blind spots
# --------------------------------------------------------------------------

CHURN_ROUND_MARKER = re.compile(
    r"^(?:#{1,6}\s*)?(?:round|review|iteration|attempt)\s*[-#:]?\s*(\d+)\b", re.I | re.M
)


def detect_churn(report_text: str) -> tuple[list[str], list[str]]:
    """Find rounds in a report and flag pass/fail flapping between them.

    The community pattern is an agent passing its own check for days, then a
    review passing the bug the *fix* introduced. Flapping between rounds in a
    single report is the cheap local signal for the same shape.
    """

    rounds: list[str] = []
    churn: list[str] = []
    lines = report_text.splitlines()
    current: list[str] = []
    for line in lines:
        if CHURN_ROUND_MARKER.match(line.strip()):
            if current:
                rounds.append("\n".join(current))
            current = [line]
        elif current:
            current.append(line)
    if current:
        rounds.append("\n".join(current))

    if len(rounds) < 2:
        return rounds, churn

    verdicts: list[bool | None] = []
    for block in rounds:
        run = testlog_mod.parse(block)
        if run.observed_failure:
            verdicts.append(False)
        elif run.observed_success:
            verdicts.append(True)
        else:
            verdicts.append(None)

    for index in range(1, len(verdicts)):
        previous, current_v = verdicts[index - 1], verdicts[index]
        if previous is not None and current_v is not None and previous != current_v:
            direction = "pass -> fail" if previous else "fail -> pass"
            churn.append(f"round {index} -> {index + 1}: {direction}")
    return rounds, churn


def audit_self_review(
    d: diff_mod.Diff,
    claimset: claims_mod.ClaimSet,
    tests: testlog_mod.TestRun,
) -> list[Finding]:
    findings: list[Finding] = []
    # Editing tests is only a concern when the run's job was to fix product code.
    # A run whose whole purpose is writing tests, or one that merely reports a
    # green suite, is not evading anything by touching them.
    bugfix_run = bool(claimset.of_kind("bugfix"))

    edited_tests = d.test_files
    if edited_tests:
        removal_heavy = [f for f in edited_tests if f.assertions_removed > f.assertions_added]
        detail = "; ".join(f.describe() for f in edited_tests)
        # Touching tests in a bugfix run is only *suspicious* when the tests got
        # weaker or when no product code moved. Adding a regression test beside a
        # source fix is what a good run looks like, and stays informational.
        evasive = bool(removal_heavy) or not d.source_files
        severity = Severity.SUSPICIOUS if (bugfix_run and evasive) else Severity.INFO
        findings.append(
            Finding(
                code="tests-edited",
                severity=severity,
                title="test files edited in this run",
                detail=f"{len(edited_tests)} test file(s) changed: {detail}",
                evidence=[f"{f.path}: +{f.assertions_added}/-{f.assertions_removed} assertions"
                          for f in edited_tests],
            )
        )
        if removal_heavy:
            names = ", ".join(f.path for f in removal_heavy)
            findings.append(
                Finding(
                    code="tests-weakened",
                    severity=Severity.CONTRADICTION if bugfix_run else Severity.SUSPICIOUS,
                    title="test assertions removed",
                    detail=(
                        f"{len(removal_heavy)} test file(s) lost more assertions than they gained: {names}. "
                        "A run that removes checks can pass without the bug being fixed."
                    ),
                    evidence=[f"{f.path}: -{f.assertions_removed}/+{f.assertions_added} assertions"
                              for f in removal_heavy],
                )
            )
        if edited_tests and not d.source_files:
            findings.append(
                Finding(
                    code="tests-only-change",
                    severity=Severity.CONTRADICTION,
                    title="only tests changed",
                    detail=(
                        "The diff touches test files and no source files. Nothing in the "
                        "product changed, so any 'fixed' or 'passing' claim describes the "
                        "checks, not the code."
                    ),
                    evidence=[f.path for f in edited_tests],
                )
            )

    if tests.observed_failure and claimset.has_test_claim:
        findings.append(
            Finding(
                code="claim-vs-log",
                severity=Severity.CONTRADICTION,
                title="report claims passing tests, log shows failures",
                detail=f"Log verdict: {tests.summary()}",
                evidence=tests.raw_evidence[:5] or [tests.summary()],
            )
        )
    return findings


def audit_claims(
    claimset: claims_mod.ClaimSet,
    d: diff_mod.Diff,
    tests: testlog_mod.TestRun,
    *,
    has_log: bool,
) -> list[ClaimCheck]:
    """Turn each checkable claim into verified / refuted / unsupported."""

    checks: list[ClaimCheck] = []
    for claim in claimset.claims:
        checks.append(_check_one(claim, d, tests, has_log=has_log))
    return checks


def _check_one(
    claim: claims_mod.Claim,
    d: diff_mod.Diff,
    tests: testlog_mod.TestRun,
    *,
    has_log: bool,
) -> ClaimCheck:
    kind = claim.kind

    if kind in {"tests_pass", "tests_pass_counted", "build_ok"}:
        if not has_log:
            return ClaimCheck(
                claim,
                Verdict.UNSUPPORTED,
                "no test log was supplied, so this cannot be checked from the evidence given",
            )
        if tests.observed_failure:
            return ClaimCheck(claim, Verdict.REFUTED, f"log shows {tests.summary()}")
        if tests.observed_success:
            return ClaimCheck(claim, Verdict.VERIFIED, f"log shows {tests.summary()}")
        if kind == "tests_pass_counted" and tests.passed:
            return ClaimCheck(claim, Verdict.VERIFIED, f"log shows {tests.summary()}")
        return ClaimCheck(
            claim,
            Verdict.UNCHECKABLE,
            f"log supplied but carries no parseable verdict ({tests.summary()})",
        )

    if kind == "tests_added":
        if d.test_files and any(f.added > 0 for f in d.test_files):
            return ClaimCheck(
                claim,
                Verdict.VERIFIED,
                "diff adds lines to " + ", ".join(f.path for f in d.test_files),
            )
        if d.test_files:
            return ClaimCheck(claim, Verdict.REFUTED, "test files changed but gained no lines")
        return ClaimCheck(claim, Verdict.REFUTED, "no test file changed in the diff")

    if kind == "no_regression":
        if tests.observed_failure:
            return ClaimCheck(claim, Verdict.REFUTED, f"log shows {tests.summary()}")
        if not has_log:
            return ClaimCheck(
                claim,
                Verdict.UNSUPPORTED,
                "no test log supplied, so 'no regressions' is an assertion rather than a result",
            )
        return ClaimCheck(claim, Verdict.VERIFIED, f"no failures in log ({tests.summary()})")

    if kind == "coverage":
        if tests.observed_failure:
            return ClaimCheck(claim, Verdict.REFUTED, f"log shows {tests.summary()}")
        if not d.test_files:
            return ClaimCheck(
                claim,
                Verdict.REFUTED,
                "no test file appears in the diff, so the coverage claim is not backed by changes",
            )
        return ClaimCheck(claim, Verdict.UNSUPPORTED, "coverage is not measurable from a diff and a log")

    if kind == "bugfix":
        if not d.files:
            return ClaimCheck(claim, Verdict.UNSUPPORTED, "empty diff — nothing to attribute the fix to")
        if tests.observed_failure:
            return ClaimCheck(
                claim,
                Verdict.UNSUPPORTED,
                f"a fix may well be present, but the log still shows {tests.summary()}",
            )
        return ClaimCheck(
            claim,
            Verdict.VERIFIED if tests.observed_success else Verdict.UNSUPPORTED,
            f"diff changes {len(d.files)} file(s); " + (f"log shows {tests.summary()}" if has_log else "no log to confirm against"),
        )

    if kind == "clean_tree":
        return ClaimCheck(claim, Verdict.UNCHECKABLE, "working-tree state is not visible from a diff")

    if kind == "no_functional_change":
        if d.source_files:
            return ClaimCheck(
                claim,
                Verdict.REFUTED,
                f"{len(d.source_files)} source file(s) changed, which is a functional surface",
            )
        return ClaimCheck(claim, Verdict.VERIFIED, "no source file changed")

    return ClaimCheck(claim, Verdict.UNCHECKABLE, "no rule for this claim type")


# --------------------------------------------------------------------------
# (c) unearned confidence + verbosity
# --------------------------------------------------------------------------

VERBOSITY_WORDS = 800
VERBOSITY_RATIO = 40  # words per changed line before a report is called padded
VERBOSITY_MIN_WORDS = 200  # below this, even a high ratio is just a short note


def audit_confidence(
    claimset: claims_mod.ClaimSet,
    d: diff_mod.Diff,
    tests: testlog_mod.TestRun,
    *,
    has_log: bool,
    churn: list[str],
) -> list[Finding]:
    findings: list[Finding] = []

    if claimset.has_test_claim and not has_log:
        findings.append(
            Finding(
                code="no-test-evidence",
                severity=Severity.CONTRADICTION,
                title="success claimed with no test log",
                detail=(
                    "The report asserts tests pass but no log was supplied. This is the "
                    "failure mode where an agent's own summary becomes the only evidence."
                ),
                evidence=[c.sentence for c in claimset.of_kind("tests_pass", "tests_pass_counted")][:3],
            )
        )

    if claimset.has_test_claim and not d.test_files and not has_log:
        findings.append(
            Finding(
                code="claim-without-substance",
                severity=Severity.CONTRADICTION,
                title="passing tests claimed, no tests touched, no log",
                detail="Nothing in the evidence supports the claim.",
                evidence=[c.sentence for c in claimset.claims if "test" in c.kind][:3],
            )
        )

    changed = max(d.added + d.removed, 1)
    padded_by_ratio = claimset.words / changed > VERBOSITY_RATIO and claimset.words >= VERBOSITY_MIN_WORDS
    if claimset.words > VERBOSITY_WORDS or padded_by_ratio:
        findings.append(
            Finding(
                code="wall-of-text",
                severity=Severity.SUSPICIOUS,
                title="report is long relative to the change",
                detail=(
                    f"{claimset.words} words of report for {changed} changed lines "
                    f"({claimset.words / changed:.0f} words per line)."
                ),
                evidence=[f"boilerplate lines: {claimset.boilerplate_lines}"],
            )
        )

    if claimset.boilerplate_lines >= 3:
        findings.append(
            Finding(
                code="boilerplate",
                severity=Severity.INFO,
                title="padding detected in the report",
                detail=f"{claimset.boilerplate_lines} line(s) are filler ('to summarize', 'overall', ...).",
                evidence=[],
            )
        )

    if churn:
        findings.append(
            Finding(
                code="review-churn",
                severity=Severity.SUSPICIOUS,
                title="pass/fail flapping between rounds",
                detail=(
                    "The report's own rounds disagree on whether the tests pass. This is "
                    "the shape of a fix that introduced the next failure."
                ),
                evidence=churn,
            )
        )

    if d.empty and (claimset.claims or claimset.words > 50):
        findings.append(
            Finding(
                code="empty-diff",
                severity=Severity.CONTRADICTION,
                title="report describes work, diff is empty",
                detail=(
                    "The 'automation ran successfully, nothing got done' pattern: prose "
                    "describing changes that are not in the diff."
                ),
                evidence=[c.sentence for c in claimset.claims][:3] or [f"{claimset.words} words, 0 changed lines"],
            )
        )
    return findings


# --------------------------------------------------------------------------
# top level
# --------------------------------------------------------------------------

def audit(
    *,
    diff_text: str,
    report_text: str,
    log_text: str = "",
    log_exit_code: int | None = None,
) -> Report:
    """Run all three audits and assemble the report. Pure function."""

    d = diff_mod.parse(diff_text)
    claimset = claims_mod.extract(report_text)
    tests = testlog_mod.parse(log_text, exit_code=log_exit_code)
    has_log = bool(log_text.strip()) or log_exit_code is not None

    rounds, churn = detect_churn(report_text)
    report = Report(
        diff=d,
        claims=claimset,
        tests=tests,
        changelog=build_changelog(d),
        prior_rounds=rounds,
        churn=churn,
    )
    report.findings.extend(audit_self_review(d, claimset, tests))
    report.findings.extend(
        audit_confidence(claimset, d, tests, has_log=has_log, churn=churn)
    )
    report.checks = audit_claims(claimset, d, tests, has_log=has_log)
    return report
