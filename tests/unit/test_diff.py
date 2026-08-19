"""Boundary tests for strict in-process unified-diff dry runs."""

from __future__ import annotations

import pytest

import patchshuttle._diff as diff_module
from patchshuttle._diff import apply_file_diff, parse_unified_diff
from patchshuttle.errors import PlanningError, PlanningErrorCode

ITEM_ID = "action_001"


def parse(value: str, *, strip: int = 1):
    return parse_unified_diff(value, strip=strip, item_id=ITEM_ID)


@pytest.mark.parametrize(
    ("value", "strip", "code"),
    (
        ("", 1, PlanningErrorCode.DIFF_INVALID),
        ("deleted file mode 100644\n", 1, PlanningErrorCode.DIFF_PATH_INVALID),
        ("--- a/file.txt\n", 1, PlanningErrorCode.DIFF_INVALID),
        ("--- a/file.txt\n+++ b/file.txt\n", 1, PlanningErrorCode.DIFF_INVALID),
        (
            "--- a/file.txt\n+++ b/file.txt\n@@ malformed\n",
            1,
            PlanningErrorCode.DIFF_INVALID,
        ),
        (
            '--- "a/file.txt"\n+++ "b/file.txt"\n@@ -1 +1 @@\n-old\n+new\n',
            1,
            PlanningErrorCode.DIFF_PATH_INVALID,
        ),
        (
            "--- a/file.txt\n+++ b/file.txt\n@@ -1 +1 @@\n-old\n+new\n",
            2,
            PlanningErrorCode.DIFF_PATH_INVALID,
        ),
        (
            "--- a/file.txt\n+++ b/file.txt\n@@ -1 +1 @@\n?invalid\n",
            1,
            PlanningErrorCode.DIFF_INVALID,
        ),
        (
            "--- a/file.txt\n+++ b/file.txt\n@@ -1,2 +1 @@\n-old\n+new\n",
            1,
            PlanningErrorCode.DIFF_INVALID,
        ),
        (
            "--- a/file.txt\n+++ b/file.txt\n@@ -1 +1 @@\n-old\n\\ unknown marker\n+new\n",
            1,
            PlanningErrorCode.DIFF_INVALID,
        ),
        (
            "--- a/file.txt\n+++ b/file.txt\n@@ -0,0 +0,0 @@\n\\ No newline at end of file\n",
            1,
            PlanningErrorCode.DIFF_INVALID,
        ),
        (
            "--- a/file.txt\n+++ b/file.txt\n@@ -0 +1 @@\n-old\n+new\n",
            1,
            PlanningErrorCode.DIFF_INVALID,
        ),
        (
            "--- a/file.txt\n+++ b/file.txt\n@@ -1 +0 @@\n-old\n+new\n",
            1,
            PlanningErrorCode.DIFF_INVALID,
        ),
    ),
)
def test_malformed_diff_variants_have_stable_codes(
    value: str,
    strip: int,
    code: PlanningErrorCode,
) -> None:
    with pytest.raises(PlanningError) as caught:
        parse(value, strip=strip)

    assert caught.value.code is code


def test_duplicate_file_patch_is_rejected() -> None:
    value = """\
--- a/file.txt
+++ b/file.txt
@@ -1 +1 @@
-one
+two
--- a/file.txt
+++ b/file.txt
@@ -1 +1 @@
-two
+three
"""

    with pytest.raises(PlanningError) as caught:
        parse(value)

    assert caught.value.code is PlanningErrorCode.DIFF_INVALID


def test_git_metadata_and_no_final_newline_markers_are_supported() -> None:
    value = """\
diff --git a/file.txt b/file.txt
index 1234567..89abcde 100644
--- a/file.txt
+++ b/file.txt
@@ -1 +1 @@
-old
\\ No newline at end of file
+new
\\ No newline at end of file
"""

    (file_diff,) = parse(value)

    assert file_diff.path == "file.txt"
    assert file_diff.hunks[0].old_lines == ("old",)
    assert file_diff.hunks[0].new_lines == ("new",)
    assert apply_file_diff("old", file_diff, item_id=ITEM_ID) == "new"


def test_context_no_final_newline_marker_updates_both_sides() -> None:
    value = """\
--- a/file.txt
+++ b/file.txt
@@ -1 +1 @@
 same
\\ No newline at end of file
"""

    (file_diff,) = parse(value)

    assert file_diff.hunks[0].old_lines == ("same",)
    assert file_diff.hunks[0].new_lines == ("same",)


def test_duplicate_no_final_newline_marker_is_rejected() -> None:
    value = """\
--- a/file.txt
+++ b/file.txt
@@ -1 +1 @@
-old
\\ No newline at end of file
\\ No newline at end of file
+new
"""

    with pytest.raises(PlanningError) as caught:
        parse(value)

    assert caught.value.code is PlanningErrorCode.DIFF_INVALID


