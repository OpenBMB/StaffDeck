"""`CitationMarkerStreamStripper`：SSE 分片下的引用角标剥离。"""

from __future__ import annotations

from app.knowledge.citations import (
    CitationMarkerStreamStripper,
    strip_citation_section,
)


def _feed_all(chunks: list[str]) -> str:
    stripper = CitationMarkerStreamStripper()
    return "".join(stripper.feed(chunk) for chunk in chunks)


def test_strips_markers_in_one_chunk():
    assert _feed_all(["制度答复 [1][2]，按标准执行。"]) == "制度答复，按标准执行。"


def test_strips_marker_split_across_chunks():
    # 模型逐 token 输出时 `[`、`1`、`]` 很可能落在不同分片里
    assert _feed_all(["制度答复 [", "1]"]) == "制度答复"
    assert _feed_all(["制度答复 [1", "]"]) == "制度答复"
    assert _feed_all(["制度答复 [1]", "，按标准执行。"]) == "制度答复，按标准执行。"


def test_split_marker_does_not_leave_trailing_space():
    assert _feed_all(["制度答复", " [", "1]", "，按标准执行。"]) == "制度答复，按标准执行。"


def test_keeps_markdown_links_and_code():
    assert _feed_all(["见 [1](https://example.com)"]) == "见 [1](https://example.com)"
    assert _feed_all(["`arr[1]` 是第二项 [2]"]) == "`arr[1]` 是第二项"


def test_code_fence_state_survives_chunk_boundaries():
    chunks = ["```python\n", "values = arr[1]\n", "[2]\n", "``", "`\n正文 [3]"]
    assert _feed_all(chunks) == "```python\nvalues = arr[1]\n[2]\n```\n正文"


def test_flush_releases_held_tail():
    stripper = CitationMarkerStreamStripper()
    assert stripper.feed("制度答复 [") == "制度答复"
    assert stripper.has_pending
    # 只有一个孤立的 `[`（编号前的空格也被一起压住）：不是完整编号，flush 时原样吐出
    assert stripper.flush() == " ["
    assert not stripper.has_pending


def test_reset_drops_pending_tail():
    stripper = CitationMarkerStreamStripper()
    stripper.feed("旧的 [1")
    stripper.reset()
    assert stripper.feed("新的 [1]") == "新的"


def test_strip_citation_section_removes_trailing_source_list():
    content = "制度答复 [1]。\n\n参考来源：[1] [2]"
    assert strip_citation_section(content) == "制度答复。"
