"""Trusted publication facade. Teams reuse the selected Knowledge module's upload path."""
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException


@dataclass(frozen=True)
class KnowledgePublicationContext:
    db: Any = field(repr=False)
    user: Any = field(repr=False)
    authorization: str = field(default="", repr=False)
    module_id: str | None = None


def _provider(context):
    from staffdeck_harness.modules.registry import peek_registry
    registry = context.db.info.get("staffdeck_registry") or peek_registry()
    if registry is None:
        from staffdeck_harness.modules.builtin import KnowledgeProvider
        provider = KnowledgeProvider()
    else:
        candidates = registry.providers_for_operation("knowledge.search/v1")
        if context.module_id:
            candidates = tuple(item for item in candidates if item.manifest.module_id == context.module_id)
        if len(candidates) > 1:
            raise HTTPException(409, "存在多个知识模块，请在团队配置中明确 knowledge_module_id")
        item = candidates[0] if candidates else None
        provider = item.provider if item else None
    return provider


def publication_targets(context):
    list_targets = getattr(_provider(context), "publication_targets", None)
    if not callable(list_targets):
        raise HTTPException(409, "当前知识模块未提供发布目标目录")
    return list_targets(context)


def publish_document(context, payload):
    publish = getattr(_provider(context), "publish_document", None)
    if not callable(publish):
        raise HTTPException(409, "当前知识模块未提供文档写入能力")
    if payload.get("tenant_id") != context.user.tenant_id:
        raise HTTPException(403, "知识写入租户不匹配")
    result = publish(context, payload)
    if not isinstance(result, dict) or not result.get("knowledge_base_id") or not result.get("id"):
        raise HTTPException(502, "知识模块未返回有效入库回执")
    return result
