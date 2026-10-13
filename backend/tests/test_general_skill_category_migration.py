"""general_skills.category 的启动期补列迁移。

背景：技能广场需要「标签分类」（办公类 / 财务类 / 人力资源类 / 编程类）并按此下拉过滤。
新表由 create_all 直接建出 category；这里固定住给**已存在**的表补列的路径
（本地 dev 与线上都是老表），历史技能保持 NULL = 未分类。
"""

from __future__ import annotations

from sqlalchemy import create_engine, text

from app.db import database


def _legacy_engine(tmp_path):
    """造一张没有 category 的旧结构 general_skills 表。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'genskill-category.db'}")
    with engine.begin() as conn:
        conn.execute(
            text("CREATE TABLE general_skills (id VARCHAR PRIMARY KEY, slug VARCHAR, name VARCHAR)")
        )
        conn.execute(
            text("INSERT INTO general_skills (id, slug, name) VALUES ('legacy', 'weather-zh', '天气')")
        )
    return engine


def test_migration_adds_category_and_keeps_legacy_rows_uncategorized(tmp_path, monkeypatch) -> None:
    engine = _legacy_engine(tmp_path)
    monkeypatch.setattr(database, "engine", engine)

    database._migrate_general_skill_category()

    with engine.begin() as conn:
        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(general_skills)"))}
        assert "category" in columns
        # 历史行 NULL = 未分类，不改动原有数据
        assert conn.execute(text("SELECT category FROM general_skills")).scalar_one() is None
        assert conn.execute(text("SELECT name FROM general_skills")).scalar_one() == "天气"


def test_migration_is_idempotent(tmp_path, monkeypatch) -> None:
    engine = _legacy_engine(tmp_path)
    monkeypatch.setattr(database, "engine", engine)

    database._migrate_general_skill_category()
    database._migrate_general_skill_category()

    with engine.begin() as conn:
        columns = [row[1] for row in conn.execute(text("PRAGMA table_info(general_skills)"))]
    assert columns.count("category") == 1


def test_migration_is_a_noop_when_the_table_is_absent(tmp_path, monkeypatch) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    monkeypatch.setattr(database, "engine", engine)

    database._migrate_general_skill_category()  # 不应抛异常