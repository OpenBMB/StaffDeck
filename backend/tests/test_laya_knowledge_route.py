"""P2'：Laya 知识路由（选文档 / 选桶）测试。

覆盖：
- 问题构造：逐候选 ``noul``、key→id 映射、分批
- ``score_candidates``：解析 ``noul``、上限回退、错误 / 熔断 / 缺答案
- 服务层 ``_select_via_laya``：阈值、排序（平局保留词法序）、影子、关闭零副作用
- ``_select_documents`` / ``_select_buckets``：Laya 命中不调 LLM；判不了回退 LLM
- 引擎侧构造：默认关闭，开启时带正确配置
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Callable

import pytest

from app.core.knowledge_flow.laya_client import LayaError
from app.core.knowledge_flow.routing import (
    LayaRouter,
    build_relevance_background,
    build_relevance_questions,
)
from app.knowledge import service as service_module


def _predict_noul(probabilities: dict[str, float]) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """按问题 key 返回 ``noul`` 概率；缺省 0.5。"""

    def predict(payload: dict[str, Any]) -> dict[str, Any]:
        answers: dict[str, Any] = {}
        for key in payload["questions"]:
            value = probabilities.get(key, 0.5)
            answers[key] = {
                "type": "noul",
                "noul": value,
                "confidence": max(value, 1 - value),
            }
        return {"answers": answers, "elapsed_ms": 12.5}

    return predict


def _router(**kwargs: Any) -> LayaRouter:
    options: dict[str, Any] = {
        "enabled": True,
        "shadow": False,
        "min_confidence": 0.7,
        "predict": _predict_noul({}),
    }
    options.update(kwargs)
    return LayaRouter(**options)


# ---------------------------------------------------------------------------
# 问题构造
# ---------------------------------------------------------------------------


def test_build_relevance_background_carries_query() -> None:
    assert build_relevance_background("年假怎么请") == "用户问题：年假怎么请"
    assert build_relevance_background("   ") == ""


def test_build_relevance_questions_are_noul_without_criteria() -> None:
    questions, key_to_id = build_relevance_questions(
        [("doc-1", "标题：报销制度｜摘要：差旅报销"), ("doc-2", "标题：年假制度")]
    )
    assert set(questions) == {"cand0", "cand1"}
    assert key_to_id == {"cand0": "doc-1", "cand1": "doc-2"}
    for question in questions.values():
        assert question["type"] == "noul"
        assert "criteria" not in question
        assert "是否与用户问题相关" in question["instructions"]
    assert "报销制度" in questions["cand0"]["instructions"]


# ---------------------------------------------------------------------------
# score_candidates
# ---------------------------------------------------------------------------


def test_score_candidates_returns_probabilities_in_input_order() -> None:
    router = _router(predict=_predict_noul({"cand0": 0.2, "cand1": 0.9}))
    outcome = router.score_candidates(
        query="年假", candidates=[("d1", "a"), ("d2", "b")]
    )
    assert [item.key for item in outcome.scores] == ["d1", "d2"]
    assert [item.probability for item in outcome.scores] == [0.2, 0.9]
    assert outcome.candidate_count == 2 and outcome.calls == 1


def test_score_candidates_batches_when_more_than_per_call() -> None:
    calls: list[int] = []

    def predict(payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(len(payload["questions"]))
        return {
            "answers": {
                key: {"type": "noul", "noul": 0.8} for key in payload["questions"]
            },
            "elapsed_ms": 5.0,
        }

    router = _router(predict=predict, questions_per_call=2)
    outcome = router.score_candidates(
        query="q", candidates=[("d1", "a"), ("d2", "b"), ("d3", "c")]
    )
    assert calls == [2, 1]
    assert outcome.calls == 2
    assert len(outcome.scores) == 3


def test_score_candidates_oversize_falls_back_without_calling_upstream() -> None:
    def _boom(_payload: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("候选超限时不应调用 Laya")

    router = _router(predict=_boom, max_candidates=2)
    outcome = router.score_candidates(
        query="q", candidates=[("d1", "a"), ("d2", "b"), ("d3", "c")]
    )
    assert outcome.fallback_reason == "oversize"
    assert outcome.scores == []


def test_score_candidates_disabled_and_no_candidates() -> None:
    disabled = LayaRouter(enabled=False)
    assert disabled.score_candidates(query="q", candidates=[("d1", "a")]).fallback_reason == "disabled"
    assert _router().score_candidates(query="q", candidates=[]).fallback_reason == "no_candidates"


def test_score_candidates_error_and_circuit_breaker() -> None:
    def _boom(_payload: dict[str, Any]) -> dict[str, Any]:
        raise LayaError("connect refused", kind="network")

    router = _router(predict=_boom, max_failures=2)
    assert router.score_candidates(query="q", candidates=[("d1", "a")]).fallback_reason == "error"
    assert router.score_candidates(query="q", candidates=[("d1", "a")]).fallback_reason == "error"
    assert router.open is True
    opened = router.score_candidates(query="q", candidates=[("d1", "a")])
    assert opened.fallback_reason == "circuit_open"


def test_score_candidates_missing_answers() -> None:
    router = _router(predict=lambda _payload: {"elapsed_ms": 1.0})
    outcome = router.score_candidates(query="q", candidates=[("d1", "a")])
    assert outcome.fallback_reason == "missing_answers"


# ---------------------------------------------------------------------------
# 服务层 _select_via_laya
# ---------------------------------------------------------------------------


def _service(router: Any) -> service_module.KnowledgeService:
    return service_module.KnowledgeService(None, laya_router=router)


def test_select_via_laya_returns_ranked_topk() -> None:
    router = _router(
        predict=_predict_noul({"cand0": 0.9, "cand1": 0.4, "cand2": 0.95})
    )
    trace: list[dict[str, Any]] = []
    ids = _service(router)._select_via_laya(
        "q", [("d1", "a"), ("d2", "b"), ("d3", "c")], 2, "document", trace
    )
    assert ids == ["d3", "d1"]
    assert trace[-1]["phase"] == "laya_document_route"
    assert trace[-1]["selected_count"] == 2


def test_select_via_laya_ties_keep_lexical_order() -> None:
    router = _router(predict=_predict_noul({"cand0": 0.9, "cand1": 0.9}))
    trace: list[dict[str, Any]] = []
    ids = _service(router)._select_via_laya(
        "q", [("d1", "a"), ("d2", "b")], 2, "bucket", trace
    )
    assert ids == ["d1", "d2"]


def test_select_via_laya_low_confidence_falls_back() -> None:
    router = _router(predict=_predict_noul({"cand0": 0.2, "cand1": 0.3}))
    trace: list[dict[str, Any]] = []
    ids = _service(router)._select_via_laya(
        "q", [("d1", "a"), ("d2", "b")], 2, "document", trace
    )
    assert ids is None
    assert trace[-1]["phase"] == "laya_document_route_skipped"
    assert trace[-1]["reason"] == "low_confidence"


def test_select_via_laya_shadow_does_not_adopt() -> None:
    router = _router(shadow=True, predict=_predict_noul({"cand0": 0.95}))
    trace: list[dict[str, Any]] = []
    ids = _service(router)._select_via_laya("q", [("d1", "a")], 1, "document", trace)
    assert ids is None
    assert trace[-1]["phase"] == "laya_document_route_shadow"


@pytest.mark.parametrize("router", [None, LayaRouter(enabled=False)])
def test_select_via_laya_disabled_is_silent(router: Any) -> None:
    trace: list[dict[str, Any]] = []
    assert _service(router)._select_via_laya("q", [("d1", "a")], 1, "document", trace) is None
    assert trace == []


# ---------------------------------------------------------------------------
# 服务层 _select_documents / _select_buckets：Laya 优先、判不了回退 LLM
# ---------------------------------------------------------------------------


def _patch_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        service_module, "_document_route_label", lambda row: f"doc:{row.id}"
    )
    monkeypatch.setattr(
        service_module, "_bucket_route_label", lambda row: f"bucket:{row.id}"
    )


def test_select_documents_uses_laya_without_calling_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_labels(monkeypatch)
    called: list[int] = []

    def _llm(*_args: Any, **_kwargs: Any) -> list[str]:
        called.append(1)
        return ["should-not-happen"]

    monkeypatch.setattr(service_module.KnowledgeService, "_select_documents_with_llm", _llm)
    router = _router(predict=_predict_noul({"cand0": 0.2, "cand1": 0.95}))
    trace: list[dict[str, Any]] = []
    documents = [SimpleNamespace(id="d1"), SimpleNamespace(id="d2")]
    ids = _service(router)._select_documents("q", documents, 5, object(), trace)
    assert ids == ["d2"]
    assert called == []


def test_select_documents_falls_back_to_llm_when_laya_unsure(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_labels(monkeypatch)
    called: list[int] = []

    def _llm(*_args: Any, **_kwargs: Any) -> list[str]:
        called.append(1)
        return ["llm-pick"]

    monkeypatch.setattr(service_module.KnowledgeService, "_select_documents_with_llm", _llm)
    router = _router(predict=_predict_noul({"cand0": 0.1, "cand1": 0.2}))
    trace: list[dict[str, Any]] = []
    documents = [SimpleNamespace(id="d1"), SimpleNamespace(id="d2")]
    ids = _service(router)._select_documents("q", documents, 5, object(), trace)
    assert ids == ["llm-pick"]
    assert called == [1]
    assert trace[0]["phase"] == "laya_document_route_skipped"


def test_select_buckets_uses_laya_and_records_dimension(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_labels(monkeypatch)

    def _llm(*_args: Any, **_kwargs: Any) -> list[str]:
        raise AssertionError("Laya 命中时不应调 LLM")

    monkeypatch.setattr(service_module.KnowledgeService, "_select_buckets_with_llm", _llm)
    router = _router(predict=_predict_noul({"cand0": 0.9}))
    trace: list[dict[str, Any]] = []
    buckets = [SimpleNamespace(id="b1")]
    ids = _service(router)._select_buckets("q", buckets, 4, object(), trace, "answer")
    assert ids == ["b1"]
    assert trace[-1]["phase"] == "laya_bucket_route"


def test_select_documents_without_router_goes_straight_to_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_labels(monkeypatch)
    called: list[int] = []

    def _llm(*_args: Any, **_kwargs: Any) -> list[str]:
        called.append(1)
        return ["llm-pick"]

    monkeypatch.setattr(service_module.KnowledgeService, "_select_documents_with_llm", _llm)
    trace: list[dict[str, Any]] = []
    documents = [SimpleNamespace(id="d1")]
    ids = service_module.KnowledgeService(None)._select_documents(
        "q", documents, 5, object(), trace
    )
    assert ids == ["llm-pick"]
    assert called == [1]
    assert trace == []


# ---------------------------------------------------------------------------
# 引擎侧构造
# ---------------------------------------------------------------------------


def test_build_knowledge_laya_router_respects_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import harness_v2_engine as engine_module

    enabled = SimpleNamespace(
        laya_knowledge_route_enabled=True,
        laya_knowledge_shadow_mode=True,
        laya_min_confidence=0.7,
        laya_knowledge_timeout_seconds=6.0,
        laya_sop_circuit_breaker_failures=3,
        laya_knowledge_max_candidates=30,
        laya_knowledge_questions_per_call=30,
    )
    monkeypatch.setattr(engine_module, "get_settings", lambda: enabled)
    router = engine_module._build_knowledge_laya_router()
    assert router is not None
    assert router.enabled is True and router.shadow is True
    assert router.max_candidates == 30

    monkeypatch.setattr(
        engine_module,
        "get_settings",
        lambda: SimpleNamespace(laya_knowledge_route_enabled=False),
    )
    assert engine_module._build_knowledge_laya_router() is None