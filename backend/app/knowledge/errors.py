"""共享知识库领域错误：为 HTTP 与 Agent 工具提供稳定代码和安全中文消息。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

KNOWLEDGE_CONTEXT_MISMATCH = "KNOWLEDGE_CONTEXT_MISMATCH"
KNOWLEDGE_GRANT_REQUIRED = "KNOWLEDGE_GRANT_REQUIRED"
KNOWLEDGE_DEFAULT_NOT_CONFIGURED = "KNOWLEDGE_DEFAULT_NOT_CONFIGURED"
KNOWLEDGE_PUBLISH_CONFLICT = "KNOWLEDGE_PUBLISH_CONFLICT"
KNOWLEDGE_VERSION_NOT_READY = "KNOWLEDGE_VERSION_NOT_READY"
KNOWLEDGE_MODE_INVALID = "KNOWLEDGE_MODE_INVALID"
KNOWLEDGE_BINDING_REVISION_CONFLICT = "KNOWLEDGE_BINDING_REVISION_CONFLICT"
KNOWLEDGE_IDEMPOTENCY_CONFLICT = "KNOWLEDGE_IDEMPOTENCY_CONFLICT"
KNOWLEDGE_IDEMPOTENCY_REQUIRED = "KNOWLEDGE_IDEMPOTENCY_REQUIRED"
KNOWLEDGE_CONVERSION_VALIDATION_FAILED = "KNOWLEDGE_CONVERSION_VALIDATION_FAILED"

_ERROR_DEFAULTS: dict[str, tuple[int, str]] = {
    KNOWLEDGE_CONTEXT_MISMATCH: (403, "当前会话与知识库上下文不匹配。"),
    KNOWLEDGE_GRANT_REQUIRED: (403, "当前员工没有执行此知识库操作所需的权限。"),
    KNOWLEDGE_DEFAULT_NOT_CONFIGURED: (400, "团队尚未配置默认写入知识库。"),
    KNOWLEDGE_PUBLISH_CONFLICT: (409, "知识库正式版本已变化，请基于最新版本重新操作。"),
    KNOWLEDGE_VERSION_NOT_READY: (409, "知识版本尚未完成处理或校验，暂不能发布。"),
    KNOWLEDGE_MODE_INVALID: (409, "当前知识库类型不支持此操作。"),
    KNOWLEDGE_BINDING_REVISION_CONFLICT: (409, "团队知识库配置已变化，请刷新后重试。"),
    KNOWLEDGE_IDEMPOTENCY_CONFLICT: (409, "同一幂等键已用于不同的知识库操作。"),
    KNOWLEDGE_IDEMPOTENCY_REQUIRED: (400, "Agent 知识库变更必须提供幂等键。"),
    KNOWLEDGE_CONVERSION_VALIDATION_FAILED: (409, "知识库转换后的资产校验失败。"),
}


class KnowledgeError(RuntimeError):
    """可由 API 和 Harness 统一映射的知识库领域错误。"""

    def __init__(
        self,
        code: str,
        message: str | None = None,
        *,
        status_code: int | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        """保存稳定代码、HTTP 状态和不含受保护内容的诊断标识。"""
        default_status, default_message = _ERROR_DEFAULTS.get(
            code,
            (400, "知识库操作失败。"),
        )
        self.code = code
        self.status_code = status_code or default_status
        self.message = message or default_message
        self.details = dict(details or {})
        super().__init__(self.message)


def knowledge_error(
    code: str,
    *,
    message: str | None = None,
    status_code: int | None = None,
    details: Mapping[str, Any] | None = None,
) -> KnowledgeError:
    """按稳定错误代码创建知识库异常，供服务层保持一致的失败语义。"""
    return KnowledgeError(
        code,
        message,
        status_code=status_code,
        details=details,
    )
