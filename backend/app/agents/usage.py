"""数字员工「被使用次数」计数。

口径：**每个新建的会话算一次**，站内、分享访客、Public API（第三方 / 小程序）
一视同仁；同一个会话内的多轮对话不重复计数。

底层是 `agent_profiles.usage_count` 的冗余列：员工广场/热门榜直接读它，
不必对每个员工做一次 `count(sessions)`。
"""

from __future__ import annotations

from sqlmodel import Session, update

from app.db.models import AgentProfile, utc_now


def bump_agent_usage(db: Session, agent_id: str | None) -> None:
    """新建会话时给对应员工 usage_count +1。

    用 SQL 侧自增而不是 read-modify-write，避免并发建会话互相覆盖。
    """
    if not agent_id:
        return
    db.exec(
        update(AgentProfile)
        .where(AgentProfile.id == agent_id)
        .values(usage_count=AgentProfile.usage_count + 1, updated_at=utc_now())
        .execution_options(synchronize_session=False)
    )
