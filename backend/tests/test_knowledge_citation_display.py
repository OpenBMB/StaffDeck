"""「展示知识来源」关掉后正文不再出现 [n] 编号的回归测试。

口径（与前端 `stripCitationMarkers` / `knowledgeCitationsHidden` 一致）：
- 租户开关关掉时，服务端在下发/落库前清掉正文里的引用编号，
  避免老客户端、缓存旧包、分享页第二端漏出悬空编号；
- 开关打开 / 读不到配置时保持原样（不能把正文吞掉）；
- 代码块与行内代码里的 `[1]` 是下标或示例，不能删。
"""

from __future__ import annotations

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.core.ui_flags import knowledge_citations_hidden
from app.db.models import Message, Tenant, UIConfig
from app.knowledge.citations import strip_citation_markers
from app.session.message_read import message_read


def _session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def test_strip_inline_markers() -> None:
    content = (
        "身高和体重确实都有要求[1]。\n"
        "全园设备按区域分级适配[1][2]，具体以现场公告为准。\n"
        "如有心脏不适等情况，也不能乘坐大型游乐设备。[3]"
    )
    assert strip_citation_markers(content) == (
        "身高和体重确实都有要求。\n"
        "全园设备按区域分级适配，具体以现场公告为准。\n"
        "如有心脏不适等情况，也不能乘坐大型游乐设备。"
    )


def test_keeps_markers_inside_code() -> None:
    content = "取值见 `items[1]`：\n\n```python\nprint(items[1])\n```"
    assert strip_citation_markers(content) == content


def test_keeps_markdown_links_and_definitions() -> None:
    content = "[1](https://example.com) 和 [1]: https://example.com"
    assert strip_citation_markers(content) == content


def test_keeps_long_bracket_numbers() -> None:
    assert strip_citation_markers("见 [2024] 年度报告") == "见 [2024] 年度报告"


def test_hidden_defaults_to_false_without_config_row() -> None:
    db = _session()
    db.add(Tenant(id="tenant_demo", name="Demo"))
    db.commit()
    assert knowledge_citations_hidden(db, "tenant_demo") is False


def test_hidden_follows_tenant_switch() -> None:
    db = _session()
    db.add(Tenant(id="tenant_demo", name="Demo"))
    db.commit()
    row = UIConfig(tenant_id="tenant_demo", show_knowledge_citations=True)
    db.add(row)
    db.commit()

    assert knowledge_citations_hidden(db, "tenant_demo") is False

    row.show_knowledge_citations = False
    db.add(row)
    db.commit()
    # 同一 session 里改完要能读到新值（缓存按 session 存）
    db.info.pop("ui_flags.knowledge_citations_hidden", None)
    assert knowledge_citations_hidden(db, "tenant_demo") is True


def _seed_message(db: Session, *, citation: dict) -> Message:
    db.add(Tenant(id="tenant_demo", name="Demo"))
    db.add(UIConfig(tenant_id="tenant_demo", show_knowledge_citations=False))
    message = Message(
        id="msg_1",
        tenant_id="tenant_demo",
        session_id="session_1",
        role="assistant",
        content="部分设备设有体重限制，具体以现场公告为准[1]。",
        metadata_json={"knowledge_citations": [citation]},
    )
    db.add(message)
    db.commit()
    return message


def test_message_read_strips_markers_when_switch_off() -> None:
    db = _session()
    message = _seed_message(
        db,
        citation={"id": "cite_1", "label": "[1]", "title": "乐园手册：乘坐须知"},
    )

    read = message_read(message, db=db)

    assert read.content == "部分设备设有体重限制，具体以现场公告为准。"
    # 来源元数据保留：租户把开关改回来时，卡片还能照常展示
    assert read.metadata["knowledge_citations"][0]["label"] == "[1]"
    # 库里原文不动，随时可以还原
    assert "[1]" in db.get(Message, "msg_1").content


def test_message_read_keeps_markers_when_switch_on() -> None:
    db = _session()
    message = _seed_message(
        db,
        citation={"id": "cite_1", "label": "[1]", "title": "乐园手册：乘坐须知"},
    )
    row = db.get(UIConfig, "tenant_demo")
    row.show_knowledge_citations = True
    db.add(row)
    db.commit()

    read = message_read(message, db=db)

    assert "[1]" in read.content
