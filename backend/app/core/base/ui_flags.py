"""租户级 UI 展示开关的服务端读取。

这些开关本来是给前端用的（`GET /api/chat/ui-config`），但有些展示口径必须在
服务端兜底——比如「不展示知识来源」时正文里的 `[n]` 编号，如果只靠前端裁剪，
老客户端、浏览器缓存的旧包、分享页的第二端都会漏出来。
"""

from __future__ import annotations

from sqlmodel import Session

from app.db.models import UIConfig

_KNOWLEDGE_CITATIONS_CACHE_KEY = "ui_flags.knowledge_citations_hidden"


def knowledge_citations_hidden(db: Session | None, tenant_id: str) -> bool:
    """该租户是否关掉了「展示知识来源」。

    没配置行 / 读不到时按「展示」处理（与前端 `knowledgeCitationsHidden` 一致），
    避免配置读取失败把可见内容静默吞掉。查询结果按 session 缓存，一个请求里
    多条消息不会重复打库。
    """
    if db is None or not tenant_id:
        return False
    cache = db.info.setdefault(_KNOWLEDGE_CITATIONS_CACHE_KEY, {})
    if tenant_id not in cache:
        row = db.get(UIConfig, tenant_id)
        cache[tenant_id] = bool(row is not None and row.show_knowledge_citations is False)
    return bool(cache[tenant_id])
