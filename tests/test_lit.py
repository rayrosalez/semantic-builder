import re

import pytest

from apply_changes import lit

ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "'": "'", '"': '"', "\\": "\\"}


def spark_unescape(literal: str) -> str:
    """What Databricks SQL makes of a '...' literal (default escapedStringLiterals=false)."""
    assert literal[0] == literal[-1] == "'"
    body = literal[1:-1]
    assert not re.search(r"(?<!\\)(\\\\)*'", body), "unescaped quote would end the literal"
    return re.sub(r"\\(.)", lambda m: ESCAPES.get(m.group(1), m.group(1)), body, flags=re.S)


@pytest.mark.parametrize("text", [
    "Customer's primary account",
    "Path like C:\\data\\new",
    "Ends with a backslash \\",
    "Line one\nLine two\r\nLine three",
    "Tabs\tand 'quotes' and \"doubles\" and \\' sneaky",
    "",
])
def test_lit_round_trips(text):
    out = lit(text)
    assert "\n" not in out and "\r" not in out
    assert spark_unescape(out) == text


def test_lit_none():
    assert lit(None) == "NULL"
