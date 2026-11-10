from redux_build import text
from redux_build.text import mtp_counts, search


def test_search_extracts_match():
    assert search(r"Found \d+ error\w*", "Found 3 errors.", "x") == "Found 3 errors"


def test_search_falls_back_to_default():
    assert search(r"Found \d+ error\w*", "clean", "default") == "default"


# Microsoft Testing Platform's tail, as `dotnet test --project` prints it (with the colour codes
# it writes under CI=true when --no-ansi is not passed).
MTP_TAIL = (
    "\x1b[32mTest run summary: Passed!\x1b[m\n"
    "  total: 2\n  failed: 0\n  succeeded: 2\n  skipped: 0\n  duration: 1s 905ms\n"
)


def test_mtp_counts_read_the_summary_block():
    assert mtp_counts(MTP_TAIL) == {
        "total": 2,
        "failed": 0,
        "succeeded": 2,
        "skipped": 0,
    }


def test_mtp_counts_none_without_a_summary_block():
    assert mtp_counts("Passed!  - Failed:     0, Passed:     2") is None


def test_test_counts_understand_mtp():
    assert text.test_counts(MTP_TAIL) == "2 passed"


def test_test_counts_prefer_the_generic_shape():
    assert text.test_counts("1 failed, 8 passed\n" + MTP_TAIL) == "1 failed · 8 passed"
