"""Unified-diff parsing.

The audit needs three things out of a diff: which files changed, how many lines
moved, and — critically — whether any of those files are *tests*. A run that
edits its own tests is the single most common way a false "passed" is produced.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

HUNK_HEADER = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@(?P<section>.*)$"
)
DIFF_HEADER = re.compile(r"^diff --git a/(?P<a>.+?) b/(?P<b>.+?)$")

# Assertion-ish constructs. Removing these from a test file is how a test gets
# "fixed" without the bug being fixed.
ASSERTION = re.compile(
    r"""\b(?:
        assert(?:\s|_|\() | expect(?:\s|\() | should\b |
        require\b | assertEquals | assertEqual | t\.Error | t\.Fatal |
        die\( | panic\( | unwrap\(\) | must_
    )""",
    re.VERBOSE,
)

TEST_DIRS = frozenset(
    {"test", "tests", "spec", "specs", "__tests__", "testing", "it", "e2e"}
)

# Documentation is not a functional surface. A "no functional change" claim is
# satisfied by a docs-only diff, and refuted only by real source edits.
DOC_EXT = (".md", ".rst", ".txt", ".adoc", ".mdx")
DOC_NAMES = frozenset({"README", "CHANGELOG", "CONTRIBUTING", "AUTHORS", "NOTICE", "LICENSE"})


def is_doc_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    lower = name.lower()
    if lower.endswith(DOC_EXT):
        return True
    # Extensionless docs (README, CHANGELOG) and license variants.
    stem = name.rsplit(".", 1)[0] if "." in name else name
    return stem.upper() in DOC_NAMES or stem.upper().startswith(("README", "CHANGELOG"))
TEST_FILE_PATTERNS = (
    re.compile(r"^test_.*\.py$"),
    re.compile(r".*_test\.py$"),
    re.compile(r".*\.test\.[jt]sx?$"),
    re.compile(r".*\.spec\.[jt]sx?$"),
    re.compile(r".*_test\.go$"),
    re.compile(r".*Test\.java$"),
    re.compile(r".*\.test\.rs$"),
    re.compile(r"^test_.*\.rb$"),
    re.compile(r".*_spec\.rb$"),
    re.compile(r".*\.spec\.ts$"),
)
# Test infra: config/conftest changes can silently change what runs at all.
TEST_INFRA_NAMES = frozenset(
    {
        "conftest.py",
        "pytest.ini",
        "tox.ini",
        "noxfile.py",
        "jest.config.js",
        "jest.config.ts",
        "vitest.config.ts",
        "vitest.config.js",
        "karma.conf.js",
        "setup.cfg",
    }
)


def is_test_file(path: str) -> bool:
    """True when ``path`` looks like a test file or test infrastructure."""

    parts = [p for p in path.split("/") if p]
    if not parts:
        return False
    name = parts[-1]
    if name in TEST_INFRA_NAMES:
        return True
    if any(part.lower() in TEST_DIRS for part in parts[:-1]):
        return True
    return any(pattern.match(name) for pattern in TEST_FILE_PATTERNS)


@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    section: str = ""
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    @property
    def header(self) -> str:
        return f"@@ -{self.old_start},{self.old_count} +{self.new_start},{self.new_count} @@"

    def anchor(self) -> str:
        where = f"{self.new_start}-{self.new_start + max(self.new_count - 1, 0)}"
        return f"{self.section.strip()} L{where}" if self.section.strip() else f"L{where}"


@dataclass
class FileChange:
    path: str
    old_path: str = ""
    new_path: str = ""
    hunks: list[Hunk] = field(default_factory=list)
    is_new: bool = False
    is_deleted: bool = False
    is_renamed: bool = False

    @property
    def is_test(self) -> bool:
        return is_test_file(self.path)

    @property
    def added(self) -> int:
        return sum(len(h.added) for h in self.hunks)

    @property
    def removed(self) -> int:
        return sum(len(h.removed) for h in self.hunks)

    @property
    def assertions_added(self) -> int:
        return sum(
            1 for h in self.hunks for line in h.added if ASSERTION.search(line)
        )

    @property
    def assertions_removed(self) -> int:
        return sum(
            1 for h in self.hunks for line in h.removed if ASSERTION.search(line)
        )

    def touched_symbols(self) -> list[str]:
        """Section headings (function/class names) from hunk headers."""

        seen: list[str] = []
        for hunk in self.hunks:
            section = hunk.section.strip()
            if section and section not in seen:
                seen.append(section)
        return seen

    def describe(self) -> str:
        verb = "added" if self.is_new else "deleted" if self.is_deleted else "modified"
        return f"{self.path} ({verb}, +{self.added}/-{self.removed})"


@dataclass
class Diff:
    files: list[FileChange] = field(default_factory=list)

    @property
    def test_files(self) -> list[FileChange]:
        return [f for f in self.files if f.is_test]

    @property
    def source_files(self) -> list[FileChange]:
        """Files that change functional behaviour: not tests, not docs."""

        return [f for f in self.files if not f.is_test and not is_doc_file(f.path)]

    @property
    def doc_files(self) -> list[FileChange]:
        return [f for f in self.files if is_doc_file(f.path)]


    @property
    def added(self) -> int:
        return sum(f.added for f in self.files)

    @property
    def removed(self) -> int:
        return sum(f.removed for f in self.files)

    @property
    def empty(self) -> bool:
        return not self.files


def parse(text: str) -> Diff:
    """Parse a unified diff (git or plain). Unrecognised headers are ignored."""

    diff = Diff()
    current: FileChange | None = None
    hunk: Hunk | None = None

    for raw in text.splitlines():
        line = raw.rstrip("\n")

        match = DIFF_HEADER.match(line)
        if match:
            current = FileChange(path=match.group("b"), old_path=match.group("a"), new_path=match.group("b"))
            diff.files.append(current)
            hunk = None
            continue

        if line.startswith("--- "):
            # Plain (non-git) diff: "--- a/path" starts a file.
            if current is None:
                current = FileChange(path=_strip_prefix(line[4:]))
                diff.files.append(current)
            else:
                current.old_path = _strip_prefix(line[4:])
            hunk = None
            continue
        if line.startswith("+++ "):
            path = _strip_prefix(line[4:])
            if current is None:
                current = FileChange(path=path)
                diff.files.append(current)
            else:
                current.path = path
                current.new_path = path
            hunk = None
            continue

        if current is None:
            continue

        if line.startswith("new file mode"):
            current.is_new = True
            continue
        if line.startswith("deleted file mode"):
            current.is_deleted = True
            continue
        if line.startswith("rename from") or line.startswith("rename to"):
            current.is_renamed = True
            continue

        hunk_match = HUNK_HEADER.match(line)
        if hunk_match:
            hunk = Hunk(
                old_start=int(hunk_match.group("old_start")),
                old_count=int(hunk_match.group("old_count") or 1),
                new_start=int(hunk_match.group("new_start")),
                new_count=int(hunk_match.group("new_count") or 1),
                section=hunk_match.group("section") or "",
            )
            current.hunks.append(hunk)
            continue

        if hunk is None:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            hunk.added.append(line[1:])
        elif line.startswith("-") and not line.startswith("---"):
            hunk.removed.append(line[1:])

    # Drop file entries that never produced a hunk or a path (e.g. mode-only
    # stubs) so the changelog does not list phantom files.
    diff.files = [f for f in diff.files if f.path and (f.hunks or f.is_renamed)]
    return diff


def _strip_prefix(path: str) -> str:
    text = path.strip()
    if text in ("/dev/null", ""):
        return ""
    for prefix in ("a/", "b/"):
        if text.startswith(prefix):
            return text[len(prefix) :]
    return text
