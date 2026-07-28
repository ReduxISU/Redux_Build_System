from redux_build.text import search


def test_search_extracts_match():
    assert search(r"Found \d+ error\w*", "Found 3 errors.", "x") == "Found 3 errors"


def test_search_falls_back_to_default():
    assert search(r"Found \d+ error\w*", "clean", "default") == "default"
