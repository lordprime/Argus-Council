import pytest

from argus.tools.builtin import NotesWrite
from argus.tools.knowledge import KnowledgeSearch, MemoryWrite


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (["survey", "citations"], ["survey", "citations"]),
        ("survey, citations, reliability", ["survey", "citations", "reliability"]),
        ('["survey", "citations"]', ["survey", "citations"]),
        ("single", ["single"]),
        ("", []),
        (" , ,", []),
    ],
)
def test_notes_write_tags_accept_string_forms(raw, expected):
    args = NotesWrite.Args.model_validate({"title": "t", "body": "b", "tags": raw})
    assert args.tags == expected


def test_list_args_keep_array_schema_and_limits():
    # The model still sees a plain array; the leniency is validation-side only.
    schema = NotesWrite().spec().input_schema
    assert schema["properties"]["tags"]["type"] == "array"
    assert NotesWrite().parse({"title": "t", "body": "b", "tags": ",".join("abcdefghijk")}) is None


def test_knowledge_list_args_accept_strings():
    assert MemoryWrite.Args.model_validate({"text": "x", "tags": "a,b"}).tags == ["a", "b"]
    args = KnowledgeSearch.Args.model_validate({"query": "q", "knowledge_bases": "papers, news"})
    assert args.knowledge_bases == ["papers", "news"]
    assert KnowledgeSearch.Args.model_validate({"query": "q"}).knowledge_bases is None
