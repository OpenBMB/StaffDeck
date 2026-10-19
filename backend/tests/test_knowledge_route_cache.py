"""知识路由决策缓存（route_cache）测试。

覆盖：
- query_norm 归一聚合（相似问法同 key）
- route_cache_slot 构造边界 / 维度与版本参与 key
- 命中：跳过 LLM 路由，按 max_* 截断
- 写回：LLM 决策成功后按维度独立写回（另一维失败不影响本维）
- 词法快速路径 / LLM 失败词法兜底的结果不回写
- 命中但过滤为空 → 视为失效并回退 LLM（不再静默返回空）
- 按库精准失效（不误伤其它库 / 其它租户）
- Redis 不可用（get_redis 返回 None）时全链路与无缓存一致
"""

from __future__ import annotations

import fnmatch

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.db.models import (
    KnowledgeBase,
    KnowledgeBucket,
    KnowledgeChunk,
    KnowledgeDocument,
    ModelConfig,
    Tenant,
)
from app.knowledge.route_cache import (
    ALL_KB_SCOPE,
    KIND_BUCKET,
    KIND_DOCUMENT,
    get_route_decision,
    invalidate_knowledge_base,
    query_norm,
    route_cache_slot,
    store_route_decision,
)
from app.knowledge.schema import KnowledgeSearchRequest
from app.knowledge import service as knowledge_service
from app.knowledge.service import KnowledgeService, _stable_cache_query


class _FakeRedis:
    """内存版 redis 客户端（decode_responses 语义：key/value 均为 str）。"""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}

    def get(self, key: str):
        return self.data.get(key)

    def set(self, key: str, value: str, ex=None):  # noqa: ARG002
        self.data[key] = value

    def delete(self, *keys: str) -> None:
        for key in keys:
            self.data.pop(key, None)
            self.sets.pop(key, None)

    def scan_iter(self, match: str | None = None, count: int | None = None):  # noqa: ARG002
        keys = list(self.data) + list(self.sets)
        return [key for key in keys if fnmatch.fnmatchcase(key, match)]

    def sadd(self, key: str, *values: str) -> None:
        self.sets.setdefault(key, set()).update(values)

    def smembers(self, key: str) -> set[str]:
        return set(self.sets.get(key, set()))

    def expire(self, key: str, seconds: int) -> bool:  # noqa: ARG002
        return True


@pytest.fixture()
def fake_redis(monkeypatch):
    """把 object_cache 的 Redis 底座替换为内存实现。"""

    import app.object_cache as object_cache

    client = _FakeRedis()
    monkeypatch.setattr(object_cache, "get_redis", lambda: client)
    return client


@pytest.fixture(autouse=True)
def _disable_small_candidate_shortcut(monkeypatch):
    """默认关掉小候选短路。

    本文件既有用例的 fixture 全是 1-3 chunk 的小库，短路（默认阈值 12）
    会在缓存未命中后直接全读，LLM 路由分支永远走不到——那正是这些用例
    要覆盖的行为。需要测短路本身的用例自行把阈值改回正值。
    """

    monkeypatch.setattr(knowledge_service, "SMALL_CANDIDATE_CHUNK_LIMIT", -1)


def _test_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _seed_ambiguous_fixture(db, document_count: int = 2) -> None:
    """标题词法高度相似的文档+桶+chunk，强制走 LLM 路由分支。"""

    db.add(Tenant(id="tenant_demo", name="Demo"))
    db.add(KnowledgeBase(id="kb_demo", tenant_id="tenant_demo", name="默认知识库"))
    titles = [
        ("员工手册", "入职流程与考勤规则说明"),
        ("员工手册附录", "考勤与假期规则补充"),
        ("员工手册补充", "考勤规则补充说明"),
    ][:document_count]
    for index, (title, summary) in enumerate(titles):
        document = KnowledgeDocument(
            id=f"kdoc_{index}",
            tenant_id="tenant_demo",
            knowledge_base_id="kb_demo",
            filename=f"doc{index}.md",
            file_type="md",
            title=title,
            status="ready",
            metadata_json={"document_card": {"title": title, "summary": summary}},
        )
        bucket = KnowledgeBucket(
            id=f"kbucket_{index}",
            tenant_id="tenant_demo",
            knowledge_base_id="kb_demo",
            document_id=document.id,
            bucket_key=f"section_{index}",
            title=title,
            summary=summary,
        )
        chunk = KnowledgeChunk(
            tenant_id="tenant_demo",
            knowledge_base_id="kb_demo",
            document_id=document.id,
            bucket_id=bucket.id,
            chunk_index=0,
            content=summary,
        )
        db.add(document)
        db.add(bucket)
        db.add(chunk)
    db.commit()


