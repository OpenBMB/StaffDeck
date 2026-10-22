"""边条件异步编译的调度层测试（不需要 Redis / rq / 真库）。

重点验证**降级路径**：写路径绝不能因为后台队列不可用而失败或变慢。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.config import get_settings
from app.skills.edge_condition_jobs import (
    JOB_FUNC_PATH,
    JOB_NAME,
    has_conditional_edges,
    schedule_edge_condition_compile,
)


def _graph(condition: str) -> dict[str, Any]:
    return {
        "nodes": [{"node_id": "a", "expected_user_info": ["x"]}, {"node_id": "b"}],
        "edges": [{"source_node_id": "a", "next_node_id": "b", "condition": condition}],
    }


# ---------------------------------------------------------------------------
# has_conditional_edges 短路
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("condition", ["", "always", "default", "else", "  "])
def test_linear_graphs_need_no_compilation(condition: str) -> None:
    assert has_conditional_edges(_graph(condition)) is False


@pytest.mark.parametrize("condition", ["用户确认后进入", "上一步工具调用成功后进入"])
def test_conditional_graphs_are_detected(condition: str) -> None:
    assert has_conditional_edges(_graph(condition)) is True


def test_has_conditional_edges_tolerates_junk() -> None:
    assert has_conditional_edges(None) is False
    assert has_conditional_edges("not a graph") is False
    assert has_conditional_edges({"edges": "not a list"}) is False
    # 缺端点的悬空边不算
    assert has_conditional_edges({"edges": [{"condition": "有条件的"}]}) is False


# ---------------------------------------------------------------------------
# 调度：rq 优先，失败降级进程内异步队列
# ---------------------------------------------------------------------------


def test_schedule_prefers_rq(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []

    def _fake_enqueue(queue_name: str, func_path: str, *args: Any, **kwargs: Any) -> str:
        calls.append((queue_name, func_path, *args))
        return "rq-job-1"

    monkeypatch.setattr("app.scheduled_tasks.rq_dispatch.enqueue_job", _fake_enqueue)
    job_id = schedule_edge_condition_compile("tenant_a", "sop_1", agent_id="agent_1")

    assert job_id == "rq-job-1"
    assert calls == [(get_settings().skill_compile_queue, JOB_FUNC_PATH, "tenant_a", "sop_1", "agent_1")]


def test_schedule_falls_back_to_inprocess_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    """rq 不可用（返回 None）时必须落到进程内异步队列，且调用的是同一个执行函数。"""

    monkeypatch.setattr(
        "app.scheduled_tasks.rq_dispatch.enqueue_job",
        lambda *args, **kwargs: None,
    )
    captured: dict[str, Any] = {}

    class _Job:
        id = "local-job-1"

    def _fake_async_enqueue(name: str, func: Any, *args: Any, **kwargs: Any) -> Any:
        captured["name"] = name
        captured["func"] = func
        captured["args"] = args
        return _Job()

    monkeypatch.setattr("app.async_jobs.enqueue_async_job", _fake_async_enqueue)
    job_id = schedule_edge_condition_compile("tenant_a", "sop_1")

    assert job_id == "local-job-1"
    assert captured["name"] == JOB_NAME
    assert captured["args"] == ("tenant_a", "sop_1", None)
    # 两条通道必须调用同一个执行函数，否则语义会分叉
    from app.skills.edge_condition_jobs import run_edge_condition_compile

    assert captured["func"] is run_edge_condition_compile


def test_schedule_returns_none_when_everything_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.scheduled_tasks.rq_dispatch.enqueue_job",
        lambda *args, **kwargs: None,
    )

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("queue closed")

    monkeypatch.setattr("app.async_jobs.enqueue_async_job", _boom)
    # 后台编译不是写路径的必要条件：必须返回 None 而不是抛异常
    assert schedule_edge_condition_compile("tenant_a", "sop_1") is None


def test_schedule_survives_rq_import_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("rq exploded")

    monkeypatch.setattr("app.scheduled_tasks.rq_dispatch.enqueue_job", _boom)

    class _Job:
        id = "local-job-2"

    monkeypatch.setattr(
        "app.async_jobs.enqueue_async_job",
        lambda *args, **kwargs: _Job(),
    )
    assert schedule_edge_condition_compile("tenant_a", "sop_1") == "local-job-2"


# ---------------------------------------------------------------------------
# rq 侧：降级与队列清单
# ---------------------------------------------------------------------------


def test_rq_enqueue_job_returns_none_without_redis() -> None:
    """测试环境 REDIS_HOST 为空（见 conftest）→ 入队必须安全地返回 None。"""

    from app.scheduled_tasks import rq_dispatch

    rq_dispatch.reset_caches()
    try:
        assert rq_dispatch.enqueue_job("any_queue", JOB_FUNC_PATH, "t", "s") is None
    finally:
        rq_dispatch.reset_caches()


def test_rq_worker_consumes_all_background_queues() -> None:
    from app.scheduled_tasks import rq_dispatch

    names = rq_dispatch.consume_queue_names()
    settings = get_settings()
    assert settings.scheduled_task_queue in names
    assert settings.skill_compile_queue in names
    assert settings.knowledge_ingest_queue in names
    assert len(names) == len(set(names))


def test_job_func_path_is_importable() -> None:
    """rq 用点分字符串路径跨进程取函数——必须能被 **rq 自己的解析器** 解析出来。

    这里刻意不自己 ``split(".")`` 再 ``getattr``：那样写只能证明「路径长得像
    点分」，证明不了 rq 认它。历史上这个常量写成过 ``module:qualname``，手工
    拆分解析不了的用例照样绿，直到 worker 侧抛
    ``ValueError: Invalid attribute name`` 才暴露。
    """

    from rq.utils import import_attribute

    target = import_attribute(JOB_FUNC_PATH)
    assert callable(target)
    assert target.__name__ == "run_edge_condition_compile"


def test_resolve_job_func_normalizes_colon_path() -> None:
    """兼容写法 ``module:func`` 要被归一化成点分，而不是排一个注定失败的任务。"""

    from app.scheduled_tasks.rq_dispatch import _resolve_job_func

    resolved = _resolve_job_func("app.skills.edge_condition_jobs:run_edge_condition_compile")
    assert resolved is not None
    path, func = resolved
    assert path == "app.skills.edge_condition_jobs.run_edge_condition_compile"
    assert callable(func)


def test_resolve_job_func_rejects_bad_paths() -> None:
    from app.scheduled_tasks.rq_dispatch import _resolve_job_func

    assert _resolve_job_func("app.skills.edge_condition_jobs.no_such_func") is None
    assert _resolve_job_func("app.no_such_module.func") is None
    assert _resolve_job_func("no_dot_at_all") is None
    # 目录不是函数（不可调用）也要挡住
    assert _resolve_job_func("app.skills.edge_condition_jobs") is None


def test_enqueue_job_fails_fast_on_unresolvable_func(monkeypatch) -> None:
    """路径写错时必须在**生产端**就返回 None，而不是排进队列变哑弹。

    用「碰 Redis 就炸」替身证明短路点在连接之前：若代码没做前置解析，就会走到
    ``_redis_connection`` 而触发断言。
    """

    from app.scheduled_tasks import rq_dispatch

    def _boom(*_args, **_kwargs):
        raise AssertionError("函数路径不可解析时不应触碰 Redis")

    monkeypatch.setattr(rq_dispatch, "_redis_connection", _boom)
    rq_dispatch.reset_caches()
    try:
        bad = "app.skills.edge_condition_jobs:definitely_missing"
        assert rq_dispatch.enqueue_job("any_queue", bad) is None
    finally:
        rq_dispatch.reset_caches()


# ---------------------------------------------------------------------------
# 引擎侧加载器：加速项失败不能影响对话
# ---------------------------------------------------------------------------


def test_engine_loader_skips_conversation_frames() -> None:
    from app.core.engine.turn import _load_edge_condition_specs

    assert _load_edge_condition_specs(None, "tenant_a", object(), "conversation") == {}


def test_engine_loader_returns_empty_without_skill() -> None:
    from app.core.engine.turn import _load_edge_condition_specs

    assert _load_edge_condition_specs(None, "tenant_a", None, "sop") == {}


def test_engine_loader_swallows_db_errors() -> None:
    class _Skill:
        skill_id = "sop_1"

    class _BrokenDb:
        def exec(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("db down")

    from app.core.engine.turn import _load_edge_condition_specs

    assert _load_edge_condition_specs(_BrokenDb(), "tenant_a", _Skill(), "sop") == {}
