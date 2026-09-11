"""Tests for lexical retrieval over blocks."""

from app.core.acp import AcpEngine, SearchResult
from app.core.acp.search import analyze, cjk_bigrams, search_blocks, stem


def test_cjk_bigram_path_hits_chinese_content() -> None:
    engine = AcpEngine()
    engine.add_messages([("m1", "退款政策说明"), ("m2", "发货时间安排")])
    result = engine.search_context("退款")
    assert isinstance(result, SearchResult)
    assert result.matched
    assert result.hits[0].block_id == 1
    assert "退款" in result.hits[0].matched_terms


def test_stemming_path_hits_english_content() -> None:
    engine = AcpEngine()
    engine.add_messages([("m1", "refund policy details"), ("m2", "shipping schedule")])
    result = engine.search_context("refunds")
    assert isinstance(result, SearchResult)
    assert result.matched
    assert result.hits[0].block_id == 1


def test_mixed_chinese_english_content_both_paths_hit() -> None:
    engine = AcpEngine()
    engine.add_messages([("m1", "退款政策 refund policy"), ("m2", "发货 shipping")])
    chinese = engine.search_context("退款")
    english = engine.search_context("refunds")
    assert chinese.matched and chinese.hits[0].block_id == 1
    assert english.matched and english.hits[0].block_id == 1


def test_no_hits_returns_empty_result_without_raising() -> None:
    engine = AcpEngine()
    engine.add_messages([("m1", "退款政策")])
    result = engine.search_context("量子计算")
    assert isinstance(result, SearchResult)
    assert not result.matched
    assert result.hits == ()
    assert result.total == 0


def test_fuzzy_ngram_matching_finds_close_terms() -> None:
    engine = AcpEngine()
    engine.add_messages([("m1", "refund policy details")])
    result = engine.search_context("refundpolicy")
    assert isinstance(result, SearchResult)
    assert result.matched


def test_stem_and_bigram_helpers() -> None:
    assert stem("refunds") == "refund"
    assert stem("running") == "runn"
    assert cjk_bigrams("退款政策") == ["退款", "款政", "政策"]
    terms = analyze("退款 refunds")
    assert terms["退款"] > 0
    assert terms["refund"] > 0


def test_search_blocks_ranks_best_match_first() -> None:
    engine = AcpEngine()
    engine.add_messages(
        [("m1", "shipping schedule"), ("m2", "refund policy and refund handling")]
    )
    result = engine.search_context("refund")
    assert isinstance(result, SearchResult)
    assert result.hits[0].block_id == 2


def test_search_blocks_top_k_truncation() -> None:
    engine = AcpEngine()
    engine.add_messages(
        [("m1", "refund a"), ("m2", "refund b"), ("m3", "refund c"), ("m4", "refund d")]
    )
    result = engine.search_context("refund", top_k=2)
    assert isinstance(result, SearchResult)
    assert len(result.hits) == 2
    assert result.total == 4
    assert result.truncated


def test_search_blocks_returns_all_hits_sorted() -> None:
    engine = AcpEngine()
    engine.add_messages([("m1", "alpha"), ("m2", "beta")])
    hits = search_blocks(engine.blocks(), "alpha")
    assert [hit.block_id for hit in hits] == [1]