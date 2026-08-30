from types import SimpleNamespace
from typing import ClassVar

import pytest

from app.capabilities.contracts import CapabilityContext, KnowledgeSearchQuery
from app.capabilities.errors import CapabilityProviderError
from app.capabilities.local_knowledge import LocalKnowledgeRuntime
from app.knowledge.errors import KnowledgeError
from app.knowledge.schema import KnowledgeChunkRead, KnowledgeSearchResponse


class FakeKnowledgeService:
    calls: ClassVar[list[object]] = []

    def __init__(self, db: object) -> None:
        self.db = db

    def search(
        self,
        request: object,
        model_config: object,
        **trusted_context: object,
    ) -> KnowledgeSearchResponse:
        """记录 Provider 交给服务层的请求和服务端可信授权参数。"""
        self.calls.append((request, model_config, trusted_context))
        return KnowledgeSearchResponse(
            chunks=[
                KnowledgeChunkRead(
                    id="chunk-1",
                    tenant_id="tenant-1",
                    knowledge_base_id="kb-1",
                    document_id="doc-1",
                    bucket_id="bucket-1",
                    chunk_index=0,
                    content="报销上限是 1000 元。",
                    source_ref="doc-1#chunk-1",
                    metadata={"source": "policy"},
                    created_at="2026-07-27T00:00:00Z",
                    updated_at="2026-07-27T00:00:00Z",
                )
            ],
            trace=[{"phase": "local"}],
        )


def test_local_knowledge_scope_listing_is_tenant_and_agent_scoped(monkeypatch) -> None:
    class FakeAccessService:
        """为列表测试返回一个与当前员工绑定的授权知识投影。"""

        def __init__(self, db: object) -> None:
            """保留数据库占位引用，不产生副作用。"""
            self.db = db

        def resolve_projections(self, **context: object) -> list[SimpleNamespace]:
            """返回唯一专用投影，并把员工标识留给版本元数据断言。"""
            return [
                SimpleNamespace(
                    knowledge_base_id="kb-1",
                    knowledge_base_version_id="kbver-1",
                )
            ]

    class FakeDB:
        """只实现列表适配器读取版本所需的 get 接口。"""

        def get(self, model: object, version_id: str) -> SimpleNamespace:
            """返回固定版本快照；未知标识在本测试中不应出现。"""
            assert version_id == "kbver-1"
            return SimpleNamespace(
                name="Policies",
                version="2.0.0",
                metadata_json={"owner": "agent-1"},
            )

    monkeypatch.setattr(
        "app.capabilities.local_knowledge.KnowledgeAccessService",
        FakeAccessService,
    )
    runtime = LocalKnowledgeRuntime(FakeKnowledgeService, db=FakeDB(), model_config=None)
    scopes = runtime.list_scopes(
        CapabilityContext(
            request_id="req-1",
            tenant_id="tenant-1",
            agent_id="agent-1",
            user_id="user-1",
            session_id="session-1",
            turn_id="turn-1",
            channel="web",
        )
    )
    assert scopes[0].scope_id == "kb-1"
    assert scopes[0].metadata["owner"] == "agent-1"


def test_local_knowledge_adapter_preserves_service_owned_result(monkeypatch) -> None:
    """适配器保留服务层结果，同时附加服务端解析的知识库版本授权。"""
    class FakeAccessService:
        """为私聊适配器测试提供唯一可读版本。"""

        def __init__(self, db: object) -> None:
            """保存数据库占位引用，不产生副作用。"""
            self.db = db

        def resolve_projections(self, **context: object) -> list[SimpleNamespace]:
            """返回当前私聊上下文唯一可读的专用知识版本。"""
            return [
                SimpleNamespace(
                    knowledge_base_id="kb-1",
                    knowledge_base_version_id="kbver-1",
                )
            ]

    monkeypatch.setattr(
        "app.capabilities.local_knowledge.KnowledgeAccessService",
        FakeAccessService,
    )
    FakeKnowledgeService.calls = []
    runtime = LocalKnowledgeRuntime(FakeKnowledgeService, db=object(), model_config="model")
    result = runtime.search(
        CapabilityContext(
            request_id="req-1",
            tenant_id="tenant-1",
            agent_id="agent-1",
            user_id="user-1",
            session_id="session-1",
            turn_id="turn-1",
            channel="web",
        ),
        KnowledgeSearchQuery(query="报销上限"),
    )

    assert result.query_id.startswith("kquery_")
    assert result.items[0].source_ref == "doc-1#chunk-1"
    assert result.extensions["local_knowledge"]["request_id"] == "req-1"
    request, model_config, trusted_context = FakeKnowledgeService.calls[0]
    assert request.tenant_id == "tenant-1"
    assert request.query == "报销上限"
    assert model_config == "model"
    assert trusted_context == {
        "trusted_team_id": None,
        "authorized_knowledge_versions": {"kb-1": "kbver-1"},
    }
    try:
        runtime.resolve_citation(
            CapabilityContext(
                request_id="req-2",
                tenant_id="tenant-1",
                agent_id="agent-1",
                user_id="user-1",
                session_id="session-1",
                turn_id="turn-1",
                channel="web",
            ),
            "chunk-1",
        )
    except CapabilityProviderError as exc:
        assert exc.info.code == "KNOWLEDGE_CITATION_NOT_DURABLE"
        payload = exc.info.to_payload()
        assert payload["extensions"] == {}
        assert "provider_citation_ref" not in str(payload)
    else:  # pragma: no cover - the adapter must never expose an in-memory citation
        raise AssertionError("non-durable citation unexpectedly resolved")


