"""Lexical retrieval over blocks: stemming + CJK bigram + char n-gram.

No vector database is involved. Latin words are stemmed, CJK text is
indexed as bigrams (plus unigrams for short queries), and character
n-grams provide fuzzy matching for longer query terms.
"""

import re
from collections import Counter
from dataclasses import dataclass
from typing import Mapping, Sequence

from .blocks import Block

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_WORD_RE = re.compile(r"[a-zA-Z]+")
_STEM_SUFFIXES = ("ies", "es", "ed", "ing", "ly", "s")


def stem(word: str) -> str:
    """Strip common English suffixes for light stemming."""
    lowered = word.lower()
    for suffix in _STEM_SUFFIXES:
        if len(lowered) > 4 and lowered.endswith(suffix):
            if suffix == "ies":
                return lowered[:-3] + "y"
            return lowered[: -len(suffix)]
    return lowered


def cjk_bigrams(text: str) -> list[str]:
    """Bigrams over consecutive CJK characters."""
    chars = _CJK_RE.findall(text)
    return [chars[i] + chars[i + 1] for i in range(len(chars) - 1)]


def cjk_unigrams(text: str) -> list[str]:
    """Individual CJK characters (for short queries)."""
    return _CJK_RE.findall(text)


def char_ngrams(text: str, size: int = 3) -> set[str]:
    """Character n-grams over the whole text for fuzzy matching."""
    normalized = re.sub(r"\s+", "", text.lower())
    if len(normalized) < size:
        return {normalized} if normalized else set()
    return {normalized[i : i + size] for i in range(len(normalized) - size + 1)}


def analyze(text: str, ngram_size: int = 3) -> Counter[str]:
    """Weighted term analysis of ``text`` for scoring."""
    terms: Counter[str] = Counter()
    for word in _WORD_RE.findall(text):
        terms[stem(word)] += 2
    for bigram in cjk_bigrams(text):
        terms[bigram] += 1.5
    for char in cjk_unigrams(text):
        terms[char] += 1
    return terms


@dataclass(frozen=True)
class SearchHit:
    """A retrieval hit linked to its block."""

    block_id: int
    message_id: str
    tier: int
    snippet: str
    score: float
    matched_terms: tuple[str, ...]
    source: str = "visible"


def _match_score(
    query_terms: Counter[str],
    content_terms: Counter[str],
    query_ngrams: set[str],
    content_text: str,
    ngram_size: int,
) -> float:
    score = 0.0
    for term, weight in query_terms.items():
        if content_terms[term] > 0:
            score += weight
    if query_ngrams:
        overlap = query_ngrams & char_ngrams(content_text, size=ngram_size)
        score += len(overlap) * 0.5
    return score


def _snippet(text: str, query: str, radius: int = 40) -> str:
    lowered = text.lower()
    for term in analyze(query):
        index = lowered.find(term)
        if index >= 0:
            start = max(0, index - radius)
            end = min(len(text), index + len(term) + radius)
            prefix = "…" if start > 0 else ""
            suffix = "…" if end < len(text) else ""
            return f"{prefix}{text[start:end]}{suffix}"
    return text[: radius * 2]


def search_blocks(
    blocks: Sequence[Block],
    query: str,
    *,
    min_score: float = 0.0,
    ngram_size: int = 3,
    hidden_texts: Mapping[int, str] | None = None,
) -> list[SearchHit]:
    """Rank blocks by lexical relevance to ``query``.

    Each block yields at most one hit, sourced from either its visible
    content or its hidden (checkpointed) content, whichever scores higher.
    Hits are sorted by descending score.
    """
    query_terms = analyze(query, ngram_size=ngram_size)
    query_ngrams = char_ngrams(query, size=ngram_size)
    hidden_texts = hidden_texts or {}
    hits: list[SearchHit] = []
    for block in blocks:
        visible_terms = analyze(block.content, ngram_size=ngram_size)
        visible_score = _match_score(
            query_terms, visible_terms, query_ngrams, block.content, ngram_size
        )
        hidden = hidden_texts.get(block.block_id, "")
        hidden_terms = analyze(hidden, ngram_size=ngram_size) if hidden else Counter()
        hidden_score = (
            _match_score(query_terms, hidden_terms, query_ngrams, hidden, ngram_size)
            if hidden
            else 0.0
        )
        if visible_score <= 0 and hidden_score <= 0:
            continue
        if hidden_score > visible_score:
            score, terms, source, snippet_text = hidden_score, hidden_terms, "hidden", hidden
        else:
            score, terms, source, snippet_text = (
                visible_score, visible_terms, "visible", block.content
            )
        matched = tuple(sorted(term for term in query_terms if terms[term] > 0))
        hits.append(
            SearchHit(
                block_id=block.block_id,
                message_id=block.message_id,
                tier=block.tier,
                snippet=_snippet(snippet_text, query),
                score=score,
                matched_terms=matched,
                source=source,
            )
        )
    hits.sort(key=lambda hit: hit.score, reverse=True)
    if min_score > 0:
        hits = [hit for hit in hits if hit.score >= min_score]
    return hits