"""Claim extraction: what did the run say it did, and how checkable is it?

The point is not to read the report. It is to find every statement that *could*
be checked against the diff or the test log, so the audit has something concrete
to confirm or refute. A claim that cannot be checked is itself worth reporting.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# (pattern, kind, description)
CLAIM_PATTERNS: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"\ball\s+(?:the\s+)?tests?\s+(?:now\s+)?pass(?:ed|es)?\b", re.I), "tests_pass", "all tests pass"),
    (re.compile(r"\btests?\s+(?:are\s+)?(?:all\s+)?passing\b", re.I), "tests_pass", "tests passing"),
    (re.compile(r"\b\d+[/ ]\d+\s+tests?\s+pass", re.I), "tests_pass_counted", "N/M tests pass"),
    (re.compile(r"\b\d+\s+tests?\s+(?:pass|passed|passing)", re.I), "tests_pass_counted", "N tests pass"),
    (re.compile(r"\btest\s+suite\s+(?:passes|passed|is\s+green)\b", re.I), "tests_pass", "test suite green"),
    (re.compile(r"\bgreen\s+(?:ci|build|tests?)\b|\bci\s+is\s+green\b", re.I), "tests_pass", "CI green"),
    (re.compile(r"\bno\s+(?:tests?\s+)?failures?\b|\bzero\s+failures?\b", re.I), "tests_pass", "no failures"),
    (re.compile(r"\bdoes\s+not\s+break\b|\bno\s+regressions?\b|\bwithout\s+breaking\b", re.I), "no_regression", "no regressions"),
    (re.compile(r"\b(?:fully|completely|thoroughly)\s+tested\b|\bwell[- ]tested\b", re.I), "coverage", "thoroughly tested"),
    (re.compile(r"\b(?:added|wrote|includes?)\s+(?:unit\s+|integration\s+|new\s+)?tests?\b", re.I), "tests_added", "tests added"),
    (re.compile(r"\ball\s+(?:the\s+)?checks?\s+pass", re.I), "tests_pass", "all checks pass"),
    (re.compile(r"\bbuild\s+(?:succe\w+|pass\w+)\b", re.I), "build_ok", "build succeeded"),
    (re.compile(r"\bno\s+(?:uncommitted\s+)?(?:changes?|modifications?)\s+remain", re.I), "clean_tree", "no changes remain"),
    (re.compile(r"\bmade\s+no\s+(?:functional\s+)?changes?\b", re.I), "no_functional_change", "no functional change"),
    (re.compile(r"\brefactor(?:ed|ing)?\s+(?:only|without\s+behaviou?r)", re.I), "no_functional_change", "refactor only"),
    (re.compile(r"\bfixed\s+(?:the\s+)?(?:bug|issue|regression)\b", re.I), "bugfix", "bug fixed"),
    (re.compile(r"\bresolved\s+(?:the\s+)?(?:bug|issue)\b", re.I), "bugfix", "issue resolved"),
    (re.compile(r"\b100%\s+(?:pass|coverage|test)\w*", re.I), "coverage", "100% claim"),
]

# Filler that inflates a report without adding information. Counted, not judged
# by content, because the metric that matters is how much has to be read.
BOILERPLATE = re.compile(
    r"""^(?:
        (?:in\s+(?:this|summary)|to\s+summari[sz]e|overall|in\s+conclusion|
        first(?:ly)?|second(?:ly)?|third(?:ly)?|next|then|finally|
        let\s+me\s+know|i\s+hope\s+this\s+helps|feel\s+free\s+to|
        as\s+(?:requested|you\s+can\s+see)|here(?:'s|\s+is)\s+(?:what|a\s+summary))[\s,:]
        |^[#]{1,6}\s*(?:summary|overview|conclusion|next\s+steps?)\s*$
    )""",
    re.I | re.VERBOSE,
)


@dataclass
class Claim:
    kind: str
    description: str
    sentence: str
    line_number: int

    def __str__(self) -> str:
        return f"[{self.kind}] {self.sentence}"


@dataclass
class ClaimSet:
    claims: list[Claim] = field(default_factory=list)
    sentences: int = 0
    words: int = 0
    paragraphs: int = 0
    boilerplate_lines: int = 0
    headings: int = 0

    def of_kind(self, *kinds: str) -> list[Claim]:
        wanted = set(kinds)
        return [c for c in self.claims if c.kind in wanted]

    @property
    def has_test_claim(self) -> bool:
        return bool(self.of_kind("tests_pass", "tests_pass_counted", "coverage"))

    @property
    def claims_only_tests(self) -> bool:
        return self.claims and all(c.kind.startswith("tests") or c.kind in {"coverage", "build_ok"} for c in self.claims)


def split_sentences(text: str) -> list[str]:
    """Sentence split that does not trip over abbreviations or file paths."""

    # Mask the period in known abbreviations so the sentence split below does not
    # treat them as terminators. A replace-callback avoids backreference and
    # look-behind width constraints entirely.
    cleaned = re.sub(
        r"\b(?:e\.g|i\.e|etc|vs|Dr|Mr|Ms|v)\.",
        lambda m: m.group(0)[:-1] + "<DOT>",
        text,
    )
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9#\-*`_])", cleaned)
    return [p.replace("<DOT>", ".").strip() for p in parts if p.strip()]


def extract(text: str) -> ClaimSet:
    """Pull checkable claims and verbosity stats out of a run report."""

    result = ClaimSet()
    lines = text.splitlines()
    result.words = len(text.split())
    result.headings = sum(1 for line in lines if line.lstrip().startswith("#"))

    paragraphs = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    result.paragraphs = len(paragraphs)

    seen: dict[str, Claim] = {}
    # When two patterns match the same sentence, keep the more specific kind:
    # "59 tests passing" is a counted claim, not a generic one.
    specificity = {"tests_pass_counted": 2, "coverage": 1}

    for line_number, line in enumerate(lines, 1):
        if BOILERPLATE.match(line.strip()):
            result.boilerplate_lines += 1

    for line_number, line in enumerate(lines, 1):
        for sentence in split_sentences(line):
            result.sentences += 1
            for pattern, kind, description in CLAIM_PATTERNS:
                if not pattern.search(sentence):
                    continue
                # Dedup on the sentence, not on (kind, sentence): one sentence is
                # one claim even when several patterns match it.
                normalized = re.sub(r"[^a-z0-9 ]+", "", sentence.strip().lower()).strip()
                existing = seen.get(normalized)
                if existing is not None:
                    if specificity.get(kind, 0) > specificity.get(existing.kind, 0):
                        result.claims.remove(existing)
                    else:
                        continue
                claim = Claim(
                    kind=kind,
                    description=description,
                    sentence=sentence.strip()[:280],
                    line_number=line_number,
                )
                seen[normalized] = claim
                result.claims.append(claim)
    return result
