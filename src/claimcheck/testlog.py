"""Test-log parsing.

A claim of "all tests pass" is only checkable if there is a log to check it
against. This module extracts the verdict from common runners and, importantly,
treats *absence of evidence* as a state of its own rather than as success.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# pytest: "5 passed, 1 failed in 0.12s" / "3 failed, 2 passed"
PYTEST_SUMMARY = re.compile(
    r"(?P<count>\d+)\s+(?P<word>passed|failed|error(?:s)?|skipped|xfailed|xpassed|warning(?:s)?|deselected)\b",
    re.I,
)
PYTEST_EXIT = re.compile(r"^(?:exit(?:\s+code)?[:\s]+)(?P<code>-?\d+)\s*$", re.I | re.M)

# go test: "--- FAIL: TestThing" / "ok  pkg  0.1s" / "FAIL\tpkg\t0.1s"
GO_FAIL = re.compile(r"^--- FAIL: (?P<name>\S+)", re.M)
GO_PASS = re.compile(r"^--- PASS: (?P<name>\S+)", re.M)
GO_PKG_FAIL = re.compile(r"^FAIL\s+(?P<pkg>\S+)", re.M)
GO_PKG_OK = re.compile(r"^ok\s+(?P<pkg>\S+)", re.M)

# jest/vitest: "Tests:  1 failed, 4 passed, 5 total"
JEST_SUMMARY = re.compile(r"^Tests:\s*(?P<rest>.+)$", re.M | re.I)
JEST_COUNTS = re.compile(r"(?P<count>\d+)\s+(?P<word>failed|passed|skipped|todo|total)", re.I)

# npm: "npm ERR!" plus a non-zero exit; cargo: "test result: FAILED. 3 passed; 1 failed"
CARGO_SUMMARY = re.compile(r"^test result:\s*(?P<verdict>\w+)\.\s*(?P<rest>.*)$", re.M | re.I)
CARGO_COUNTS = re.compile(r"(?P<count>\d+)\s+(?P<word>passed|failed|ignored|measured)", re.I)

# Generic non-zero exit markers people paste into reports.
GENERIC_FAIL = re.compile(
    r"(?:npm\s+ERR!|ERROR:|Traceback \(most recent call last\)|"
    r"\bcommand\s+failed\b|\bfailed\s+with\s+exit\b|make:\s+\*\*\*)",
    re.I,
)
GENERIC_OK = re.compile(r"\bAll tests passed\b|\bBUILD SUCCESS\b|\bSUCCESS\b", re.I)


@dataclass
class TestRun:
    """What the log actually shows."""

    source: str = ""
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    exit_code: int | None = None
    failed_names: list[str] = field(default_factory=list)
    runner: str = "unknown"
    raw_evidence: list[str] = field(default_factory=list)
    provided: bool = False

    @property
    def has_counts(self) -> bool:
        return bool(self.passed or self.failed or self.errors or self.skipped)

    @property
    def observed_failure(self) -> bool:
        """Any positive evidence that something failed."""

        return bool(self.failed or self.errors or self.failed_names) or (
            self.exit_code is not None and self.exit_code != 0
        )

    @property
    def observed_success(self) -> bool:
        """Positive evidence of a clean run — an exit code of 0, or passes with
        no failures recorded anywhere in the same log."""

        if self.exit_code is not None:
            return self.exit_code == 0 and not self.observed_failure
        return self.passed > 0 and not self.observed_failure

    def summary(self) -> str:
        if not self.has_counts and self.exit_code is None:
            # Be precise about which of the two failures this is: nothing was
            # supplied, versus something was supplied that carried no verdict.
            return (
                "log supplied but no parseable verdict"
                if self.provided
                else "no log supplied"
            )
        bits = []
        if self.passed:
            bits.append(f"{self.passed} passed")
        if self.failed:
            bits.append(f"{self.failed} failed")
        if self.errors:
            bits.append(f"{self.errors} error(s)")
        if self.skipped:
            bits.append(f"{self.skipped} skipped")
        if self.exit_code is not None:
            bits.append(f"exit {self.exit_code}")
        return ", ".join(bits) or "no counts parsed"


def parse(text: str, *, source: str = "", exit_code: int | None = None) -> TestRun:
    """Extract the verdict from a test log. Never guesses a pass."""

    run = TestRun(source=source, exit_code=exit_code, provided=bool(text.strip()))
    if not text.strip():
        return run

    # pytest
    if "passed" in text or "failed" in text:
        counts: dict[str, int] = {}
        for match in PYTEST_SUMMARY.finditer(text):
            word = match.group("word").lower()
            counts[word] = counts.get(word, 0) + int(match.group("count"))
        if counts:
            run.runner = "pytest-ish"
            run.passed = counts.get("passed", 0) + counts.get("xpassed", 0)
            run.failed = counts.get("failed", 0) + counts.get("xfailed", 0)
            run.errors = counts.get("error", 0) + counts.get("errors", 0)
            run.skipped = counts.get("skipped", 0)
            for line in text.splitlines():
                if re.search(r"^(?:FAILED|ERROR)\s", line.strip()):
                    run.raw_evidence.append(line.strip())

    if "test result:" in text.lower():
        for match in CARGO_SUMMARY.finditer(text):
            run.runner = "cargo"
            rest = match.group("rest")
            for count_match in CARGO_COUNTS.finditer(rest):
                word = count_match.group("word").lower()
                value = int(count_match.group("count"))
                if word == "passed":
                    run.passed += value
                elif word == "failed":
                    run.failed += value
                elif word == "ignored":
                    run.skipped += value
            if match.group("verdict").upper().startswith("FAIL"):
                run.raw_evidence.append(match.group(0).strip())

    # go test. Detection must not be gated on failure markers — a fully passing
    # Go log ("ok  pkg  0.1s") has no FAIL line at all, and gating on one made
    # every green Go run read as "no usable verdict".
    if GO_FAIL.search(text) or GO_PKG_FAIL.search(text) or GO_PKG_OK.search(text) or GO_PASS.search(text):
        run.runner = "go"
        run.failed_names.extend(m.group("name") for m in GO_FAIL.finditer(text))
        package_failures = len(GO_PKG_FAIL.findall(text))
        run.failed += len(run.failed_names) or package_failures
        run.passed += len(GO_PASS.findall(text)) + len(GO_PKG_OK.findall(text))
        for line in text.splitlines():
            stripped = line.strip()
            if GO_FAIL.match(stripped) or GO_PKG_FAIL.match(stripped):
                run.raw_evidence.append(stripped)

    jest = JEST_SUMMARY.search(text)
    if jest:
        run.runner = "jest/vitest"
        for count_match in JEST_COUNTS.finditer(jest.group("rest")):
            word = count_match.group("word").lower()
            value = int(count_match.group("count"))
            if word == "passed":
                run.passed += value
            elif word == "failed":
                run.failed += value
            elif word == "skipped":
                run.skipped += value
        run.raw_evidence.append(jest.group(0).strip())

    if run.runner == "unknown":
        if GENERIC_FAIL.search(text):
            run.raw_evidence.extend(
                line.strip() for line in text.splitlines() if GENERIC_FAIL.search(line)
            )
            run.runner = "generic"
            if not run.failed:
                run.failed = len(run.raw_evidence)
        elif GENERIC_OK.search(text):
            run.runner = "generic"

    return run
