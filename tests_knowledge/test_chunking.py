import pytest


def tokenizer():
    from tokenizers import Tokenizer, models, pre_tokenizers

    value = Tokenizer(
        models.WordLevel(
            {
                "[UNK]": 0,
                **{f"w{i}": i + 1 for i in range(2000)},
                "۔": 2001,
                "؟": 2002,
                ".": 2003,
                "ہے": 2004,
            },
            unk_token="[UNK]",
        )
    )
    value.pre_tokenizer = pre_tokenizers.Whitespace()
    return value


def test_exact_overlap_headings_and_no_truncation():
    from knowledge.chunking import chunk_document
    from knowledge.parsing import ParsedDocument, Section

    text = " ".join(f"w{i}" for i in range(1100))
    chunks = chunk_document(ParsedDocument(text, (Section("Hours", text),), "en"), tokenizer())
    assert [c.token_count for c in chunks] == [500, 500, 200]
    assert chunks[1].text.split()[:50] == chunks[0].text.split()[-50:]
    assert chunks[2].text.endswith("w1099")
    assert [c.ordinal for c in chunks] == [0, 1, 2]
    assert all(c.heading == "Hours" for c in chunks)


def test_urdu_sentence_boundary_within_300_500_and_long_sentence_split():
    from knowledge.chunking import chunk_document
    from knowledge.parsing import ParsedDocument, Section

    text = " ".join(["ہے"] * 349) + "۔ " + " ".join(["ہے"] * 700) + "؟"
    chunks = chunk_document(ParsedDocument(text, (Section("اوقات", text),), "ur"), tokenizer())
    assert chunks[0].text.endswith("۔")
    assert chunks[0].token_count == 350
    assert chunks[-1].text.endswith("؟")
    assert all(c.token_count <= 500 and c.language == "ur" for c in chunks)


def test_chunk_limit_fails_whole_document():
    from knowledge.chunking import chunk_document
    from knowledge.parsing import IngestionError, ParsedDocument, Section

    text = " ".join(["w1"] * 116000)
    with pytest.raises(IngestionError, match="too_many_chunks"):
        chunk_document(ParsedDocument(text, (Section("", text),), "en"), tokenizer())


def test_dense_sentence_boundaries_finish_within_bounded_worker_time():
    import subprocess
    import sys

    code = """
from knowledge.chunking import chunk_document
from knowledge.parsing import ParsedDocument, Section
from tests_knowledge.test_chunking import tokenizer
text = 'w1. ' * 50000
chunks = chunk_document(ParsedDocument(text, (Section('', text),), 'en'), tokenizer())
assert len(chunks) == 223
assert chunks[-1].text.endswith('.')
"""
    subprocess.run([sys.executable, "-c", code], check=True, timeout=5, capture_output=True)


@pytest.mark.parametrize("format", ["md", "html", "docx"])
@pytest.mark.parametrize("only", [False, True])
def test_heading_facts_reach_ordered_chunks(format, only):
    from knowledge.chunking import chunk_document
    from knowledge.parsing import parse_bytes
    from tests_knowledge.test_parsing import heading_document

    chunks = chunk_document(parse_bytes(heading_document(format, only=only), format), tokenizer())
    assert [c.text for c in chunks] == [
        "Parent hours",
        "Weekdays" + ("" if only else "\nOpen 9 to 5"),
        "Emergency closed Sunday",
    ]
    assert [c.heading for c in chunks] == ["Parent hours", "Weekdays", "Emergency closed Sunday"]
    assert [c.ordinal for c in chunks] == [0, 1, 2]
