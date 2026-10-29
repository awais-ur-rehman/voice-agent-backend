"""Offset-preserving sentence windows; no decoding loss or silent truncation."""

import re
from bisect import bisect_left
from dataclasses import dataclass

from .parsing import IngestionError, ParsedDocument


@dataclass(frozen=True)
class ChunkInput:
    ordinal: int
    heading: str
    language: str
    text: str
    token_count: int


def chunk_document(document: ParsedDocument, tokenizer) -> list[ChunkInput]:
    chunks: list[ChunkInput] = []
    for section in document.sections:
        encoded = tokenizer.encode(section.text, add_special_tokens=False)
        offsets = encoded.offsets
        boundaries = [m.end() for m in re.finditer(r"[.!?۔؟](?:[\s]|$)", section.text)]
        start = 0
        while start < len(offsets):
            end = min(start + 500, len(offsets))
            if end < len(offsets):
                # Sentence punctuation belongs to the preceding chunk, including Urdu stops.
                for candidate in range(end, start + 299, -1):
                    position = bisect_left(boundaries, offsets[candidate - 1][1])
                    if position < len(boundaries) and boundaries[position] <= offsets[candidate][0]:
                        end = candidate
                        break
            while True:
                value = section.text[offsets[start][0] : offsets[end - 1][1]]
                count = len(tokenizer.encode(value, add_special_tokens=False).ids)
                total = len(tokenizer.encode(value).ids)
                if count <= 500 and total <= 512 and len(value) <= 32768:
                    break
                end -= 1
                if end <= start:
                    raise IngestionError("tokenizer_limit")
            if not value.strip() or not count:
                raise IngestionError("no_text")
            chunks.append(ChunkInput(len(chunks), section.heading, document.lang, value, total))
            if len(chunks) > 256:
                raise IngestionError("too_many_chunks")
            if end == len(offsets):
                break
            if end - start <= 50:
                raise IngestionError("tokenizer_limit")
            start = end - 50
    if not chunks:
        raise IngestionError("no_text")
    return chunks
