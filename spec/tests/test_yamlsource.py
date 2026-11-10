from pathlib import Path

import pytest

from iki_spec.problems import Location
from iki_spec.yamlsource import MAX_BYTES, parse_documents, read_documents


def one(text: str) -> object:
    documents, problems = parse_documents(text, "x.yaml")
    assert problems == []
    assert len(documents) == 1
    return documents[0].data


def test_reads_yaml_1_2_core_types() -> None:
    data = one(
        "a: no\nb: on\nc: off\nd: yes\ne: true\nf: False\ng: 010\nh: 0o10\ni: 0x1F\n"
        "j: 1.5\nk: 1e3\nl: .inf\nm: ~\nn:\no: 1:30\np: 2024-01-02\nq: '12'\nr: !!str 12\n"
    )
    assert data == {
        "a": "no",
        "b": "on",
        "c": "off",
        "d": "yes",
        "e": True,
        "f": False,
        "g": 10,
        "h": 8,
        "i": 31,
        "j": 1.5,
        "k": 1000.0,
        "l": float("inf"),
        "m": None,
        "n": None,
        "o": "1:30",
        "p": "2024-01-02",
        "q": "12",
        "r": "12",
    }


def test_remembers_where_values_and_keys_are() -> None:
    documents, _ = parse_documents("name: x\nfields:\n  seq:\n    type: string\n", "x.yaml")
    document = documents[0]
    assert document.locate(("fields", "seq", "type")) == Location(4, 11)
    assert document.locate(("fields", "seq", "type"), key=True) == Location(4, 5)
    # Missing paths fall back to the nearest ancestor.
    assert document.locate(("fields", "seq", "unit")) == Location(4, 5)
    assert document.locate(("nope",), key=True) == Location(1, 1)


def test_reads_several_documents() -> None:
    documents, problems = parse_documents("a: 1\n---\nb: 2\n", "x.yaml")
    assert problems == []
    assert [d.data for d in documents] == [{"a": 1}, {"b": 2}]
    assert [d.index for d in documents] == [0, 1]
    assert documents[1].start == Location(3, 1)


@pytest.mark.parametrize(
    ("text", "code", "line", "message"),
    [
        ("a: 1\nb: 2\na: 3\n", "yaml-duplicate-key", 3, "`a` appears twice here (first on line 1)"),
        ("a: &x 1\nb: *x\n", "yaml-alias", 2, "aliases"),
        ("a: &x {b: 1}\nc:\n  <<: *x\n", "yaml-alias", 3, "aliases"),
        ("a:\n  <<: {b: 1}\n", "yaml-merge-key", 2, "merge keys"),
        ("a: !!python/name:os.system x\n", "yaml-tag", 1, "tags"),
        ("a: !custom x\n", "yaml-tag", 1, "tags"),
        ("a: !!int '3'\n", "yaml-tag", 1, "tags"),
        ("? [a]\n: 1\n", "yaml-key", 1, "Keys must be plain text"),
        ("a: [1\n", "yaml-syntax", 2, "isn't valid YAML"),
        ("a:\n\tb: 1\n", "yaml-syntax", 2, "isn't valid YAML"),
    ],
)
def test_refuses_what_makes_manifests_unsafe_or_ambiguous(
    text: str, code: str, line: int, message: str
) -> None:
    _, problems = parse_documents(text, "x.yaml")
    assert [p.code for p in problems] == [code]
    assert problems[0].location is not None
    assert problems[0].location.line == line
    assert message in problems[0].message
    assert problems[0].file == "x.yaml"


def test_tabs_get_a_hint() -> None:
    _, problems = parse_documents("a:\n\tb: 1\n", "x.yaml")
    assert problems[0].hint == "Indent with spaces; YAML doesn't allow tabs."


def test_refuses_huge_files(tmp_path: Path) -> None:
    big = tmp_path / "big.yaml"
    big.write_bytes(b"#" * (MAX_BYTES + 1))
    _, problems = read_documents(big, "big.yaml")
    assert [p.code for p in problems] == ["file-too-large"]


def test_refuses_files_that_arent_utf8(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_bytes(b"a: \xff\n")
    _, problems = read_documents(bad, "bad.yaml")
    assert [p.code for p in problems] == ["not-utf8"]


def test_reports_unreadable_files(tmp_path: Path) -> None:
    _, problems = read_documents(tmp_path / "missing.yaml", "missing.yaml")
    assert [p.code for p in problems] == ["unreadable-file"]