def _route_model() -> ModelConfig:
    return ModelConfig(id="model_route", tenant_id="tenant_demo", name="Route", model="route")


def _doc_slot(query: str = "员工手册", *, version_ids=None):
    return route_cache_slot(
        "tenant_demo",
        None,
        ["kb_demo"],
        query,
        kind=KIND_DOCUMENT,
        knowledge_base_version_ids=version_ids,
    )


def _bucket_slot(query: str = "员工手册", *, version_ids=None):
    return route_cache_slot(
        "tenant_demo",
        None,
        ["kb_demo"],
        query,
        kind=KIND_BUCKET,
        knowledge_base_version_ids=version_ids,
    )


def _run_search(db, query: str = "员工手册", **overrides):
    payload = {
        "tenant_id": "tenant_demo",
        "knowledge_base_ids": ["kb_demo"],
        "query": query,
        "mode": "chat",
    }
    payload.update(overrides)
    return KnowledgeService(db).search(KnowledgeSearchRequest(**payload), _route_model())


def _phases(response) -> list[str]:
    return [str(item.get("phase")) for item in response.route_trace]


# ---------------------------------------------------------------------------
# 纯函数：query_norm / route_cache_slot
# ---------------------------------------------------------------------------


def test_query_norm_aggregates_similar_phrasings() -> None:
    assert query_norm("如何申领电脑") == query_norm("怎么申领电脑")
    assert query_norm("如何申领电脑") == query_norm("我想知道如何申领电脑？")
    assert query_norm("如何申领电脑") == query_norm("申领电脑")
    assert query_norm("如何申领电脑") == query_norm(" 请问 如何 申领 电脑 ？")
    # 空白/标点/大小写
    assert query_norm("Hello World！") == "helloworld"


def test_route_cache_slot_boundaries() -> None:
    # 空租户 / 空 query / 纯噪声 query → None
    assert route_cache_slot("", None, ["kb"], "查询", kind=KIND_DOCUMENT) is None
    assert route_cache_slot("tenant", None, ["kb"], "", kind=KIND_DOCUMENT) is None
    assert route_cache_slot("tenant", None, ["kb"], "？？？", kind=KIND_DOCUMENT) is None

    # 基准：tenant:kind:agent:digest
    slot = route_cache_slot("tenant_demo", "agent_a", ["kb_b", "kb_a"], "如何申领电脑", kind=KIND_DOCUMENT)
    assert slot is not None
    assert slot.key.startswith("tenant_demo:doc:agent_a:")
    assert slot.kb_scopes == ("kb_a", "kb_b")

    # agent 缺省 → "-"
    assert route_cache_slot("tenant_demo", None, ["kb"], "查询", kind=KIND_DOCUMENT).key.split(":")[2] == "-"
    # kb 顺序无关
    assert route_cache_slot("t", None, ["kb1", "kb2"], "查询", kind=KIND_DOCUMENT) == route_cache_slot(
        "t", None, ["kb2", "kb1"], "查询", kind=KIND_DOCUMENT
    )
    # query_norm 聚合
    assert route_cache_slot("t", None, ["kb"], "如何申领电脑", kind=KIND_DOCUMENT) == route_cache_slot(
        "t", None, ["kb"], "我想知道怎么申领电脑？", kind=KIND_DOCUMENT
    )