def test_local_knowledge_adapter_rejects_unknown_query_type() -> None:
    runtime = LocalKnowledgeRuntime(FakeKnowledgeService, db=object(), model_config=None)
    try:
        runtime.search(
            CapabilityContext(
                request_id="req-3",
                tenant_id="tenant-1",
                agent_id="agent-1",
                user_id="user-1",
                session_id="session-1",
                turn_id="turn-1",
                channel="web",
            ),
            KnowledgeSearchQuery(query="x", query_type="future_mode"),
        )
    except CapabilityProviderError as exc:
        assert exc.info.code == "KNOWLEDGE_UNSUPPORTED_QUERY_TYPE"
    else:  # pragma: no cover - unknown operation semantics must not be downgraded
        raise AssertionError("unknown query type was silently downgraded")


def test_team_provider_intersects_requested_bases_with_trusted_access(monkeypatch) -> None:
    """团队检索只把请求 ID 与实时授权投影交集后的冻结版本交给服务层。"""
    class FakeAccessService:
        """为 Provider 测试提供一个只授权项目 A 知识库的可信解析器。"""

        def __init__(self, db: object) -> None:
            """保存测试数据库占位对象，不产生副作用。"""
            self.db = db

        def resolve_projections(self, **context: object) -> list[SimpleNamespace]:
            """断言团队上下文来自 CapabilityContext，并返回唯一授权投影。"""
            assert context["team_id"] == "team-a"
            return [
                SimpleNamespace(
                    knowledge_base_id="kb-a",
                    knowledge_base_version_id="kbver-a-1",
                    mode="shared",
                    permission="reader",
                    team_id="team-a",
                    is_default_write=False,
                )
            ]

    monkeypatch.setattr(
        "app.capabilities.local_knowledge.KnowledgeAccessService",
        FakeAccessService,
    )
    FakeKnowledgeService.calls = []
    runtime = LocalKnowledgeRuntime(FakeKnowledgeService, db=object(), model_config="model")

    runtime.search(
        CapabilityContext(
            request_id="req-team",
            tenant_id="tenant-1",
            agent_id="agent-1",
            user_id="user-1",
            session_id="session-team",
            turn_id="turn-team",
            channel="web",
            team_id="team-a",
        ),
        KnowledgeSearchQuery(
            query="选题规范",
            knowledge_base_ids=("kb-a", "kb-b"),
        ),
    )

    request, _, trusted_context = FakeKnowledgeService.calls[0]
    assert request.knowledge_base_ids == ["kb-a"]
    assert request.knowledge_base_version_ids == ["kbver-a-1"]
    assert trusted_context == {
        "trusted_team_id": "team-a",
        "authorized_knowledge_versions": {"kb-a": "kbver-a-1"},
    }


def test_shared_knowledge_actions_reject_private_conversation_context() -> None:
    """共享维护动作没有服务端团队上下文时必须在访问数据库前失败。"""
    from app.capabilities import local_knowledge

    runtime_model = getattr(local_knowledge, "SharedKnowledgeAgentRuntime", None)
    assert runtime_model is not None, "missing SharedKnowledgeAgentRuntime"
    runtime = runtime_model(db=object())

    with pytest.raises(KnowledgeError) as denied:
        runtime.execute(
            CapabilityContext(
                request_id="req-private-action",
                tenant_id="tenant-1",
                agent_id="agent-1",
                user_id="user-1",
                session_id="session-private",
                turn_id="turn-private",
                channel="web",
            ),
            "knowledge_create_draft",
            {
                "change_reason": "私聊不得写共享库",
                "idempotency_key": "private-1",
            },
        )

    assert getattr(denied.value, "code", None) == "KNOWLEDGE_CONTEXT_MISMATCH"
