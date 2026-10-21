"""`_expand_related_chunks` 的边界：扩展必须受 max_chunks / 窗口上限约束。

背景（2026-10-21）：命中 chunk 所在 ``related_group`` 曾被整组返回，一篇 FAQ
就是一个组，``max_chunks=8`` 实测被撑到 17~19 条；检索载荷随即超过内联预算，
被换成沙箱 JSON 文件指针，模型只能 ``read_file`` 盲读再 ``read_knowledge_chunk``，
一次问答多两次模型往返。
"""

from __future__ import annotations

from app.db.models import KnowledgeChunk
from app.knowledge.service import (
    RELATED_CHUNK_MAX_COUNT,
    _expand_related_chunks,
)

GROUP_SIZE = 19


def _chunk(index: int, *, group: str | None = "group-qa") -> KnowledgeChunk:
    metadata: dict[str, object] = {"section_path": "段落组 1"}
    if group:
        metadata["related_group_id"] = group
        metadata["related_chunk_ids"] = [
            f"kchunk_{item:02d}" for item in range(GROUP_SIZE)
        ]
    return KnowledgeChunk(
        id=f"kchunk_{index:02d}",
        tenant_id="tenant_demo",
        knowledge_base_id="kb_demo",
        document_id="kdoc_1",
        bucket_id="kbucket_1",
        chunk_index=index,
        content=f"第 {index} 段正文",
        metadata_json=metadata,
    )


def _group() -> list[KnowledgeChunk]:
    return [_chunk(index) for index in range(GROUP_SIZE)]


def test_single_hit_expands_within_window_limit() -> None:
    chunks = _group()
    expanded = _expand_related_chunks([chunks[9]], chunks, max_chunks=8)
    assert len(expanded) <= RELATED_CHUNK_MAX_COUNT
    # 命中自身必须保留
    assert chunks[9].id in {chunk.id for chunk in expanded}


def test_expansion_never_exceeds_max_chunks_total() -> None:
    chunks = _group()
    # 8 个命中都落在同一组：整组 19 条曾全量返回
    hits = [chunks[index] for index in range(8)]
    expanded = _expand_related_chunks(hits, chunks, max_chunks=8)
    assert len(expanded) <= 8
    assert {hit.id for hit in hits} <= {chunk.id for chunk in expanded}


def test_direct_hits_are_never_dropped_by_the_limit() -> None:
    chunks = _group()
    hits = [chunks[index] for index in range(0, GROUP_SIZE, 2)]  # 10 个直接命中
    expanded = _expand_related_chunks(hits, chunks, max_chunks=3)
    assert {hit.id for hit in hits} <= {chunk.id for chunk in expanded}


def test_hits_without_group_keep_one_to_one_behaviour() -> None:
    chunks = [_chunk(index, group=None) for index in range(4)]
    expanded = _expand_related_chunks(chunks, chunks, max_chunks=8)
    assert [chunk.id for chunk in expanded] == [chunk.id for chunk in chunks]


def test_window_returns_contiguous_neighbours_of_the_hit() -> None:
    chunks = _group()
    expanded = _expand_related_chunks([chunks[9]], chunks, max_chunks=8)
    indexes = [chunk.chunk_index for chunk in expanded]
    assert indexes == list(range(min(indexes), min(indexes) + len(indexes)))
    assert 9 in indexes