def test_route_cache_slot_dimension_and_version_isolate_keys() -> None:
    """文档/索引两维、以及知识库版本维度都必须产生不同 key。"""

    base = _doc_slot("员工手册")
    assert base != _bucket_slot("员工手册")  # 维度隔离
    assert base != _doc_slot("员工手册", version_ids=["kbv_2"])  # 版本隔离
    assert _doc_slot("员工手册", version_ids=["kbv_2"]) == _doc_slot(
        "员工手册", version_ids=["kbv_2"]
    )
    # 版本顺序无关
    assert _doc_slot("员工手册", version_ids=["kbv_2", "kbv_1"]) == _doc_slot(
        "员工手册", version_ids=["kbv_1", "kbv_2"]
    )


def test_route_cache_slot_scope_falls_back_to_all(fake_redis) -> None:
    """无显式知识库维度时登记到 _all_ scope，仍可被任意库变更失效。"""

    slot = route_cache_slot("tenant_demo", "agent_a", [], "员工手册", kind=KIND_DOCUMENT)
    assert slot is not None
    assert slot.kb_scopes == (ALL_KB_SCOPE,)
    store_route_decision(slot, ["kdoc_0"])
    invalidate_knowledge_base("tenant_demo", "kb_demo")
    assert get_route_decision(slot) is None


# ---------------------------------------------------------------------------
# 集成：命中 / 写回 / 不回写 / 失效 / Redis 不可用
# ---------------------------------------------------------------------------