def test_empty_existing_file_can_receive_an_insertion_hunk() -> None:
    value = """\
--- a/file.txt
+++ b/file.txt
@@ -0,0 +1 @@
+created content
"""

    (file_diff,) = parse(value)

    assert apply_file_diff("", file_diff, item_id=ITEM_ID) == "created content\n"


def test_zero_count_hunk_inserts_after_the_reported_old_line() -> None:
    value = """\
--- a/file.txt
+++ b/file.txt
@@ -1,0 +2 @@
+inserted
"""

    (file_diff,) = parse(value)

    assert apply_file_diff("first\nsecond\n", file_diff, item_id=ITEM_ID) == (
        "first\ninserted\nsecond\n"
    )


def test_out_of_range_and_overlapping_hunks_are_rejected() -> None:
    out_of_range = parse("--- a/file.txt\n+++ b/file.txt\n@@ -3 +3 @@\n-old\n+new\n")[0]
    with pytest.raises(PlanningError) as range_error:
        apply_file_diff("old\n", out_of_range, item_id=ITEM_ID)
    assert range_error.value.code is PlanningErrorCode.DIFF_HUNK_MISMATCH
    assert range_error.value.details == (
        "  hunk: 1",
        "  declared_old_start: 3",
        "  declared_old_lines: 1",
        "  source_lines: 1",
        "  previous_hunk_end: 0",
    )

    overlapping = parse("""\
--- a/file.txt
+++ b/file.txt
@@ -1 +1 @@
-one
+ONE
@@ -1 +1 @@
-one
+again
""")[0]
    with pytest.raises(PlanningError) as overlap_error:
        apply_file_diff("one\n", overlapping, item_id=ITEM_ID)
    assert overlap_error.value.code is PlanningErrorCode.DIFF_HUNK_MISMATCH
    assert "overlaps a previous hunk" in overlap_error.value.message


def test_hunk_count_error_reports_declared_and_actual_counts() -> None:
    value = """\
--- a/file.txt
+++ b/file.txt
@@ -1,3 +1,2 @@
-old
+new
"""

    with pytest.raises(PlanningError) as caught:
        parse(value)

    assert caught.value.code is PlanningErrorCode.DIFF_INVALID
    assert caught.value.message == "hunk 1 body line counts do not match its header"
    assert caught.value.details == (
        "  hunk: 1",
        "  declared_old_lines: 3",
        "  actual_old_lines: 1",
        "  declared_new_lines: 2",
        "  actual_new_lines: 1",
    )


def test_hunk_context_error_reports_first_mismatching_line() -> None:
    file_diff = parse(
        "--- a/file.txt\n+++ b/file.txt\n@@ -2,2 +2,2 @@\n expected\n-old\n+new\n"
    )[0]

    with pytest.raises(PlanningError) as caught:
        apply_file_diff("first\nactual\nold\n", file_diff, item_id=ITEM_ID)

    assert caught.value.code is PlanningErrorCode.DIFF_HUNK_MISMATCH
    assert caught.value.details == (
        "  hunk: 1",
        "  first_mismatch_line: 2",
        "  expected: 'expected'",
        "  actual: 'actual'",
    )


def test_context_diagnostic_skips_equal_lines_and_has_equal_fallback() -> None:
    assert diff_module._first_context_mismatch(
        ("same\n", "expected\n"),
        ("same\n", "actual\n"),
        first_line=7,
    ) == (
        "  first_mismatch_line: 8",
        "  expected: 'expected'",
        "  actual: 'actual'",
    )
    assert diff_module._first_context_mismatch(
        ("same\n",),
        ("same\n",),
        first_line=1,
    ) == ("  first_mismatch_line: unknown",)


def test_long_context_diagnostic_is_bounded() -> None:
    expected = "e" * 300
    actual = "a" * 300
    file_diff = parse(
        f"--- a/file.txt\n+++ b/file.txt\n@@ -1 +1 @@\n-{expected}\n+new\n"
    )[0]

    with pytest.raises(PlanningError) as caught:
        apply_file_diff(actual + "\n", file_diff, item_id=ITEM_ID)

    assert caught.value.details[2].endswith("...")
    assert caught.value.details[3].endswith("...")


def test_forbidden_metadata_after_a_zero_line_hunk_is_rejected() -> None:
    value = """\
--- a/file.txt
+++ b/file.txt
@@ -0,0 +0,0 @@
new file mode 100644
"""

    with pytest.raises(PlanningError) as caught:
        parse(value)

    assert caught.value.code is PlanningErrorCode.DIFF_PATH_INVALID
