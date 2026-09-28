"""Diff parsing, claim extraction, and test-log parsing in isolation."""

from __future__ import annotations

import pytest

from claimcheck import claims as claims_mod
from claimcheck import diff as diff_mod
from claimcheck import testlog as testlog_mod

# --------------------------------------------------------------------------
# diff parsing
# --------------------------------------------------------------------------


def test_counts_added_and_removed_lines():
    text = (
        "diff --git a/src/a.py b/src/a.py\n"
        "index 111..222 100644\n"
        "--- a/src/a.py\n"
        "+++ b/src/a.py\n"
        "@@ -1,3 +1,3 @@\n"
        "-old\n"
        "+new\n"
        "+extra\n"
    )
    d = diff_mod.parse(text)
    assert len(d.files) == 1
    assert d.added == 2
    assert d.removed == 1
    assert d.files[0].path == "src/a.py"


def test_test_file_detection():
    for path in ("tests/test_x.py", "src/x_test.py", "spec/x_spec.rb", "__tests__/x.js"):
        assert diff_mod.is_test_file(path), path
    for path in ("src/x.py", "lib/x.js", "docs/guide.md", "Makefile"):
        assert not diff_mod.is_test_file(path), path


def test_doc_file_detection():
    for path in ("README.md", "README", "CHANGELOG.md", "LICENSE", "docs/a.rst", "notes.txt"):
        assert diff_mod.is_doc_file(path), path
    for path in ("src/a.py", "tests/test_a.py", "Makefile"):
        assert not diff_mod.is_doc_file(path), path


def test_source_files_exclude_tests_and_docs():
    text = (
        "diff --git a/src/a.py b/src/a.py\n@@ -1 +1 @@\n-a\n+b\n"
        "diff --git a/tests/test_a.py b/tests/test_a.py\n@@ -1 +1 @@\n-a\n+b\n"
        "diff --git a/README.md b/README.md\n@@ -1 +1 @@\n-a\n+b\n"
    )
    d = diff_mod.parse(text)
    assert [f.path for f in d.source_files] == ["src/a.py"]
    assert [f.path for f in d.test_files] == ["tests/test_a.py"]
    assert [f.path for f in d.doc_files] == ["README.md"]


def test_new_and_deleted_files_are_tracked():
    text = (
        "diff --git a/new.py b/new.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/new.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+a\n"
        "+b\n"
    )
    d = diff_mod.parse(text)
    assert len(d.files) == 1
    assert d.files[0].is_new
    assert d.files[0].added == 2


def test_empty_diff_parses_to_no_files():
    d = diff_mod.parse("")
    assert d.empty
    assert d.added == 0 and d.removed == 0
    assert d.files == []


def test_garbage_input_does_not_crash():
    d = diff_mod.parse("not a diff at all\njust some text\n@@ malformed @@\n")
    assert d.empty


def test_assertion_counts_use_real_assert_syntax():
    text = (
        "diff --git a/tests/test_a.py b/tests/test_a.py\n"
        "@@ -1,2 +1,2 @@\n"
        "-    assert x == 1\n"
        "-    assert y == 2\n"
        "+    assert x is not None\n"
    )
    d = diff_mod.parse(text)
    change = d.files[0]
    assert change.assertions_removed == 2
    assert change.assertions_added == 1


def test_hunk_anchor_carries_a_line_range():
    text = (
        "diff --git a/src/a.py b/src/a.py\n"
        "@@ -10,4 +12,6 @@ def handler(self):\n"
        "-old\n"
        "+new\n"
    )
    d = diff_mod.parse(text)
    anchor = d.files[0].hunks[0].anchor()
    assert "L12-17" in anchor
    assert "def handler" in anchor


# --------------------------------------------------------------------------
# claim extraction
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,kind",
    [
        ("All tests pass.", "tests_pass"),
        ("The tests are passing now.", "tests_pass"),
        ("42/42 tests pass", "tests_pass_counted"),
        ("Fixed the bug in the parser.", "bugfix"),
        ("Resolved the issue with pagination.", "bugfix"),
        ("Added tests for the edge case.", "tests_added"),
        ("No regressions observed.", "no_regression"),
        ("Refactor only, no behaviour change.", "no_functional_change"),
    ],
)
def test_claim_kinds_are_detected(text, kind):
    found = {c.kind for c in claims_mod.extract(text).claims}
    assert kind in found, f"{kind} not found in {found}"