def test_route_cache_hit_skips_llm_routing(fake_redis, monkeypatch) -> None:
    """命中缓存：直接用缓存 ID，LLM 路由完全不被调用。"""

    def _forbidden(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("LLM routing must not be called on route cache hit")

    store_route_decision(_doc_slot(), ["kdoc_1"])
    store_route_decision(_bucket_slot(), ["kbucket_1"])
    monkeypatch.setattr(KnowledgeService, "_select_documents_with_llm", _forbidden)
    monkeypatch.setattr(KnowledgeService, "_select_buckets_with_llm", _forbidden)

    with _test_session() as db:
        _seed_ambiguous_fixture(db)
        response = _run_search(db, "员工手册")

    phases = _phases(response)
    assert "route_cache_hit" in phases
    assert "document_route_cache_hit" in phases
    assert "bucket_route_cache_hit" in phases
    assert "route_cache_stored" not in phases
    assert [bucket.id for bucket in response.selected_buckets] == ["kbucket_1"]


def test_route_cache_hit_truncates_to_max_buckets(fake_redis, monkeypatch) -> None:
    """命中缓存也要按当次 max_buckets 截断（旧实现会返回上次缓存的全部）。"""

    def _forbidden(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("LLM routing must not be called on route cache hit")

    store_route_decision(_doc_slot(), ["kdoc_0", "kdoc_1", "kdoc_2"])
    store_route_decision(_bucket_slot(), ["kbucket_0", "kbucket_1", "kbucket_2"])
    monkeypatch.setattr(KnowledgeService, "_select_documents_with_llm", _forbidden)
    monkeypatch.setattr(KnowledgeService, "_select_buckets_with_llm", _forbidden)

    with _test_session() as db:
        _seed_ambiguous_fixture(db, document_count=3)
        response = _run_search(db, "员工手册", max_buckets=1)

    assert [bucket.id for bucket in response.selected_buckets] == ["kbucket_0"]


def test_route_cache_stale_falls_back_to_llm(fake_redis, monkeypatch) -> None:
    """缓存决策与当前候选集不匹配（全部被过滤）→ 回退 LLM，不再静默返回空。"""

    store_route_decision(_doc_slot(), ["kdoc_已删除"])  # 指向不存在的文档
    calls: list[str] = []

    def fake_select_documents(*args, **kwargs):  # noqa: ANN002, ANN003
        calls.append("document")
        return ["kdoc_0"]

    def fake_select_buckets(*args, **kwargs):  # noqa: ANN002, ANN003
        calls.append("bucket")
        return ["kbucket_0"]

    monkeypatch.setattr(KnowledgeService, "_select_documents_with_llm", fake_select_documents)
    monkeypatch.setattr(KnowledgeService, "_select_buckets_with_llm", fake_select_buckets)

    with _test_session() as db:
        _seed_ambiguous_fixture(db)
        response = _run_search(db, "员工手册")

    phases = _phases(response)
    assert "route_cache_stale" in phases
    assert "document_route_cache_hit" not in phases
    assert "document" in calls
    assert [bucket.id for bucket in response.selected_buckets] == ["kbucket_0"]


def test_route_cache_stored_after_llm_decision(fake_redis, monkeypatch) -> None:
    """未命中 → LLM 决策成功 → 写回；相同问法第二次搜索命中。"""

    monkeypatch.setattr(
        KnowledgeService, "_select_documents_with_llm", lambda *a, **k: ["kdoc_0", "kdoc_1"]
    )
    monkeypatch.setattr(
        KnowledgeService, "_select_buckets_with_llm", lambda *a, **k: ["kbucket_0"]
    )

    with _test_session() as db:
        _seed_ambiguous_fixture(db)
        response = _run_search(db, "员工手册")
        phases = _phases(response)
        assert phases.count("route_cache_stored") == 2  # 两个维度各自写回
        assert get_route_decision(_doc_slot()) == ["kdoc_0", "kdoc_1"]
        assert get_route_decision(_bucket_slot()) == ["kbucket_0"]

    def _forbidden(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("LLM routing must not be called on route cache hit")

    monkeypatch.setattr(KnowledgeService, "_select_documents_with_llm", _forbidden)
    monkeypatch.setattr(KnowledgeService, "_select_buckets_with_llm", _forbidden)

    with _test_session() as db:
        _seed_ambiguous_fixture(db)
        response = _run_search(db, "我想知道员工手册？")  # 噪声词变体同 key

    phases = _phases(response)
    assert "route_cache_hit" in phases
    assert [bucket.id for bucket in response.selected_buckets] == ["kbucket_0"]


def test_route_cache_dimensions_write_independently(fake_redis, monkeypatch) -> None:
    """文档维走 LLM 成功、索引维 LLM 失败走词法兜底 → 文档决策照样缓存。

    旧实现要求「两路都走 LLM 成功」才写，这种组合一个字都不缓存。
    """

    monkeypatch.setattr(
        KnowledgeService, "_select_documents_with_llm", lambda *a, **k: ["kdoc_0"]
    )
    monkeypatch.setattr(KnowledgeService, "_select_buckets_with_llm", lambda *a, **k: None)

    with _test_session() as db:
        _seed_ambiguous_fixture(db)
        response = _run_search(db, "员工手册")

    phases = _phases(response)
    assert "route_cache_stored" in phases
    assert get_route_decision(_doc_slot()) == ["kdoc_0"]
    assert get_route_decision(_bucket_slot()) is None  # 词法兜底不回写


def test_route_cache_not_written_on_lexical_fast_path(fake_redis, monkeypatch) -> None:
    """词法快速路径的选择不回写缓存（避免固化非 LLM 决策）。"""

    def _forbidden(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("LLM routing must not be called on decisive lexical match")

    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(KnowledgeBase(id="kb_demo", tenant_id="tenant_demo", name="默认知识库"))
        document = KnowledgeDocument(
            id="kdoc_frontend",
            tenant_id="tenant_demo",
            knowledge_base_id="kb_demo",
            filename="frontend.md",
            file_type="md",
            title="前端编码规范",
            status="ready",
            metadata_json={
                "document_card": {"title": "前端编码规范", "summary": "前端编码规范与组件命名规范。"}
            },
        )
        bucket = KnowledgeBucket(
            id="kbucket_frontend",
            tenant_id="tenant_demo",
            knowledge_base_id="kb_demo",
            document_id=document.id,
            bucket_key="frontend",
            title="前端编码规范",
            summary="Vue 3、Vite、TypeScript、组件编写和命名规范。",
        )
        chunk = KnowledgeChunk(
            tenant_id="tenant_demo",
            knowledge_base_id="kb_demo",
            document_id=document.id,
            bucket_id=bucket.id,
            chunk_index=0,
            content="前端编码规范包括 Vue 3、Vite、TypeScript 和组件命名规范。",
        )
        db.add_all([document, bucket, chunk])
        db.commit()
        monkeypatch.setattr(KnowledgeService, "_select_documents_with_llm", _forbidden)
        monkeypatch.setattr(KnowledgeService, "_select_buckets_with_llm", _forbidden)

        response = _run_search(db, "前端编码规范")

    phases = _phases(response)
    assert "document_route_lexical_fast_path" in phases
    assert "bucket_route_lexical_fast_path" in phases
    assert "route_cache_stored" not in phases
    assert get_route_decision(_doc_slot("前端编码规范")) is None
    assert get_route_decision(_bucket_slot("前端编码规范")) is None


def test_route_cache_not_written_on_llm_failure_fallback(fake_redis, monkeypatch) -> None:
    """LLM 失败走词法兜底时，兜底选择不回写缓存。"""

    monkeypatch.setattr(KnowledgeService, "_select_documents_with_llm", lambda *a, **k: None)
    monkeypatch.setattr(KnowledgeService, "_select_buckets_with_llm", lambda *a, **k: None)

    with _test_session() as db:
        _seed_ambiguous_fixture(db)
        response = _run_search(db, "员工手册")

    phases = _phases(response)
    assert "document_route_lexical_fallback" in phases
    assert "route_cache_stored" not in phases
    assert get_route_decision(_doc_slot()) is None
    assert get_route_decision(_bucket_slot()) is None


def test_route_cache_invalidation_scoped_to_kb(fake_redis) -> None:
    """失效按库精准删除：不误伤其它库、其它租户，也不整租户清空。"""

    slot_a = route_cache_slot("tenant_demo", None, ["kb_a"], "如何申领电脑", kind=KIND_DOCUMENT)
    slot_a_bucket = route_cache_slot("tenant_demo", None, ["kb_a"], "如何申领电脑", kind=KIND_BUCKET)
    slot_b = route_cache_slot("tenant_demo", None, ["kb_b"], "如何申领电脑", kind=KIND_DOCUMENT)
    slot_agent = route_cache_slot("tenant_demo", "agent_x", [], "如何申领电脑", kind=KIND_DOCUMENT)
    slot_other_tenant = route_cache_slot(
        "tenant_other", None, ["kb_a"], "如何申领电脑", kind=KIND_DOCUMENT
    )
    for slot, ids in (
        (slot_a, ["d_a"]),
        (slot_a_bucket, ["b_a"]),
        (slot_b, ["d_b"]),
        (slot_agent, ["d_all"]),
        (slot_other_tenant, ["d_other"]),
    ):
        assert store_route_decision(slot, ids) is True

    invalidate_knowledge_base("tenant_demo", "kb_a")

    assert get_route_decision(slot_a) is None
    assert get_route_decision(slot_a_bucket) is None
    assert get_route_decision(slot_agent) is None  # _all_ scope 一并失效
    assert get_route_decision(slot_b) == ["d_b"]  # 其它库不受影响
    assert get_route_decision(slot_other_tenant) == ["d_other"]  # 其它租户不受影响


def test_route_cache_invalidation_without_kb_clears_tenant(fake_redis) -> None:
    """不指定库时按租户全量失效（保留给「整库清空」类调用）。"""

    slot_a = route_cache_slot("tenant_demo", None, ["kb_a"], "如何申领电脑", kind=KIND_DOCUMENT)
    slot_b = route_cache_slot("tenant_demo", None, ["kb_b"], "如何申领电脑", kind=KIND_DOCUMENT)
    slot_other = route_cache_slot("tenant_other", None, ["kb_a"], "如何申领电脑", kind=KIND_DOCUMENT)
    store_route_decision(slot_a, ["d_a"])
    store_route_decision(slot_b, ["d_b"])
    store_route_decision(slot_other, ["d_other"])

    invalidate_knowledge_base("tenant_demo", None)

    assert get_route_decision(slot_a) is None
    assert get_route_decision(slot_b) is None
    assert get_route_decision(slot_other) == ["d_other"]


def test_route_cache_search_works_without_redis(monkeypatch) -> None:
    """Redis 不可用：读 None、写静默跳过，行为与无缓存完全一致。"""

    import app.object_cache as object_cache

    monkeypatch.setattr(object_cache, "get_redis", lambda: None)
    monkeypatch.setattr(
        KnowledgeService, "_select_documents_with_llm", lambda *a, **k: ["kdoc_0"]
    )
    monkeypatch.setattr(
        KnowledgeService, "_select_buckets_with_llm", lambda *a, **k: ["kbucket_0"]
    )

    with _test_session() as db:
        _seed_ambiguous_fixture(db)
        response = _run_search(db, "员工手册")

    phases = _phases(response)
    assert "route_cache_hit" not in phases
    assert "route_cache_stored" not in phases
    assert [bucket.id for bucket in response.selected_buckets] == ["kbucket_0"]

# ---------------------------------------------------------------------------
# 稳定维度（用户原话）：同问法换措辞也应命中
# ---------------------------------------------------------------------------


def test_stable_cache_query_helper() -> None:
    """归一后相同不留第二个位点；无原话/纯空白不参与。"""

    assert _stable_cache_query("员工手册 考勤", "考勤怎么算") == "考勤怎么算"
    assert _stable_cache_query("员工手册", "员工手册？") is None
    assert _stable_cache_query("员工手册", "   ") is None
    assert _stable_cache_query("员工手册", None) is None


def test_route_cache_hits_across_query_rewrites(fake_redis, monkeypatch) -> None:
    """模型每轮改写的 query 不同、用户原话相同 → 仍应命中缓存。

    这是「同一个问题问第二遍还要 16s」的根因回归：缓存 key 若只绑
    模型改写的 query，措辞一变就全 miss。
    """

    calls = {"doc": 0, "bucket": 0}

    def fake_docs(*_args, **_kwargs):
        calls["doc"] += 1
        return ["kdoc_0"]

    def fake_buckets(*_args, **_kwargs):
        calls["bucket"] += 1
        return ["kbucket_0"]

    monkeypatch.setattr(KnowledgeService, "_select_documents_with_llm", fake_docs)
    monkeypatch.setattr(KnowledgeService, "_select_buckets_with_llm", fake_buckets)

    with _test_session() as db:
        _seed_ambiguous_fixture(db)
        first = _run_search(
            db, query="员工手册 考勤 规则", cache_query="考勤怎么算"
        )
        second = _run_search(
            db, query="考勤规定一览", cache_query="考勤怎么算"
        )

    assert calls == {"doc": 1, "bucket": 1}, "第二轮换了措辞但原话相同，不该再调模型路由"
    assert "route_cache_stored" in _phases(first)
    second_phases = _phases(second)
    assert "document_route_cache_hit" in second_phases
    assert "bucket_route_cache_hit" in second_phases


def test_stable_and_query_slots_both_written(fake_redis, monkeypatch) -> None:
    """缓存写两份位点（原话 + query），两份都能独立命中。"""

    monkeypatch.setattr(
        KnowledgeService, "_select_documents_with_llm", lambda *a, **k: ["kdoc_0"]
    )
    monkeypatch.setattr(
        KnowledgeService, "_select_buckets_with_llm", lambda *a, **k: ["kbucket_0"]
    )

    with _test_session() as db:
        _seed_ambiguous_fixture(db)
        _run_search(db, query="员工手册 考勤 规则", cache_query="考勤怎么算")

    assert get_route_decision(_doc_slot("员工手册 考勤 规则")) == ["kdoc_0"]
    assert get_route_decision(_doc_slot("考勤怎么算")) == ["kdoc_0"]


def test_negative_route_decision_is_cached(fake_redis, monkeypatch) -> None:
    """模型明确判定「没有相关的内部索引」也要缓存，否则每次都白问一遍。"""

    calls = {"doc": 0, "bucket": 0}

    def fake_docs(*_args, **_kwargs):
        calls["doc"] += 1
        return ["kdoc_0"]

    def empty_buckets(*_args, **_kwargs):
        calls["bucket"] += 1
        return []

    monkeypatch.setattr(KnowledgeService, "_select_documents_with_llm", fake_docs)
    monkeypatch.setattr(KnowledgeService, "_select_buckets_with_llm", empty_buckets)

    with _test_session() as db:
        _seed_ambiguous_fixture(db)
        first = _run_search(db, query="员工手册 考勤 规则", cache_query="考勤怎么算")
        second = _run_search(db, query="考勤规定一览", cache_query="考勤怎么算")

    assert "route_cache_stored" in _phases(first)
    assert calls == {"doc": 1, "bucket": 1}, "负决策也应命中缓存，不该重复问模型"
    second_phases = _phases(second)
    assert "bucket_route_cache_hit_empty" in second_phases
    assert "route_cache_stale" not in second_phases


def test_negative_decision_not_written_on_llm_failure(fake_redis, monkeypatch) -> None:
    """模型调用失败（None）不写负缓存：失败≠「没有相关候选」。"""

    monkeypatch.setattr(KnowledgeService, "_select_documents_with_llm", lambda *a, **k: ["kdoc_0"])
    monkeypatch.setattr(KnowledgeService, "_select_buckets_with_llm", lambda *a, **k: None)

    with _test_session() as db:
        _seed_ambiguous_fixture(db)
        _run_search(db, query="员工手册 考勤 规则", cache_query="考勤怎么算")

    # 两个位点都不该写入「空桶决策」
    assert get_route_decision(_bucket_slot("考勤怎么算")) is None
    assert get_route_decision(_bucket_slot("员工手册 考勤 规则")) is None


# ---------------------------------------------------------------------------
# 小候选短路（bucket_route_small_candidate_shortcut）
# ---------------------------------------------------------------------------


def test_small_candidate_shortcut_reads_all_buckets(fake_redis, monkeypatch) -> None:
    """候选 chunk 总数 ≤ 阈值 → 跳过 LLM 桶路由全读，绕过 max_buckets，不写缓存。"""

    monkeypatch.setattr(knowledge_service, "SMALL_CANDIDATE_CHUNK_LIMIT", 12)
    monkeypatch.setattr(
        KnowledgeService, "_select_documents_with_llm", lambda *a, **k: ["kdoc_0", "kdoc_1"]
    )

    def _forbidden(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("small-candidate shortcut must skip LLM bucket routing")

    monkeypatch.setattr(KnowledgeService, "_select_buckets_with_llm", _forbidden)

    with _test_session() as db:
        _seed_ambiguous_fixture(db)
        # max_buckets=1：短路有意绕过截断（下游 rank/diverse 按 max_chunks 收敛）
        response = _run_search(db, "员工手册", max_buckets=1)

    phases = _phases(response)
    assert "bucket_route_small_candidate_shortcut" in phases
    assert "bucket_route_lexical_fast_path" not in phases
    assert [bucket.id for bucket in response.selected_buckets] == ["kbucket_0", "kbucket_1"]
    # 短路不写路由缓存：~1ms 的 COUNT 不值得引入「库内容已变」的过期风险
    assert get_route_decision(_bucket_slot("员工手册")) is None


def test_small_candidate_shortcut_disabled_above_limit(fake_redis, monkeypatch) -> None:
    """候选 chunk 总数超过阈值 → 不触发短路，照旧走 LLM 桶路由。"""

    monkeypatch.setattr(knowledge_service, "SMALL_CANDIDATE_CHUNK_LIMIT", 1)
    monkeypatch.setattr(
        KnowledgeService, "_select_documents_with_llm", lambda *a, **k: ["kdoc_0", "kdoc_1"]
    )
    monkeypatch.setattr(
        KnowledgeService, "_select_buckets_with_llm", lambda *a, **k: ["kbucket_0"]
    )

    with _test_session() as db:
        _seed_ambiguous_fixture(db)  # 2 个桶各 1 chunk = 2 > 1
        response = _run_search(db, "员工手册")

    assert "bucket_route_small_candidate_shortcut" not in _phases(response)
    assert [bucket.id for bucket in response.selected_buckets] == ["kbucket_0"]


def test_candidate_chunk_counts_helper() -> None:
    """GROUP BY 计数：同桶多 chunk 正确聚合；空桶不出现；空入参返回空。"""

    with _test_session() as db:
        _seed_ambiguous_fixture(db)
        db.add(
            KnowledgeChunk(
                tenant_id="tenant_demo",
                knowledge_base_id="kb_demo",
                document_id="kdoc_0",
                bucket_id="kbucket_0",
                chunk_index=1,
                content="补充段落",
            )
        )
        db.commit()
        counts = KnowledgeService(db)._candidate_chunk_counts(
            "tenant_demo", ["kbucket_0", "kbucket_1"]
        )
        assert counts == {"kbucket_0": 2, "kbucket_1": 1}
        assert KnowledgeService(db)._candidate_chunk_counts("tenant_demo", []) == {}


def test_small_candidate_shortcut_applies_to_document_route(fake_redis, monkeypatch) -> None:
    """文档维同款短路：候选文档 chunk 总量小 → 跳过 LLM 文档路由全选。"""

    monkeypatch.setattr(knowledge_service, "SMALL_CANDIDATE_CHUNK_LIMIT", 12)

    def _forbidden(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("small-candidate shortcut must skip LLM document routing")

    monkeypatch.setattr(KnowledgeService, "_select_documents_with_llm", _forbidden)
    monkeypatch.setattr(KnowledgeService, "_select_buckets_with_llm", _forbidden)

    with _test_session() as db:
        _seed_ambiguous_fixture(db)
        response = _run_search(db, "员工手册")

    phases = _phases(response)
    assert "document_route_small_candidate_shortcut" in phases
    # 文档全选 → 桶候选同步变全 → 桶维短路级联触发，两级 LLM 全跳过
    assert "bucket_route_small_candidate_shortcut" in phases
    assert [bucket.id for bucket in response.selected_buckets] == ["kbucket_0", "kbucket_1"]


def test_single_candidate_shortcut_skips_document_llm(fake_redis, monkeypatch) -> None:
    """候选文档唯一但 chunk 超过小候选阈值 → 单候选短路仍跳过 LLM 文档路由。

    复现线上 51 chunk / 单文档卡在小候选阈值外的场景（08_plan_a_speedup 的
    「加一条 candidate_count <= 1 → 直接选中」）。
    """

    monkeypatch.setattr(knowledge_service, "SMALL_CANDIDATE_CHUNK_LIMIT", 2)

    def _forbidden(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("single-candidate shortcut must skip LLM document routing")

    monkeypatch.setattr(KnowledgeService, "_select_documents_with_llm", _forbidden)
    monkeypatch.setattr(
        KnowledgeService, "_select_buckets_with_llm", lambda *a, **k: ["kbucket_0"]
    )

    with _test_session() as db:
        _seed_ambiguous_fixture(db, document_count=1)
        for index in (1, 2):
            db.add(
                KnowledgeChunk(
                    tenant_id="tenant_demo",
                    knowledge_base_id="kb_demo",
                    document_id="kdoc_0",
                    bucket_id="kbucket_0",
                    chunk_index=index,
                    content="补充段落",
                )
            )
        db.commit()
        response = _run_search(db, "员工手册")

    phases = _phases(response)
    assert "document_route_single_candidate_shortcut" in phases
    assert "document_route_small_candidate_shortcut" not in phases
    assert [document["id"] for document in response.selected_documents] == ["kdoc_0"]
