"""本地知识 Provider：只读检索，以及可信团队上下文中的显式共享维护动作。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.capabilities.contracts import (
    CapabilityContext,
    CitationDetail,
    KnowledgeHit,
    KnowledgeRuntime,
    KnowledgeScope,
    KnowledgeSearchQuery,
    KnowledgeSearchResult,
)
from app.capabilities.errors import CapabilityErrorInfo, CapabilityProviderError
from app.db.models import ChatSession, KnowledgeBaseVersion, new_id
from app.knowledge.access import KnowledgeAccessService
from app.knowledge.audit import KnowledgeAuditService, KnowledgeMutationReceipt
from app.knowledge.errors import (
    KNOWLEDGE_CONTEXT_MISMATCH,
    KNOWLEDGE_MODE_INVALID,
    KnowledgeError,
    knowledge_error,
)
from app.knowledge.schema import KnowledgeSearchRequest, KnowledgeSearchResponse
from app.knowledge.versioning import SharedKnowledgeVersionService


@dataclass(frozen=True)
class SharedKnowledgeAgentActionResult:
    """一次共享知识 Agent 动作的公开结果及内部重放/摄取调度标记。"""

    data: dict[str, Any]
    replayed: bool = False
    ingest_job_id: str | None = None


class SharedKnowledgeAgentRuntime:
    """在服务端可信团队上下文中实时验权并执行显式共享知识动作。"""

    def __init__(self, db: Any) -> None:
        """绑定当前事务会话；动作执行不接受模型提供的租户、员工或团队身份。"""
        self._db = db
        self._access = KnowledgeAccessService(db)
        self._audit = KnowledgeAuditService(db)
        self._versions = SharedKnowledgeVersionService(db)

    def execute(
        self,
        context: CapabilityContext,
        action: str,
        arguments: dict[str, Any],
        *,
        frozen_knowledge_versions: dict[str, str] | None = None,
    ) -> SharedKnowledgeAgentActionResult:
        """实时重验会话与授权后，分派一个已批准的共享知识动作。"""
        # 先验证服务端会话仍处于同一活动团队，私聊绝不降级执行共享动作。
        self._require_active_team_session(context)

        # 只分派固定动作词表，普通 Agent 输出不会经过此入口自动写入。
        handlers = {
            "knowledge_list_versions": self._list_versions,
            "knowledge_create_draft": self._create_draft,
            "knowledge_update_draft": self._update_draft,
            "knowledge_publish_draft": self._publish_draft,
            "knowledge_reject_draft": self._reject_draft,
            "knowledge_rollback": self._rollback,
        }
        handler = handlers.get(action)
        if handler is None:
            raise knowledge_error(
                KNOWLEDGE_MODE_INVALID,
                message="不支持的共享知识 Agent 动作。",
            )

        # 最后把清洗后的参数交给动作；可信身份始终来自 CapabilityContext。
        return handler(
            context,
            dict(arguments or {}),
            frozen_knowledge_versions=frozen_knowledge_versions or {},
        )

    def _require_active_team_session(self, context: CapabilityContext) -> ChatSession:
        """确认会话活动且租户、团队与 Core 注入的上下文完全一致。"""
        if context.team_id is None:
            raise knowledge_error(KNOWLEDGE_CONTEXT_MISMATCH)
        session = self._db.get(ChatSession, context.session_id)
        if (
            session is None
            or session.tenant_id != context.tenant_id
            or session.team_id != context.team_id
            or session.status != "active"
        ):
            raise knowledge_error(KNOWLEDGE_CONTEXT_MISMATCH)
        return session

    def _list_versions(
        self,
        context: CapabilityContext,
        arguments: dict[str, Any],
        *,
        frozen_knowledge_versions: dict[str, str],
    ) -> SharedKnowledgeAgentActionResult:
        """按实时 reader 权限列出正式历史，并按更高权限收窄草稿与驳回记录。"""
        del frozen_knowledge_versions
        requested_base_id = _optional_argument(arguments, "knowledge_base_id")
        requested_state = _optional_argument(arguments, "publication_state")
        projections = self._access.resolve_projections(
            tenant_id=context.tenant_id,
            agent_id=context.agent_id,
            team_id=context.team_id,
        )
        projections = [
            projection for projection in projections if projection.mode == "shared"
        ]
        if requested_base_id:
            projections = [
                projection
                for projection in projections
                if projection.knowledge_base_id == requested_base_id
            ]
            if not projections:
                self._access.require_shared_projection(
                    tenant_id=context.tenant_id,
                    agent_id=context.agent_id,
                    team_id=context.team_id,
                    knowledge_base_id=requested_base_id,
                )

        bases: list[dict[str, Any]] = []
        for projection in projections:
            versions = self._versions.list_versions(
                tenant_id=context.tenant_id,
                knowledge_base_id=projection.knowledge_base_id,
                publication_state=requested_state,
            )
            visible_versions = [
                _agent_version_summary(
                    version,
                    published_version_id=projection.knowledge_base_version_id,
                    permission=projection.permission,
                )
                for version in versions
                if _agent_can_view_version(
                    version,
                    context=context,
                    permission=projection.permission,
                )
            ]
            bases.append(
                {
                    "knowledge_base_id": projection.knowledge_base_id,
                    "published_version_id": projection.knowledge_base_version_id,
                    "permission": projection.permission,
                    "is_default_write": projection.is_default_write,
                    "versions": visible_versions,
                }
            )
        return SharedKnowledgeAgentActionResult(data={"knowledge_bases": bases})

    def _create_draft(
        self,
        context: CapabilityContext,
        arguments: dict[str, Any],
        *,
        frozen_knowledge_versions: dict[str, str],
    ) -> SharedKnowledgeAgentActionResult:
        """从显式目标或团队默认共享库创建带来源、可重放的草稿。"""
        target = self._access.resolve_write_target(
            tenant_id=context.tenant_id,
            agent_id=context.agent_id,
            team_id=_required_team_id(context),
            knowledge_base_id=_optional_argument(arguments, "knowledge_base_id"),
            minimum_permission="editor",
        )
        expected_version_id = _optional_argument(
            arguments,
            "expected_published_version_id",
        ) or frozen_knowledge_versions.get(target.knowledge_base_id)
        request_payload = _agent_request_payload(
            context,
            arguments,
            knowledge_base_id=target.knowledge_base_id,
            expected_published_version_id=expected_version_id,
        )
        replay = self._replay(
            context,
            knowledge_base_id=target.knowledge_base_id,
            action="draft_created",
            arguments=arguments,
            request_payload=request_payload,
        )
        if replay is not None:
            return replay

        source_task_id = _trusted_source_task_id(context, arguments)
        self._versions.create_draft(
            tenant_id=context.tenant_id,
            knowledge_base_id=target.knowledge_base_id,
            source_team_id=context.team_id,
            actor_type="agent",
            actor_id=context.agent_id,
            change_reason=_required_argument(arguments, "change_reason"),
            expected_published_version_id=expected_version_id,
            source_task_id=source_task_id,
            source_conversation_id=context.session_id,
            source_references=_source_references(arguments),
            idempotency_key=_required_argument(arguments, "idempotency_key"),
            request_payload=request_payload,
        )
        return self._persisted_result(
            context,
            knowledge_base_id=target.knowledge_base_id,
            action="draft_created",
            arguments=arguments,
            request_payload=request_payload,
        )

    def _update_draft(
        self,
        context: CapabilityContext,
        arguments: dict[str, Any],
        *,
        frozen_knowledge_versions: dict[str, str],
    ) -> SharedKnowledgeAgentActionResult:
        """实时校验草稿归属与 editor 权限后创建唯一摄取任务。"""
        del frozen_knowledge_versions
        draft = self._authorized_version(
            context,
            _required_argument(arguments, "draft_version_id"),
            minimum_permission="editor",
            require_team_draft=True,
        )
        request_payload = _agent_request_payload(
            context,
            arguments,
            knowledge_base_id=draft.knowledge_base_id,
        )
        replay = self._replay(
            context,
            knowledge_base_id=draft.knowledge_base_id,
            action="draft_updated",
            arguments=arguments,
            request_payload=request_payload,
        )
        if replay is not None:
            return replay

        job = self._versions.queue_draft_update(
            tenant_id=context.tenant_id,
            knowledge_base_id=draft.knowledge_base_id,
            draft_version_id=draft.id,
            actor_id=context.agent_id,
            source_team_id=_required_team_id(context),
            source_task_id=context.turn_id,
            source_conversation_id=context.session_id,
            title=_required_argument(arguments, "title"),
            filename=_required_argument(arguments, "filename"),
            content=_required_argument(arguments, "content"),
            source_references=_source_references(arguments),
            idempotency_key=_required_argument(arguments, "idempotency_key"),
            request_payload=request_payload,
        )
        result = self._persisted_result(
            context,
            knowledge_base_id=draft.knowledge_base_id,
            action="draft_updated",
            arguments=arguments,
            request_payload=request_payload,
        )
        return SharedKnowledgeAgentActionResult(
            data=result.data,
            replayed=False,
            ingest_job_id=job.id,
        )

    def _publish_draft(
        self,
        context: CapabilityContext,
        arguments: dict[str, Any],
        *,
        frozen_knowledge_versions: dict[str, str],
    ) -> SharedKnowledgeAgentActionResult:
        """实时校验 publisher 权限与草稿团队后，执行就绪检查和原子发布。"""
        del frozen_knowledge_versions
        draft = self._authorized_version(
            context,
            _required_argument(arguments, "draft_version_id"),
            minimum_permission="publisher",
            require_team_draft=True,
        )
        request_payload = _agent_request_payload(
            context,
            arguments,
            knowledge_base_id=draft.knowledge_base_id,
        )
        replay = self._replay(
            context,
            knowledge_base_id=draft.knowledge_base_id,
            action="version_published",
            arguments=arguments,
            request_payload=request_payload,
        )
        if replay is not None:
            return replay

        self._versions.publish_draft(
            tenant_id=context.tenant_id,
            knowledge_base_id=draft.knowledge_base_id,
            draft_version_id=draft.id,
            expected_published_version_id=_required_argument(
                arguments,
                "expected_published_version_id",
            ),
            actor_type="agent",
            actor_id=context.agent_id,
            source_team_id=context.team_id,
            change_reason=_required_argument(arguments, "change_reason"),
            idempotency_key=_required_argument(arguments, "idempotency_key"),
            request_payload=request_payload,
        )
        return self._persisted_result(
            context,
            knowledge_base_id=draft.knowledge_base_id,
            action="version_published",
            arguments=arguments,
            request_payload=request_payload,
        )

    def _reject_draft(
        self,
        context: CapabilityContext,
        arguments: dict[str, Any],
        *,
        frozen_knowledge_versions: dict[str, str],
    ) -> SharedKnowledgeAgentActionResult:
        """实时校验 publisher 权限后驳回本团队草稿，并保留快照。"""
        del frozen_knowledge_versions
        draft = self._authorized_version(
            context,
            _required_argument(arguments, "draft_version_id"),
            minimum_permission="publisher",
            require_team_draft=True,
        )
        request_payload = _agent_request_payload(
            context,
            arguments,
            knowledge_base_id=draft.knowledge_base_id,
        )
        replay = self._replay(
            context,
            knowledge_base_id=draft.knowledge_base_id,
            action="draft_rejected",
            arguments=arguments,
            request_payload=request_payload,
        )
        if replay is not None:
            return replay

        self._versions.reject_draft(
            tenant_id=context.tenant_id,
            knowledge_base_id=draft.knowledge_base_id,
            draft_version_id=draft.id,
            actor_type="agent",
            actor_id=context.agent_id,
            source_team_id=context.team_id,
            change_reason=_required_argument(arguments, "change_reason"),
            idempotency_key=_required_argument(arguments, "idempotency_key"),
            request_payload=request_payload,
        )
        return self._persisted_result(
            context,
            knowledge_base_id=draft.knowledge_base_id,
            action="draft_rejected",
            arguments=arguments,
            request_payload=request_payload,
        )

    def _rollback(
        self,
        context: CapabilityContext,
        arguments: dict[str, Any],
        *,
        frozen_knowledge_versions: dict[str, str],
    ) -> SharedKnowledgeAgentActionResult:
        """以显式或默认共享库的 publisher 权限恢复历史正式指针。"""
        del frozen_knowledge_versions
        target = self._access.resolve_write_target(
            tenant_id=context.tenant_id,
            agent_id=context.agent_id,
            team_id=_required_team_id(context),
            knowledge_base_id=_optional_argument(arguments, "knowledge_base_id"),
            minimum_permission="publisher",
        )
        request_payload = _agent_request_payload(
            context,
            arguments,
            knowledge_base_id=target.knowledge_base_id,
        )
        replay = self._replay(
            context,
            knowledge_base_id=target.knowledge_base_id,
            action="version_rolled_back",
            arguments=arguments,
            request_payload=request_payload,
        )
        if replay is not None:
            return replay

        self._versions.rollback(
            tenant_id=context.tenant_id,
            knowledge_base_id=target.knowledge_base_id,
            target_version_id=_required_argument(arguments, "target_version_id"),
            expected_published_version_id=_required_argument(
                arguments,
                "expected_published_version_id",
            ),
            actor_type="agent",
            actor_id=context.agent_id,
            source_team_id=context.team_id,
            change_reason=_required_argument(arguments, "change_reason"),
            idempotency_key=_required_argument(arguments, "idempotency_key"),
            request_payload=request_payload,
        )
        return self._persisted_result(
            context,
            knowledge_base_id=target.knowledge_base_id,
            action="version_rolled_back",
            arguments=arguments,
            request_payload=request_payload,
        )

    def _authorized_version(
        self,
        context: CapabilityContext,
        version_id: str,
        *,
        minimum_permission: str,
        require_team_draft: bool,
    ) -> KnowledgeBaseVersion:
        """隐藏式读取版本标识，并按其知识库实时重验团队授权。"""
        version = self._db.get(KnowledgeBaseVersion, version_id)
        if version is None or version.tenant_id != context.tenant_id:
            raise knowledge_error(KNOWLEDGE_CONTEXT_MISMATCH)
        self._access.require_shared_projection(
            tenant_id=context.tenant_id,
            agent_id=context.agent_id,
            team_id=context.team_id,
            knowledge_base_id=version.knowledge_base_id,
            minimum_permission=minimum_permission,
        )
        if require_team_draft and version.source_team_id != context.team_id:
            raise knowledge_error(KNOWLEDGE_CONTEXT_MISMATCH)
        return version

    def _replay(
        self,
        context: CapabilityContext,
        *,
        knowledge_base_id: str,
        action: str,
        arguments: dict[str, Any],
        request_payload: dict[str, Any],
    ) -> SharedKnowledgeAgentActionResult | None:
        """在实时授权通过后返回相同 Agent 变更的首个持久化结果。"""
        receipt = self._audit.replay_agent_mutation(
            tenant_id=context.tenant_id,
            knowledge_base_id=knowledge_base_id,
            actor_id=context.agent_id,
            action=action,
            idempotency_key=_required_argument(arguments, "idempotency_key"),
            request_payload=request_payload,
        )
        if receipt is None:
            return None
        return SharedKnowledgeAgentActionResult(data=receipt.result, replayed=True)

    def _persisted_result(
        self,
        context: CapabilityContext,
        *,
        knowledge_base_id: str,
        action: str,
        arguments: dict[str, Any],
        request_payload: dict[str, Any],
    ) -> SharedKnowledgeAgentActionResult:
        """刷新待写事件并读取其幂等收据，确保首次返回与重放完全一致。"""
        self._db.flush()
        receipt: KnowledgeMutationReceipt | None = self._audit.replay_agent_mutation(
            tenant_id=context.tenant_id,
            knowledge_base_id=knowledge_base_id,
            actor_id=context.agent_id,
            action=action,
            idempotency_key=_required_argument(arguments, "idempotency_key"),
            request_payload=request_payload,
        )
        if receipt is None:
            raise RuntimeError("共享知识 Agent 动作未生成持久化收据。")
        return SharedKnowledgeAgentActionResult(data=receipt.result)


def _required_team_id(context: CapabilityContext) -> str:
    """返回已校验的可信团队标识；私聊上下文直接失败。"""
    if context.team_id is None:
        raise knowledge_error(KNOWLEDGE_CONTEXT_MISMATCH)
    return context.team_id


def _required_argument(arguments: dict[str, Any], name: str) -> str:
    """返回去除空白后的必填字符串参数。"""
    value = str(arguments.get(name) or "").strip()
    if not value:
        raise knowledge_error(
            KNOWLEDGE_MODE_INVALID,
            message=f"{name} 不能为空。",
        )
    return value


def _optional_argument(arguments: dict[str, Any], name: str) -> str | None:
    """规范化可选字符串参数，空白值按未提供处理。"""
    value = str(arguments.get(name) or "").strip()
    return value or None


def _source_references(arguments: dict[str, Any]) -> list[dict[str, Any]]:
    """校验来源列表只包含结构化对象，避免把任意对象写入审计 JSON。"""
    value = arguments.get("source_references")
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise knowledge_error(
            KNOWLEDGE_MODE_INVALID,
            message="source_references 必须是对象列表。",
        )
    return [dict(item) for item in value]


def _trusted_source_task_id(
    context: CapabilityContext,
    arguments: dict[str, Any],
) -> str:
    """只允许当前 TaskFrame 作为来源任务，拒绝模型伪造其他团队任务标识。"""
    supplied = _optional_argument(arguments, "source_task_id")
    if supplied is not None and supplied != context.turn_id:
        raise knowledge_error(KNOWLEDGE_CONTEXT_MISMATCH)
    return context.turn_id


def _agent_request_payload(
    context: CapabilityContext,
    arguments: dict[str, Any],
    **resolved: Any,
) -> dict[str, Any]:
    """把可信团队与解析目标并入幂等摘要，防止跨团队重放同一键。"""
    return {
        "team_id": context.team_id,
        "source_task_id": context.turn_id,
        "source_conversation_id": context.session_id,
        "arguments": dict(arguments),
        "resolved": resolved,
    }


def _agent_can_view_version(
    version: KnowledgeBaseVersion,
    *,
    context: CapabilityContext,
    permission: str,
) -> bool:
    """按团队来源和权限决定 Agent 能否看到非正式版本详情。"""
    if version.publication_state == "released":
        return True
    if version.source_team_id != context.team_id:
        return False
    if version.publication_state == "draft":
        return KnowledgeAccessService.permission_allows(permission, "editor")
    return KnowledgeAccessService.permission_allows(
        permission,
        "publisher",
    ) or version.created_by_agent_id == context.agent_id


def _agent_version_summary(
    version: KnowledgeBaseVersion,
    *,
    published_version_id: str,
    permission: str,
) -> dict[str, Any]:
    """输出不含资产正文的版本摘要，并声明当前授权允许的后续动作。"""
    allowed_actions: list[str] = []
    if version.publication_state == "draft" and KnowledgeAccessService.permission_allows(
        permission,
        "editor",
    ):
        allowed_actions.append("knowledge_update_draft")
    if version.publication_state == "draft" and KnowledgeAccessService.permission_allows(
        permission,
        "publisher",
    ):
        allowed_actions.extend(["knowledge_publish_draft", "knowledge_reject_draft"])
    if (
        version.publication_state == "released"
        and version.id != published_version_id
        and KnowledgeAccessService.permission_allows(permission, "publisher")
    ):
        allowed_actions.append("knowledge_rollback")
    return {
        "id": version.id,
        "version": version.version,
        "publication_state": version.publication_state,
        "parent_version_id": version.parent_version_id,
        "source_team_id": version.source_team_id,
        "created_by_agent_id": version.created_by_agent_id,
        "change_reason": version.change_reason,
        "published_at": version.published_at.isoformat() if version.published_at else None,
        "is_published_head": version.id == published_version_id,
        "allowed_actions": allowed_actions,
    }


class LocalKnowledgeRuntime(KnowledgeRuntime):
    """Adapter for the existing local service; it has no AgentLoop dependency."""

    provider_id = "local_knowledge"

    def __init__(
        self,
        service_factory: Callable[[Any], Any],
        db: Any,
        model_config: Any | None = None,
    ) -> None:
        self._service_factory = service_factory
        self._db = db
        self._model_config = model_config

    def list_scopes(self, context: CapabilityContext) -> tuple[KnowledgeScope, ...]:
        """列出当前私聊或团队上下文实时可读的知识库及其正式版本。"""
        scopes: list[KnowledgeScope] = []
        for knowledge_base_id, version_id in self._authorized_versions(context).items():
            version = self._db.get(KnowledgeBaseVersion, version_id)
            if version is None:
                continue
            scopes.append(
                KnowledgeScope(
                    scope_id=knowledge_base_id,
                    name=version.name,
                    version=version.version,
                    metadata=dict(version.metadata_json or {}),
                )
            )
        return tuple(scopes)

    def search(
        self, context: CapabilityContext, request: KnowledgeSearchQuery
    ) -> KnowledgeSearchResult:
        """将模型请求与可信授权取交集后，把确定的知识库和版本交给本地服务。"""
        allowed_query_types = {"answer", "policy_check", "tool_discovery", "skill_discovery"}
        if request.query_type not in allowed_query_types:
            raise CapabilityProviderError(
                CapabilityErrorInfo(
                    code="KNOWLEDGE_UNSUPPORTED_QUERY_TYPE",
                    message=f"unsupported Knowledge query type: {request.query_type}",
                    retryable=False,
                    request_id=context.request_id,
                    provider_id=self.provider_id,
                )
            )
        authorized_versions = self._authorized_versions(context)
        requested_ids = {
            str(item).strip()
            for item in request.knowledge_base_ids
            if str(item).strip()
        }
        selected_ids = (
            sorted(requested_ids & set(authorized_versions))
            if requested_ids
            else sorted(authorized_versions)
        )
        if requested_ids and not selected_ids:
            raise CapabilityProviderError(
                CapabilityErrorInfo(
                    code="KNOWLEDGE_NOT_AVAILABLE",
                    message="请求的知识库不在当前上下文授权范围内。",
                    retryable=False,
                    request_id=context.request_id,
                    provider_id=self.provider_id,
                )
            )
        selected_versions = {
            knowledge_base_id: authorized_versions[knowledge_base_id]
            for knowledge_base_id in selected_ids
        }
        if not selected_versions:
            return KnowledgeSearchResult(
                query_id=new_id("kquery"),
                warnings=("当前上下文没有可检索的知识库。",),
            )

        legacy_request = KnowledgeSearchRequest(
            tenant_id=context.tenant_id,
            agent_id=context.agent_id,
            query=request.query,
            query_type=request.query_type,
            scope=dict(request.scope),
            max_chunks=request.max_chunks,
            budget_tokens=request.budget_tokens,
            mode="chat",
            knowledge_base_ids=selected_ids,
            knowledge_base_version_ids=list(selected_versions.values()),
        )
        response = self._service_factory(self._db).search(
            legacy_request,
            self._model_config,
            trusted_team_id=context.team_id,
            authorized_knowledge_versions=selected_versions,
        )
        if not isinstance(response, KnowledgeSearchResponse):
            raise TypeError("local Knowledge provider returned an invalid response")
        items = tuple(
            KnowledgeHit(
                hit_id=str(chunk.id),
                content=chunk.content,
                source_ref=chunk.source_ref,
                metadata=dict(chunk.metadata or {}),
            )
            for chunk in response.chunks
        )
        return KnowledgeSearchResult(
            query_id=new_id("kquery"),
            items=items,
            extensions={
                "local_knowledge": {
                    "request_id": context.request_id,
                    "trace": response.trace,
                    "route_trace": response.route_trace,
                    "selected_documents": response.selected_documents,
                    "selected_concepts": response.selected_concepts,
                    "evidence_pack": response.evidence_pack,
                }
            },
        )

    def _authorized_versions(self, context: CapabilityContext) -> dict[str, str]:
        """解析当前上下文可读投影，并把领域错误转换为 Provider 稳定错误。"""
        try:
            projections = KnowledgeAccessService(self._db).resolve_projections(
                tenant_id=context.tenant_id,
                agent_id=context.agent_id,
                team_id=context.team_id,
            )
        except KnowledgeError as exc:
            raise CapabilityProviderError(
                CapabilityErrorInfo(
                    code=exc.code,
                    message=exc.message,
                    retryable=False,
                    request_id=context.request_id,
                    provider_id=self.provider_id,
                )
            ) from exc
        return {
            projection.knowledge_base_id: projection.knowledge_base_version_id
            for projection in projections
        }

    def resolve_citation(
        self, context: CapabilityContext, provider_citation_ref: str
    ) -> CitationDetail:
        raise CapabilityProviderError(
            CapabilityErrorInfo(
                code="KNOWLEDGE_CITATION_NOT_DURABLE",
                message="Local Knowledge citation must be resolved from a persisted snapshot",
                retryable=False,
                request_id=context.request_id,
                provider_id=self.provider_id,
            )
        )