def test_duplicate_claims_are_deduplicated():
    text = "All tests pass. All tests pass. ALL TESTS PASS!\n"
    kinds = [c.kind for c in claims_mod.extract(text).claims]
    assert kinds.count("tests_pass") == 1


def test_one_sentence_matching_two_patterns_is_one_claim():
    """'59 tests passing' matches both the generic and counted patterns."""

    cs = claims_mod.extract("Verified: 59 tests passing.\n")
    sentences = [c.sentence for c in cs.claims]
    assert len(sentences) == len(set(sentences)), f"duplicate sentences: {sentences}"
    kinds = {c.kind for c in cs.claims}
    # The more specific kind wins over the generic one.
    assert "tests_pass_counted" in kinds
    assert "tests_pass" not in kinds


def test_distinct_claims_are_not_collapsed():
    """Two different sentences are two claims, even of the same kind."""

    kinds = [
        c.kind
        for c in claims_mod.extract("All tests pass.\nI fixed the bug in the parser.\n").claims
    ]
    assert "tests_pass" in kinds
    assert "bugfix" in kinds


def test_sentence_split_handles_abbreviations():
    sentences = claims_mod.split_sentences("Use e.g. this. Then that.")
    assert len(sentences) == 2


def test_boilerplate_counts_real_filler_only():
    text = "To summarize what I did:\n- fixed it\n\nOverall this should resolve the issue.\n"
    cs = claims_mod.extract(text)
    assert cs.boilerplate_lines == 2


def test_boilerplate_does_not_match_content_lines():
    """Regression: an unescaped # in a VERBOSE pattern matches every line."""

    cs = claims_mod.extract(
        "# Task: fix the bug\nFixed the issue in src/a.py. All tests pass.\n"
    )
    assert cs.boilerplate_lines == 0


def test_word_and_sentence_counts():
    cs = claims_mod.extract("One two three. Four five.")
    assert cs.words == 5
    assert cs.sentences == 2


# --------------------------------------------------------------------------
# test log parsing
# --------------------------------------------------------------------------


def test_pytest_summary_is_parsed():
    run = testlog_mod.parse("2 failed, 40 passed in 0.84s")
    assert run.failed == 2
    assert run.passed == 40
    assert run.observed_failure
    assert not run.observed_success


def test_pytest_all_pass_is_parsed():
    run = testlog_mod.parse("43 passed in 0.91s")
    assert run.passed == 43
    assert run.observed_success
    assert not run.observed_failure


def test_go_success_and_failure_are_both_parsed():
    """Go logs must be detected on success too, not only when FAIL appears."""

    ok = testlog_mod.parse("ok  \texample.com/pkg\t0.012s")
    assert ok.observed_success
    assert ok.runner == "go"

    fail = testlog_mod.parse("--- FAIL: TestThing (0.00s)")
    assert fail.observed_failure
    assert fail.runner == "go"


def test_cargo_summaries_are_parsed():
    assert testlog_mod.parse("test result: ok. 12 passed; 0 failed").observed_success
    assert testlog_mod.parse("test result: FAILED. 3 passed; 1 failed").observed_failure


def test_exit_code_alone_yields_a_verdict():
    assert testlog_mod.parse("", exit_code=0).observed_success
    assert testlog_mod.parse("", exit_code=1).observed_failure


def test_no_log_and_no_exit_code_is_neither():
    run = testlog_mod.parse("")
    assert not run.observed_success
    assert not run.observed_failure
    assert "no log" in run.summary()


def test_failure_beats_success_when_both_appear():
    """A log that reports a failure is not a passing log, whatever else it says."""

    run = testlog_mod.parse("43 passed in 0.9s\n2 failed, 41 passed in 0.9s")
    assert run.observed_failure


def test_skipped_counts_do_not_read_as_failures():
    run = testlog_mod.parse("40 passed, 3 skipped in 0.5s")
    assert run.observed_success
    assert not run.observed_failure
    assert run.skipped == 3
