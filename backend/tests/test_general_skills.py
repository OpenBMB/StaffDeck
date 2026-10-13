import base64
import os
import subprocess
import sys
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote
from zipfile import ZipFile

import pytest
from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.agents.branching import ensure_open_gallery_binding
from app.api.general_skills import (
    _files_from_zip,
    _general_skill_package_archive,
    _list_my_general_skills,
    _preview_package_response,
    archive_general_skill,
    delete_general_skill,
    download_general_skill_package,
    get_general_skill,
    import_clawhub_skill,
    import_general_skill,
    import_general_skill_package,
    list_general_skills,
    preview_general_skill_package,
    publish_general_skill,
    publish_general_skill_to_gallery,
    run_general_skill,
    run_general_skill_stream,
)
from app.db.models import (
    AgentEvent,
    AgentProfile,
    AgentResourceBinding,
    ChatSession,
    GeneralSkill,
    Message,
    ModelConfig,
    Skill,
    Tenant,
    User,
)
from app.general_skills.runner import (
    GeneralSkillReader,
    GeneralSkillRunner,
    GeneralSkillSelector,
    _normalize_declared_artifacts,
)
from app.general_skills.schema import (
    GeneralSkillClawHubImportRequest,
    GeneralSkillImportRequest,
    GeneralSkillPackageUploadRequest,
    GeneralSkillRunRequest,
    GeneralSkillRunResponse,
)
from app.llm import LLMClient, LLMError
from app.security.auth import hash_password
from app.security.encryption import encrypt_secret
from app.session.session_schema import ChatTurnResponse

WEATHER_SKILL_MD = """# 中国城市天气查询工具

python weather.py -json -today <地区名称>
"""


@pytest.fixture(scope="module", autouse=True)
def _reviewed_srt_runtime(tmp_path_factory):
    if os.environ.get("STAFFDECK_SRT_RUNTIME", "").strip():
        yield
        return
    repo_root = Path(__file__).resolve().parents[2]
    runtime = repo_root / "packaging" / "sandbox_runtime"
    node = runtime / "bin" / ("node.exe" if sys.platform == "win32" else "node")
    cli = runtime / "node_modules/@anthropic-ai/sandbox-runtime/dist/cli.js"
    if not node.is_file() or not cli.is_file():
        runtime = tmp_path_factory.mktemp("reviewed-srt")
        subprocess.run(
            [
                sys.executable,
                str(repo_root / "packaging" / "fetch_sandbox_runtime.py"),
                str(runtime),
            ],
            check=True,
        )
    os.environ["STAFFDECK_SRT_RUNTIME"] = str(runtime)
    try:
        yield
    finally:
        os.environ.pop("STAFFDECK_SRT_RUNTIME", None)


def test_runner_accepts_only_explicit_artifact_manifest_files(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    artifact_dir = workspace / "general_skill_test" / "artifacts"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "final.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (artifact_dir / "cache.tmp").write_text("internal", encoding="utf-8")
    structured = {
        "success": True,
        "artifacts": [
            {"path": "final.csv", "display_name": "结果.csv"},
            {"path": "../outside.txt"},
        ],
    }

    _normalize_declared_artifacts(
        structured,
        artifact_root=artifact_dir,
        workspace_root=workspace,
    )

    assert structured["artifacts"] == [
        {
            "path": "general_skill_test/artifacts/final.csv",
            "display_name": "结果.csv",
        }
    ]
    assert [item["path"] for item in structured["artifact_errors"]] == ["../outside.txt"]
    assert all(item["path"] != "cache.tmp" for item in structured["artifacts"])


def _system_and_stage_instructions(system_prompt: object, payload: object) -> str:
    stage = payload.get("_agent_stage", {}) if isinstance(payload, dict) else {}
    instructions = stage.get("instructions", "") if isinstance(stage, dict) else ""
    return f"{system_prompt}\n{instructions}"


def test_capability_selector_allows_general_skill_and_knowledge_together(monkeypatch) -> None:
    monkeypatch.setattr(LLMClient, "__init__", lambda self, model_config: None)
    monkeypatch.setattr(
        LLMClient,
        "generate_json",
        lambda self, system_prompt, payload: {
            "use_general_skill": True,
            "selected_slug": "weather-zh",
            "use_knowledge": True,
            "knowledge_query": "内部出差规范对天气风险有什么要求",
            "confidence": 0.93,
            "reason": "需要天气能力和内部出差规范共同回答。",
        },
    )
    skill = GeneralSkill(
        tenant_id="tenant_demo",
        slug="weather-zh",
        name="中国城市天气",
        skill_markdown=WEATHER_SKILL_MD,
        status="published",
    )

    decision = GeneralSkillSelector().decide(
        "结合天气和公司规范给出建议", [skill], SimpleNamespace()
    )

    assert decision.use_general_skill is True
    assert decision.selected_slug == "weather-zh"
    assert decision.use_knowledge is True
    assert decision.knowledge_query == "内部出差规范对天气风险有什么要求"


def test_capability_selector_still_checks_knowledge_without_general_skills(monkeypatch) -> None:
    received: dict[str, object] = {}
    monkeypatch.setattr(LLMClient, "__init__", lambda self, model_config: None)

    def fake_generate_json(self, system_prompt, payload):  # noqa: ANN001
        received.update(payload)
        return {
            "use_general_skill": False,
            "selected_slug": None,
            "use_knowledge": True,
            "knowledge_query": "员工报销的审批要求",
            "confidence": 0.88,
            "reason": "回答依赖企业文档。",
        }

    monkeypatch.setattr(LLMClient, "generate_json", fake_generate_json)

    decision = GeneralSkillSelector().decide("这种费用应该怎么报", [], SimpleNamespace())

    assert received["general_skills"] == []
    assert decision.use_general_skill is False
    assert decision.use_knowledge is True
    assert decision.knowledge_query == "员工报销的审批要求"


def test_general_skill_reader_does_not_generate_runner_code(monkeypatch) -> None:
    monkeypatch.setattr(LLMClient, "__init__", lambda self, model_config: None)

    def fake_generate_json(self, system_prompt, payload):  # noqa: ANN001
        instructions = payload["_agent_stage"]["instructions"]
        assert "只读说明器" in instructions
        return {
            "reply": "这是一个天气查询 Skill，只负责查询天气，不会修改业务数据。",
            "summary": "查询天气",
            "inputs": ["城市"],
            "side_effects": [],
        }

    monkeypatch.setattr(LLMClient, "generate_json", fake_generate_json)
    skill = GeneralSkill(
        tenant_id="tenant_demo",
        slug="weather-zh",
        name="中国城市天气",
        description="查询中国城市实时天气",
        skill_markdown=WEATHER_SKILL_MD,
        status="published",
    )

    response = GeneralSkillReader().read(skill, "介绍这个 Skill", SimpleNamespace())

    assert response.operation == "read"
    assert response.generated_code == ""
    assert response.structured_result["operation"] == "read"
    assert any(item["phase"] == "read_created" for item in response.execution_trace)


def test_general_skill_reader_returns_structured_failure(monkeypatch) -> None:
    monkeypatch.setattr(LLMClient, "__init__", lambda self, model_config: None)
    monkeypatch.setattr(
        LLMClient,
        "generate_json",
        lambda self, system_prompt, payload: (_ for _ in ()).throw(LLMError("model unavailable")),
    )
    skill = GeneralSkill(
        tenant_id="tenant_demo",
        slug="weather-zh",
        name="中国城市天气",
        skill_markdown=WEATHER_SKILL_MD,
        status="published",
    )

    response = GeneralSkillReader().read(skill, "介绍这个 Skill", SimpleNamespace())

    assert response.operation == "read"
    assert response.structured_result == {
        "success": False,
        "operation": "read",
        "error": "skill_read_failed",
        "message": "model unavailable",
    }
    assert any(item["phase"] == "read_failed" for item in response.execution_trace)


def _admin_user() -> User:
    return User(
        id="user_admin",
        tenant_id="tenant_demo",
        username="admin",
        role="admin",
        password_hash="test",
    )


def test_import_general_skill_uses_user_supplied_metadata() -> None:
    with _test_session() as db:
        _seed_minimal_tenant(db)
        db.add(
            AgentProfile(
                id="agent_overall", tenant_id="tenant_demo", name="整体智能体", is_overall=True
            )
        )
        db.commit()

        first = import_general_skill(
            GeneralSkillImportRequest(
                tenant_id="tenant_demo",
                name="用户填写天气技能",
                slug="weather-zh",
                description="用户填写描述",
                homepage="https://example.com/weather",
                markdown=WEATHER_SKILL_MD,
            ),
            db,
            _admin_user(),
        )
        second = import_general_skill(
            GeneralSkillImportRequest(
                tenant_id="tenant_demo",
                name="用户改名天气技能",
                slug="weather-zh",
                description="用户改写描述",
                homepage="https://example.com/weather-cn",
                original_slug="weather-zh",
                markdown=WEATHER_SKILL_MD.replace("中国城市天气查询工具", "天气 demo"),
            ),
            db,
            _admin_user(),
        )

        rows = list_general_skills("tenant_demo", db)
        assert first.id == second.id
        assert len(rows) == 1
        assert rows[0].slug == "weather-zh"
        assert rows[0].name == "用户改名天气技能"
        assert rows[0].description == "用户改写描述"
        assert rows[0].homepage == "https://example.com/weather-cn"
        assert rows[0].skill_markdown.startswith("# 天气 demo")

        try:
            import_general_skill(
                GeneralSkillImportRequest(
                    tenant_id="tenant_demo",
                    name="非法改 slug",
                    slug="weather-cn",
                    original_slug="weather-zh",
                    markdown=WEATHER_SKILL_MD,
                ),
                db,
                _admin_user(),
            )
            assert False, "expected general skill slug update to fail"
        except HTTPException as exc:
            assert exc.status_code == 400
            assert exc.detail == "General skill slug cannot be modified"


def test_import_general_skill_persists_and_clears_category() -> None:
    """标签分类：新建时落库、改名（不传）时保留、显式空串时清空。"""
    with _test_session() as db:
        _seed_minimal_tenant(db)
        db.add(
            AgentProfile(
                id="agent_overall", tenant_id="tenant_demo", name="整体智能体", is_overall=True
            )
        )
        db.commit()

        created = import_general_skill(
            GeneralSkillImportRequest(
                tenant_id="tenant_demo",
                name="分类技能",
                slug="categorized",
                category="财务类",
                markdown=WEATHER_SKILL_MD,
            ),
            db,
            _admin_user(),
        )
        assert created.category == "财务类"
        assert list_general_skills("tenant_demo", db)[0].category == "财务类"

        # 不传 category（如从开源平台整包导入）→ 保留原分类
        kept = import_general_skill(
            GeneralSkillImportRequest(
                tenant_id="tenant_demo",
                name="分类技能改",
                slug="categorized",
                original_slug="categorized",
                markdown=WEATHER_SKILL_MD,
            ),
            db,
            _admin_user(),
        )
        assert kept.category == "财务类"

        # 显式空串 → 清空（表单里选「未分类」后保存）
        cleared = import_general_skill(
            GeneralSkillImportRequest(
                tenant_id="tenant_demo",
                name="分类技能改",
                slug="categorized",
                original_slug="categorized",
                category="",
                markdown=WEATHER_SKILL_MD,
            ),
            db,
            _admin_user(),
        )
        assert cleared.category is None
        assert list_general_skills("tenant_demo", db)[0].category is None


def test_import_general_skill_without_original_slug_does_not_overwrite_existing() -> None:
    with _test_session() as db:
        _seed_minimal_tenant(db)
        db.add(
            AgentProfile(
                id="agent_overall", tenant_id="tenant_demo", name="整体智能体", is_overall=True
            )
        )
        db.commit()

        first = import_general_skill(
            GeneralSkillImportRequest(
                tenant_id="tenant_demo",
                name="已有天气技能",
                slug="weather-zh",
                markdown=WEATHER_SKILL_MD,
            ),
            db,
            _admin_user(),
        )

        try:
            import_general_skill(
                GeneralSkillImportRequest(
                    tenant_id="tenant_demo",
                    name="新导入天气技能",
                    slug="weather-zh",
                    markdown="# 新内容",
                ),
                db,
                _admin_user(),
            )
        except HTTPException as error:
            assert error.status_code == 409
        else:
            raise AssertionError("expected slug conflict")

        rows = list_general_skills("tenant_demo", db)
        assert len(rows) == 1
        assert rows[0].id == first.id
        assert rows[0].name == "已有天气技能"
        assert rows[0].skill_markdown == WEATHER_SKILL_MD.strip()


def test_deleted_open_gallery_general_skill_binding_is_not_restored_by_ensure() -> None:
    with _test_session() as db:
        _seed_minimal_tenant(db)
        db.add(
            AgentProfile(
                id="agent_overall", tenant_id="tenant_demo", name="整体智能体", is_overall=True
            )
        )
        db.commit()

        imported = import_general_skill(
            GeneralSkillImportRequest(
                tenant_id="tenant_demo",
                name="天气技能",
                slug="weather-zh",
                markdown=WEATHER_SKILL_MD,
            ),
            db,
            _admin_user(),
        )

        deleted = delete_general_skill(
            imported.slug,
            "tenant_demo",
            db,
            agent_id="agent_overall",
            current_user=_admin_user(),
        )
        assert deleted == {"status": "hidden", "slug": "weather-zh"}

        ensure_open_gallery_binding(db, "tenant_demo", "general_skill", imported.id, "active")
        db.commit()

        binding = db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.tenant_id == "tenant_demo",
                AgentResourceBinding.agent_id == "agent_overall",
                AgentResourceBinding.resource_type == "general_skill",
                AgentResourceBinding.resource_id == imported.id,
            )
        ).one()
        assert binding.status == "deleted"
        assert list_general_skills("tenant_demo", db) == []


def test_reimport_restores_deleted_private_skill_binding() -> None:
    with _test_session() as db:
        _seed_minimal_tenant(db)
        db.add(
            AgentProfile(
                id="agent_overall", tenant_id="tenant_demo", name="整体智能体", is_overall=True
            )
        )
        db.add(
            AgentProfile(
                id="agent_branch", tenant_id="tenant_demo", name="研发员工", is_overall=False
            )
        )
        db.commit()

        imported = import_general_skill(
            GeneralSkillImportRequest(
                tenant_id="tenant_demo",
                agent_id="agent_branch",
                name="天气技能",
                slug="weather-zh",
                markdown=WEATHER_SKILL_MD,
            ),
            db,
            _admin_user(),
        )
        delete_general_skill(
            imported.slug,
            "tenant_demo",
            db,
            agent_id="agent_branch",
            current_user=_admin_user(),
        )

        restored = import_general_skill(
            GeneralSkillImportRequest(
                tenant_id="tenant_demo",
                agent_id="agent_branch",
                name="更新后的天气技能",
                slug="weather-zh",
                markdown=WEATHER_SKILL_MD.replace("中国城市天气查询工具", "更新后的天气工具"),
            ),
            db,
            _admin_user(),
        )

        assert restored.id == imported.id
        assert restored.slug == "weather-zh"
        assert restored.name == "更新后的天气技能"
        assert [
            row.id for row in list_general_skills("tenant_demo", db, agent_id="agent_branch")
        ] == [imported.id]


def test_private_skill_can_be_published_to_open_gallery() -> None:
    with _test_session() as db:
        _seed_minimal_tenant(db)
        db.add(
            AgentProfile(
                id="agent_overall", tenant_id="tenant_demo", name="整体智能体", is_overall=True
            )
        )
        db.add(
            AgentProfile(
                id="agent_branch", tenant_id="tenant_demo", name="研发员工", is_overall=False
            )
        )
        db.commit()

        imported = import_general_skill(
            GeneralSkillImportRequest(
                tenant_id="tenant_demo",
                agent_id="agent_branch",
                name="天气技能",
                slug="weather-zh",
                markdown=WEATHER_SKILL_MD,
            ),
            db,
            _admin_user(),
        )
        published = publish_general_skill_to_gallery(
            imported.slug,
            "tenant_demo",
            "agent_branch",
            db,
            _admin_user(),
        )

        assert published.id == imported.id
        assert list_general_skills("tenant_demo", db) == [published]
        binding = db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.tenant_id == "tenant_demo",
                AgentResourceBinding.agent_id == "agent_overall",
                AgentResourceBinding.resource_type == "general_skill",
                AgentResourceBinding.resource_id == imported.id,
            )
        ).one()
        assert binding.status == "active"


def test_import_general_skill_folder_reads_skill_md_metadata() -> None:
    with _test_session() as db:
        _seed_minimal_tenant(db)

        row = import_general_skill(
            GeneralSkillImportRequest(
                tenant_id="tenant_demo",
                files=[
                    {
                        "path": "weather-bundle/SKILL.md",
                        "content": (
                            "---\n"
                            "name: 中国城市天气\n"
                            "slug: weather-zh\n"
                            "description: 从目录包读取天气技能\n"
                            "homepage: https://example.com/weather\n"
                            "---\n\n"
                            "# 使用说明\n"
                            "读取 data/cities.json 完成查询。\n"
                        ),
                    },
                    {
                        "path": "weather-bundle/data/cities.json",
                        "content": '{"北京": "101010100"}',
                    },
                ],
            ),
            db,
            _admin_user(),
        )

        assert row.name == "中国城市天气"
        assert row.slug == "weather-zh"
        assert row.description == "从目录包读取天气技能"
        assert row.homepage == "https://example.com/weather"
        assert row.metadata["name"] == "中国城市天气"
        assert [file.path for file in row.skill_files] == ["SKILL.md", "data/cities.json"]
        assert row.skill_markdown.startswith("---\nname: 中国城市天气")


def test_import_general_skill_rejects_missing_reference_files() -> None:
    with _test_session() as db:
        _seed_minimal_tenant(db)

        with pytest.raises(HTTPException) as exc_info:
            import_general_skill(
                GeneralSkillImportRequest(
                    tenant_id="tenant_demo",
                    name="企业微信日程",
                    slug="wecom-calendar",
                    markdown=(
                        "# 企业微信日程\n\n"
                        "执行前读取 references/wecomcli-calendar-meeting-room.md。\n"
                    ),
                ),
                db,
                _admin_user(),
            )

        assert exc_info.value.status_code == 400
        assert "references/wecomcli-calendar-meeting-room.md" in str(exc_info.value.detail)


def test_import_general_skill_accepts_markdown_link_references() -> None:
    """SKILL.md 用 `[x.md](./references/x.md)` 引用参考文件时不得误判缺失。

    这是 2026-09-20 报错 400 的根因：旧正则 `references/[^\\s...]+` 会把
    `](./references/products/aitable.md)` 整段吞成一个永不可能存在的路径。
    """
    with _test_session() as db:
        _seed_minimal_tenant(db)

        created = import_general_skill(
            GeneralSkillImportRequest(
                tenant_id="tenant_demo",
                name="飞书工具箱",
                slug="lark-toolkit",
                files=[
                    {
                        "path": "SKILL.md",
                        "content": (
                            "# 飞书工具箱\n\n"
                            "| 产品 | 参考文件 |\n"
                            "| --- | --- |\n"
                            "| AI表格 | [aitable.md](./references/products/aitable.md) |\n"
                            "| 云文档 | [doc.md](./references/products/doc.md) |\n\n"
                            "参考目录：references/products/\n"
                            "通配引用：references/products/*.md\n"
                        ),
                    },
                    {"path": "references/products/aitable.md", "content": "# AI表格\n"},
                    {"path": "references/products/doc.md", "content": "# 云文档\n"},
                ],
            ),
            db,
            _admin_user(),
        )

        assert created.slug == "lark-toolkit"


def test_general_skill_reference_validation_reports_only_real_missing_paths() -> None:
    with _test_session() as db:
        _seed_minimal_tenant(db)

        with pytest.raises(HTTPException) as exc_info:
            import_general_skill(
                GeneralSkillImportRequest(
                    tenant_id="tenant_demo",
                    name="飞书工具箱",
                    slug="lark-toolkit",
                    files=[
                        {
                            "path": "SKILL.md",
                            "content": (
                                "# 飞书工具箱\n\n"
                                "| AI表格 | [aitable.md](./references/products/aitable.md) |\n"
                                "| AI应用 | [aiapp.md](./references/products/aiapp.md) |\n"
                            ),
                        },
                        {"path": "references/products/aitable.md", "content": "# AI表格\n"},
                    ],
                ),
                db,
                _admin_user(),
            )

        assert exc_info.value.status_code == 400
        detail = exc_info.value.detail
        # 缺失清单必须恰好等于真实缺失，且路径是可读的原始路径（不带 `](` 之类的语法碎片）
        assert detail["code"] == "GENERAL_SKILL_MISSING_REFERENCES"
        assert detail["paths"] == ["references/products/aiapp.md"]


def test_import_general_skill_allows_missing_references_when_confirmed() -> None:
    """前端二次确认（allow_missing_references=true）后，缺参考文件不再阻断导入。"""
    with _test_session() as db:
        _seed_minimal_tenant(db)

        created = import_general_skill(
            GeneralSkillImportRequest(
                tenant_id="tenant_demo",
                name="飞书工具箱",
                slug="lark-toolkit",
                allow_missing_references=True,
                files=[
                    {
                        "path": "SKILL.md",
                        "content": (
                            "# 飞书工具箱\n\n"
                            "| AI应用 | [aiapp.md](./references/products/aiapp.md) |\n"
                        ),
                    },
                ],
            ),
            db,
            _admin_user(),
        )

        assert created.slug == "lark-toolkit"
        assert [file.path for file in created.skill_files] == ["SKILL.md"]


def test_markdown_only_update_preserves_existing_skill_package_files() -> None:
    with _test_session() as db:
        _seed_minimal_tenant(db)
        created = import_general_skill(
            GeneralSkillImportRequest(
                tenant_id="tenant_demo",
                name="企业微信日程",
                slug="wecom-calendar",
                files=[
                    {
                        "path": "SKILL.md",
                        "content": (
                            "# 企业微信日程\n\n"
                            "读取 references/wecomcli-calendar-meeting-room.md。\n"
                        ),
                    },
                    {
                        "path": "references/wecomcli-calendar-meeting-room.md",
                        "content": "# 会议室日程查询\n",
                    },
                ],
            ),
            db,
            _admin_user(),
        )

        updated = import_general_skill(
            GeneralSkillImportRequest(
                tenant_id="tenant_demo",
                name="企业微信日程",
                slug=created.slug,
                original_slug=created.slug,
                markdown=(
                    "# 企业微信日程（更新）\n\n"
                    "读取 references/wecomcli-calendar-meeting-room.md。\n"
                ),
            ),
            db,
            _admin_user(),
        )

        assert updated.id == created.id
        assert [file.path for file in updated.skill_files] == [
            "SKILL.md",
            "references/wecomcli-calendar-meeting-room.md",
        ]
        assert updated.skill_files[1].content == "# 会议室日程查询\n"


def test_import_general_skill_persists_empty_directories_across_updates() -> None:
    with _test_session() as db:
        _seed_minimal_tenant(db)

        created = import_general_skill(
            GeneralSkillImportRequest(
                tenant_id="tenant_demo",
                name="目录技能",
                slug="directory-skill",
                files=[{"path": "SKILL.md", "content": "# 目录技能\n"}],
                directories=["references", "references/drafts", "empty"],
            ),
            db,
            _admin_user(),
        )

        assert created.skill_directories == ["references", "references/drafts", "empty"]
        stored = db.get(GeneralSkill, created.id)
        assert stored is not None
        assert stored.metadata_json["skill_directories"] == [
            "references",
            "references/drafts",
            "empty",
        ]

        updated = import_general_skill(
            GeneralSkillImportRequest(
                tenant_id="tenant_demo",
                name="目录技能",
                slug="directory-skill",
                original_slug="directory-skill",
                files=[{"path": "SKILL.md", "content": "# 更新后的目录技能\n"}],
            ),
            db,
            _admin_user(),
        )

        assert updated.skill_directories == created.skill_directories


def test_import_clawhub_skill_reads_zip_package_without_overwriting(monkeypatch) -> None:
    package = BytesIO()
    with ZipFile(package, "w") as archive:
        archive.writestr(
            "skill-pack-main/weather/SKILL.md",
            "---\nname: 天气包\nslug: weather-pack\n---\n\n# 天气包\n",
        )
        archive.writestr("skill-pack-main/weather/scripts/run.py", "print('ok')\n")
        archive.writestr("skill-pack-main/weather/data/cities.json", '{"北京": "101010100"}')

    def fake_download(url: str):  # noqa: ANN001
        assert url == "https://example.com/weather.zip"
        return package.getvalue(), "application/zip"

    monkeypatch.setattr("app.api.general_skills._download_url", fake_download)

    with _test_session() as db:
        _seed_minimal_tenant(db)
        first = import_clawhub_skill(
            GeneralSkillClawHubImportRequest(
                tenant_id="tenant_demo", source="https://example.com/weather.zip"
            ),
            db,
            _admin_user(),
        )
        second = import_clawhub_skill(
            GeneralSkillClawHubImportRequest(
                tenant_id="tenant_demo", source="https://example.com/weather.zip"
            ),
            db,
            _admin_user(),
        )

        assert first.slug == "weather-pack"
        assert second.slug == "weather-pack-2"
        assert [file.path for file in first.skill_files] == [
            "SKILL.md",
            "scripts/run.py",
            "data/cities.json",
        ]
        assert first.skill_markdown.startswith("---\nname: 天气包")


def test_import_general_skill_package_upload_keeps_full_zip_folder() -> None:
    package = BytesIO()
    with ZipFile(package, "w") as archive:
        archive.writestr(
            "nuwa-skill-main/skill/SKILL.md",
            "---\nname: Nuwa Skill\nslug: nuwa-skill\n---\n\n# Nuwa Skill\n",
        )
        archive.writestr("nuwa-skill-main/skill/scripts/run.py", "print('nuwa')\n")
        archive.writestr("nuwa-skill-main/skill/assets/config.json", '{"mode":"demo"}')

    with _test_session() as db:
        _seed_minimal_tenant(db)
        row = import_general_skill_package(
            GeneralSkillPackageUploadRequest(
                tenant_id="tenant_demo",
                filename="nuwa-skill.zip",
                content_base64=base64.b64encode(package.getvalue()).decode("ascii"),
                status="published",
            ),
            db,
            _admin_user(),
        )

        assert row.slug == "nuwa-skill"
        assert row.name == "Nuwa Skill"
        assert [file.path for file in row.skill_files] == [
            "SKILL.md",
            "scripts/run.py",
            "assets/config.json",
        ]
        assert row.skill_markdown.startswith("---\nname: Nuwa Skill")


def test_import_general_skill_package_upload_treats_single_markdown_as_skill_md() -> None:
    markdown = "---\nname: 单文件技能\nslug: single-file-skill\n---\n\n# 单文件技能\n"

    with _test_session() as db:
        _seed_minimal_tenant(db)
        row = import_general_skill_package(
            GeneralSkillPackageUploadRequest(
                tenant_id="tenant_demo",
                filename="readme.md",
                content_base64=base64.b64encode(markdown.encode("utf-8")).decode("ascii"),
                status="published",
            ),
            db,
            _admin_user(),
        )

        assert row.slug == "single-file-skill"
        assert [file.path for file in row.skill_files] == ["SKILL.md"]


def test_preview_general_skill_package_returns_editor_payload_without_creating_row() -> None:
    """预览接口解析 zip 并返回可填入编辑器的全部内容，但不创建技能、不占用 slug。"""
    package = BytesIO()
    with ZipFile(package, "w") as archive:
        archive.writestr(
            "nuwa-skill-main/skill/SKILL.md",
            "---\nname: Nuwa Skill\nslug: nuwa-skill\ndescription: 预览解析\n---\n\n# Nuwa Skill\n",
        )
        archive.writestr("nuwa-skill-main/skill/scripts/run.py", "print('nuwa')\n")

    with _test_session() as db:
        _seed_minimal_tenant(db)
        before = db.exec(select(GeneralSkill)).all()
        payload = preview_general_skill_package(
            GeneralSkillPackageUploadRequest(
                tenant_id="tenant_demo",
                filename="nuwa-skill.zip",
                content_base64=base64.b64encode(package.getvalue()).decode("ascii"),
            )
        )

        # 不落库
        assert db.exec(select(GeneralSkill)).all() == before
        # 解析结果与导入同源：名称 / 描述 / markdown / 文件都来自包内 SKILL.md
        assert payload.name == "Nuwa Skill"
        assert payload.slug == "nuwa-skill"
        assert payload.description == "预览解析"
        assert payload.markdown.startswith("---\nname: Nuwa Skill")
        assert [file.path for file in payload.files] == ["SKILL.md", "scripts/run.py"]
        assert payload.files[0].content.startswith("---\nname: Nuwa Skill")


def test_preview_general_skill_package_supports_single_markdown() -> None:
    markdown = "---\nname: 单文件技能\nslug: single-file-skill\n---\n\n# 单文件技能\n"
    payload = preview_general_skill_package(
        GeneralSkillPackageUploadRequest(
            tenant_id="tenant_demo",
            filename="readme.md",
            content_base64=base64.b64encode(markdown.encode("utf-8")).decode("ascii"),
        )
    )

    assert payload.name == "单文件技能"
    assert [file.path for file in payload.files] == ["SKILL.md"]


def test_preview_package_response_shared_by_multipart_entry() -> None:
    """_preview_package_response 是 JSON / multipart 两个入口共用的解析核：zip 同构。"""
    package = BytesIO()
    with ZipFile(package, "w") as archive:
        archive.writestr(
            "tool-skill/SKILL.md",
            "---\nname: Tool Skill\nslug: tool-skill\n---\n\n# Tool Skill\n",
        )

    payload = _preview_package_response("tool-skill.zip", package.getvalue())

    assert payload.name == "Tool Skill"
    assert [file.path for file in payload.files] == ["SKILL.md"]


def test_import_clawhub_skill_reads_github_directory_package(monkeypatch) -> None:
    def fake_json(url: str):  # noqa: ANN001
        if url == "https://api.github.com/repos/example/skill-pack/contents/weather?ref=main":
            return [
                {
                    "type": "file",
                    "path": "weather/SKILL.md",
                    "download_url": "https://raw.githubusercontent.com/example/skill-pack/main/weather/SKILL.md",
                    "size": 46,
                },
                {
                    "type": "dir",
                    "path": "weather/scripts",
                },
                {
                    "type": "file",
                    "path": "weather/data/cities.json",
                    "download_url": "https://raw.githubusercontent.com/example/skill-pack/main/weather/data/cities.json",
                    "size": 24,
                },
            ]
        if (
            url
            == "https://api.github.com/repos/example/skill-pack/contents/weather/scripts?ref=main"
        ):
            return [
                {
                    "type": "file",
                    "path": "weather/scripts/run.py",
                    "download_url": "https://raw.githubusercontent.com/example/skill-pack/main/weather/scripts/run.py",
                    "size": 12,
                }
            ]
        raise AssertionError(f"unexpected github api url: {url}")

    def fake_download(url: str):  # noqa: ANN001
        content = {
            "https://raw.githubusercontent.com/example/skill-pack/main/weather/SKILL.md": "---\nname: 目录天气\nslug: weather-dir\n---\n\n# 天气\n",
            "https://raw.githubusercontent.com/example/skill-pack/main/weather/scripts/run.py": "print('ok')\n",
            "https://raw.githubusercontent.com/example/skill-pack/main/weather/data/cities.json": '{"北京":"101010100"}',
        }.get(url)
        if content is None:
            raise AssertionError(f"unexpected raw url: {url}")
        return content.encode("utf-8"), "text/plain"

    monkeypatch.setattr("app.api.general_skills._download_json", fake_json)
    monkeypatch.setattr("app.api.general_skills._download_url", fake_download)

    with _test_session() as db:
        _seed_minimal_tenant(db)
        row = import_clawhub_skill(
            GeneralSkillClawHubImportRequest(
                tenant_id="tenant_demo",
                source="https://github.com/example/skill-pack/tree/main/weather",
            ),
            db,
            _admin_user(),
        )

        assert row.slug == "weather-dir"
        assert [file.path for file in row.skill_files] == [
            "SKILL.md",
            "scripts/run.py",
            "data/cities.json",
        ]
        assert row.skill_files[1].content == "print('ok')\n"


def test_import_clawhub_skill_follows_page_to_real_skill_package(monkeypatch) -> None:
    def fake_download(url: str):  # noqa: ANN001
        if url == "https://clawhub.example/skills/weather":
            return (
                b'<html><a href="https://github.com/example/skill-pack/tree/main/weather">download</a></html>',
                "text/html",
            )
        content = {
            "https://raw.githubusercontent.com/example/skill-pack/main/weather/SKILL.md": "---\nname: 页面天气\nslug: weather-page\n---\n\n# 天气\n",
        }.get(url)
        if content is None:
            raise AssertionError(f"unexpected url: {url}")
        return content.encode("utf-8"), "text/plain"

    def fake_json(url: str):  # noqa: ANN001
        assert url == "https://api.github.com/repos/example/skill-pack/contents/weather?ref=main"
        return [
            {
                "type": "file",
                "path": "weather/SKILL.md",
                "download_url": "https://raw.githubusercontent.com/example/skill-pack/main/weather/SKILL.md",
                "size": 46,
            }
        ]

    monkeypatch.setattr("app.api.general_skills._download_json", fake_json)
    monkeypatch.setattr("app.api.general_skills._download_url", fake_download)

    with _test_session() as db:
        _seed_minimal_tenant(db)
        row = import_clawhub_skill(
            GeneralSkillClawHubImportRequest(
                tenant_id="tenant_demo", source="https://clawhub.example/skills/weather"
            ),
            db,
            _admin_user(),
        )

        assert row.slug == "weather-page"
        assert row.skill_files[0].path == "SKILL.md"


def test_import_clawhub_skill_uses_clawhub_download_api_for_page_url(monkeypatch) -> None:
    package = BytesIO()
    with ZipFile(package, "w") as archive:
        archive.writestr(
            "SKILL.md",
            "---\nname: weather\n---\n\n# 天气\n",
        )
        archive.writestr("scripts/weather.py", "print('weather')\n")
        archive.writestr("references/weather_details.md", "# details\n")

    calls: list[str] = []

    def fake_download(url: str):  # noqa: ANN001
        calls.append(url)
        assert url == "https://wry-manatee-359.convex.site/api/v1/download?slug=maomao-weather"
        return package.getvalue(), "application/zip"

    monkeypatch.setattr("app.api.general_skills._download_url", fake_download)

    with _test_session() as db:
        _seed_minimal_tenant(db)
        row = import_clawhub_skill(
            GeneralSkillClawHubImportRequest(
                tenant_id="tenant_demo",
                source="https://clawhub.ai/maomaoshuo/maomao-weather",
            ),
            db,
            _admin_user(),
        )

        assert calls == ["https://wry-manatee-359.convex.site/api/v1/download?slug=maomao-weather"]
        assert row.name == "weather"
        assert row.slug == "maomao-weather"
        assert row.homepage == "https://clawhub.ai/maomaoshuo/maomao-weather"
        assert [file.path for file in row.skill_files] == [
            "SKILL.md",
            "scripts/weather.py",
            "references/weather_details.md",
        ]


def test_import_clawhub_skill_accepts_cli_slug(monkeypatch) -> None:
    package = BytesIO()
    with ZipFile(package, "w") as archive:
        archive.writestr("SKILL.md", "---\nname: weather\n---\n\n# 天气\n")

    def fake_download(url: str):  # noqa: ANN001
        assert url == "https://wry-manatee-359.convex.site/api/v1/download?slug=maomao-weather"
        return package.getvalue(), "application/zip"

    monkeypatch.setattr("app.api.general_skills._download_url", fake_download)

    with _test_session() as db:
        _seed_minimal_tenant(db)
        row = import_clawhub_skill(
            GeneralSkillClawHubImportRequest(tenant_id="tenant_demo", source="maomao-weather"),
            db,
            _admin_user(),
        )

        assert row.slug == "maomao-weather"
        assert row.skill_files[0].content.startswith("---\nname: weather")


def test_import_clawhub_skill_rejects_plain_html_page(monkeypatch) -> None:
    def fake_download(url: str):  # noqa: ANN001
        assert url == "https://clawhub.example/skills/weather"
        return b"<html><body>skill landing page without package</body></html>", "text/html"

    monkeypatch.setattr("app.api.general_skills._download_url", fake_download)

    with _test_session() as db:
        _seed_minimal_tenant(db)
        try:
            import_clawhub_skill(
                GeneralSkillClawHubImportRequest(
                    tenant_id="tenant_demo", source="https://clawhub.example/skills/weather"
                ),
                db,
                _admin_user(),
            )
        except HTTPException as error:
            assert error.status_code == 400
            assert "HTML 页面不会被当作 SKILL.md 导入" in str(error.detail)
        else:
            raise AssertionError("plain HTML page must not be imported as SKILL.md")


def test_general_skill_archive_publish_and_delete_api(monkeypatch) -> None:
    captured_model_ids: list[str] = []
    read_queries: list[str] = []

    def fake_run(
        self, skill, query, model_config, user_id="enterprise_demo", max_attempts=5, event_sink=None
    ):  # noqa: ANN001
        captured_model_ids.append(model_config.id)
        return {
            "skill_slug": skill.slug,
            "execution_trace": [],
            "generated_code": "",
            "stdout": "",
            "stderr": "",
            "structured_result": {"success": True},
            "reply": f"{query} ok",
        }

    monkeypatch.setattr(GeneralSkillRunner, "run", fake_run)

    def fake_read(self, skill, query, model_config, **kwargs):  # noqa: ANN001
        read_queries.append(query)
        return GeneralSkillRunResponse(
            skill_slug=skill.slug,
            operation="read",
            structured_result={"success": True, "operation": "read"},
            reply=f"{query} read",
        )

    monkeypatch.setattr(GeneralSkillReader, "read", fake_read)

    with _test_session() as db:
        _seed_minimal_tenant(db)
        db.add(
            AgentProfile(
                id="agent_overall", tenant_id="tenant_demo", name="开放广场", is_overall=True
            )
        )
        db.add(
            ModelConfig(
                id="model_selected",
                tenant_id="tenant_demo",
                name="Selected model",
                api_key_encrypted=encrypt_secret("selected-key"),
                model="selected",
                enabled=True,
            )
        )
        db.commit()
        imported = import_general_skill(
            GeneralSkillImportRequest(
                tenant_id="tenant_demo",
                name="天气",
                slug="weather-zh",
                markdown=WEATHER_SKILL_MD,
            ),
            db,
            _admin_user(),
        )

        archived = archive_general_skill(
            imported.slug, "tenant_demo", db, current_user=_admin_user()
        )
        assert archived.status == "archived"
        try:
            run_general_skill(
                imported.slug,
                GeneralSkillRunRequest(
                    tenant_id="tenant_demo", user_id="user_demo", query="北京天气"
                ),
                db,
                _admin_user(),
            )
        except HTTPException as error:
            assert error.status_code == 400
            assert "not published" in str(error.detail)
        else:
            raise AssertionError("archived general skill should not run")

        published = publish_general_skill(
            imported.slug, "tenant_demo", db, current_user=_admin_user()
        )
        assert published.status == "published"
        result = run_general_skill(
            imported.slug,
            GeneralSkillRunRequest(tenant_id="tenant_demo", user_id="user_demo", query="北京天气"),
            db,
            _admin_user(),
        )
        assert result["reply"] == "北京天气 ok"

        selected_result = run_general_skill(
            imported.slug,
            GeneralSkillRunRequest(
                tenant_id="tenant_demo",
                user_id="user_demo",
                query="上海天气",
                model_config_id="model_selected",
            ),
            db,
            _admin_user(),
        )
        assert selected_result["reply"] == "上海天气 ok"
        assert captured_model_ids[-1] == "model_selected"

        read_result = run_general_skill(
            imported.slug,
            GeneralSkillRunRequest(
                tenant_id="tenant_demo",
                user_id="user_demo",
                query="介绍这个 Skill",
                operation="read",
            ),
            db,
            _admin_user(),
        )
        assert read_result.operation == "read"
        assert read_result.reply == "介绍这个 Skill read"
        assert read_queries == ["介绍这个 Skill"]

        deleted = delete_general_skill(
            imported.slug,
            "tenant_demo",
            db,
            agent_id="agent_overall",
            current_user=_admin_user(),
        )
        assert deleted == {"status": "hidden", "slug": "weather-zh"}
        assert list_general_skills("tenant_demo", db) == []
        try:
            get_general_skill(imported.slug, "tenant_demo", db)
        except HTTPException as error:
            assert error.status_code == 404
        else:
            raise AssertionError("deleted general skill should be gone")


@pytest.mark.anyio
async def test_general_skill_stream_executes_through_forced_harness_v2(monkeypatch) -> None:
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(test_engine)
    captured_requests = []

    def fail_legacy_runner(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("legacy GeneralSkillRunner must not handle skill tests")

    def fake_handle_turn(self, request):  # noqa: ANN001
        captured_requests.append(request)
        self.db.add(
            AgentEvent(
                tenant_id=request.tenant_id,
                session_id=request.session_id,
                event_type="general_skill_trace",
                payload_json={
                    "phase": "instructions_loaded",
                    "text": "已加载技能说明",
                },
            )
        )
        self.db.add(
            Message(
                tenant_id=request.tenant_id,
                session_id=request.session_id,
                role="assistant",
                content="北京天气晴朗",
                metadata_json={"harness_artifacts": [{"path": "weather.txt"}]},
            )
        )
        self.db.commit()
        return ChatTurnResponse(
            reply="北京天气晴朗",
            session_id=request.session_id,
            session_state={
                "session_id": request.session_id,
                "tenant_id": request.tenant_id,
                "status": "active",
            },
        )

    general_skills_module = sys.modules[run_general_skill.__module__]
    monkeypatch.setattr(general_skills_module, "engine", test_engine)
    monkeypatch.setattr(general_skills_module.AgentLoop, "handle_turn", fake_handle_turn)
    monkeypatch.setattr(GeneralSkillRunner, "run", fail_legacy_runner)

    with Session(test_engine) as db:
        _seed_minimal_tenant(db)
        db.add(
            AgentProfile(
                id="agent_overall",
                tenant_id="tenant_demo",
                name="开放广场",
                is_overall=True,
            )
        )
        db.commit()
        imported = import_general_skill(
            GeneralSkillImportRequest(
                tenant_id="tenant_demo",
                name="天气",
                slug="weather-harness",
                markdown=WEATHER_SKILL_MD,
                status="published",
            ),
            db,
            _admin_user(),
        )
        response = run_general_skill_stream(
            imported.slug,
            GeneralSkillRunRequest(
                tenant_id="tenant_demo",
                agent_id="agent_overall",
                user_id="user_demo",
                query="北京天气",
            ),
            db,
            _admin_user(),
        )
        chunks = [chunk async for chunk in response.body_iterator]
        body = "".join(
            chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk for chunk in chunks
        )
        debug_session = db.exec(
            select(ChatSession).where(ChatSession.channel == "skill_test")
        ).one()

    assert captured_requests
    assert captured_requests[0].message == "/skill weather-harness 北京天气"
    assert captured_requests[0].message_visibility == "internal"
    assert debug_session.id == captured_requests[0].session_id
    assert '"execution_engine": "harness_v2"' in body
    assert "已加载技能说明" in body
    assert "北京天气晴朗" in body
    assert '"path": "weather.txt"' in body


def test_non_overall_agent_delete_hides_general_skill_only_in_branch() -> None:
    with _test_session() as db:
        _seed_minimal_tenant(db)
        db.add(
            AgentProfile(
                id="agent_overall", tenant_id="tenant_demo", name="整体智能体", is_overall=True
            )
        )
        db.add(
            AgentProfile(
                id="agent_branch", tenant_id="tenant_demo", name="客服分支", is_overall=False
            )
        )
        imported = import_general_skill(
            GeneralSkillImportRequest(
                tenant_id="tenant_demo",
                name="天气",
                slug="weather-zh",
                markdown=WEATHER_SKILL_MD,
            ),
            db,
            _admin_user(),
        )
        db.commit()

        deleted = delete_general_skill(
            imported.slug,
            "tenant_demo",
            db,
            agent_id="agent_branch",
            current_user=_admin_user(),
        )

        assert deleted == {"status": "hidden", "slug": "weather-zh"}
        assert get_general_skill(imported.slug, "tenant_demo", db).slug == "weather-zh"
        assert list_general_skills("tenant_demo", db, agent_id="agent_branch") == []
        assert (
            list_general_skills("tenant_demo", db, agent_id="agent_overall")[0].slug == "weather-zh"
        )


def test_member_can_import_open_gallery_skill_and_see_it_in_list() -> None:
    with _test_session() as db:
        _seed_minimal_tenant(db)
        db.add(
            AgentProfile(
                id="agent_overall", tenant_id="tenant_demo", name="整体智能体", is_overall=True
            )
        )
        db.commit()

        member = User(
            id="user_member",
            tenant_id="tenant_demo",
            username="member",
            password_hash="x",
        )
        imported = import_general_skill(
            GeneralSkillImportRequest(
                tenant_id="tenant_demo",
                name="成员天气技能",
                slug="member-weather",
                markdown=WEATHER_SKILL_MD,
            ),
            db,
            member,
        )
        assert imported.metadata["owner_user_id"] == "user_member"

        listed = list_general_skills("tenant_demo", db)
        assert [row.slug for row in listed] == ["member-weather"]


def test_member_cannot_delete_others_gallery_skill_but_can_delete_own() -> None:
    with _test_session() as db:
        _seed_minimal_tenant(db)
        db.add(
            AgentProfile(
                id="agent_overall", tenant_id="tenant_demo", name="整体智能体", is_overall=True
            )
        )
        db.commit()

        author = User(
            id="user_author", tenant_id="tenant_demo", username="author", password_hash="x"
        )
        other = User(
            id="user_other_member",
            tenant_id="tenant_demo",
            username="other_member",
            password_hash="x",
        )

        own = import_general_skill(
            GeneralSkillImportRequest(
                tenant_id="tenant_demo",
                name="作者天气技能",
                slug="author-weather",
                markdown=WEATHER_SKILL_MD,
            ),
            db,
            author,
        )

        with pytest.raises(HTTPException) as delete_error:
            delete_general_skill(own.slug, "tenant_demo", db, current_user=other)
        assert delete_error.value.status_code == 403

        deleted = delete_general_skill(own.slug, "tenant_demo", db, current_user=author)
        assert deleted == {"status": "hidden", "slug": "author-weather"}
        assert list_general_skills("tenant_demo", db) == []


def test_admin_can_delete_any_gallery_skill_without_agent_id() -> None:
    """Regression for the delete ordering defect: an admin deleting a gallery skill
    without an overall agent_id was previously blocked by require_overall_agent even
    though the administrator is allowed to remove global resources."""
    with _test_session() as db:
        _seed_minimal_tenant(db)
        db.add(
            AgentProfile(
                id="agent_overall", tenant_id="tenant_demo", name="整体智能体", is_overall=True
            )
        )
        db.commit()

        author = User(
            id="user_author", tenant_id="tenant_demo", username="author", password_hash="x"
        )
        imported = import_general_skill(
            GeneralSkillImportRequest(
                tenant_id="tenant_demo",
                name="作者天气技能",
                slug="author-weather",
                markdown=WEATHER_SKILL_MD,
            ),
            db,
            author,
        )

        deleted = delete_general_skill(
            imported.slug, "tenant_demo", db, current_user=_admin_user()
        )
        assert deleted == {"status": "hidden", "slug": "author-weather"}
        assert list_general_skills("tenant_demo", db) == []


def test_member_cannot_overwrite_others_gallery_skill_via_original_slug() -> None:
    with _test_session() as db:
        _seed_minimal_tenant(db)
        db.add(
            AgentProfile(
                id="agent_overall", tenant_id="tenant_demo", name="整体智能体", is_overall=True
            )
        )
        db.commit()

        author = User(
            id="user_author", tenant_id="tenant_demo", username="author", password_hash="x"
        )
        other = User(
            id="user_other_member",
            tenant_id="tenant_demo",
            username="other_member",
            password_hash="x",
        )

        import_general_skill(
            GeneralSkillImportRequest(
                tenant_id="tenant_demo",
                name="作者天气技能",
                slug="author-weather",
                markdown=WEATHER_SKILL_MD,
            ),
            db,
            author,
        )

        with pytest.raises(HTTPException) as overwrite_error:
            import_general_skill(
                GeneralSkillImportRequest(
                    tenant_id="tenant_demo",
                    original_slug="author-weather",
                    slug="author-weather",
                    name="被恶意覆盖",
                    markdown="# 被恶意覆盖\n",
                ),
                db,
                other,
            )
        assert overwrite_error.value.status_code == 403

        survived = db.exec(
            select(GeneralSkill).where(
                GeneralSkill.tenant_id == "tenant_demo",
                GeneralSkill.slug == "author-weather",
            )
        ).first()
        assert survived is not None
        assert survived.name == "作者天气技能"


def test_member_can_overwrite_own_gallery_skill_via_original_slug() -> None:
    with _test_session() as db:
        _seed_minimal_tenant(db)
        db.add(
            AgentProfile(
                id="agent_overall", tenant_id="tenant_demo", name="整体智能体", is_overall=True
            )
        )
        db.commit()

        author = User(
            id="user_author", tenant_id="tenant_demo", username="author", password_hash="x"
        )

        import_general_skill(
            GeneralSkillImportRequest(
                tenant_id="tenant_demo",
                name="作者天气技能",
                slug="author-weather",
                markdown=WEATHER_SKILL_MD,
            ),
            db,
            author,
        )
        updated = import_general_skill(
            GeneralSkillImportRequest(
                tenant_id="tenant_demo",
                original_slug="author-weather",
                slug="author-weather",
                name="作者更新版本",
                markdown="# 作者更新版本\n",
            ),
            db,
            author,
        )
        assert updated.name == "作者更新版本"
        assert updated.metadata["owner_user_id"] == "user_author"


def test_member_cannot_publish_others_gallery_skill() -> None:
    with _test_session() as db:
        _seed_minimal_tenant(db)
        db.add(
            AgentProfile(
                id="agent_overall", tenant_id="tenant_demo", name="整体智能体", is_overall=True
            )
        )
        db.commit()

        author = User(
            id="user_author", tenant_id="tenant_demo", username="author", password_hash="x"
        )
        other = User(
            id="user_other_member",
            tenant_id="tenant_demo",
            username="other_member",
            password_hash="x",
        )

        import_general_skill(
            GeneralSkillImportRequest(
                tenant_id="tenant_demo",
                name="作者天气技能",
                slug="author-weather",
                markdown=WEATHER_SKILL_MD,
            ),
            db,
            author,
        )

        with pytest.raises(HTTPException) as publish_error:
            publish_general_skill("author-weather", "tenant_demo", db, current_user=other)
        assert publish_error.value.status_code == 403


def test_member_can_publish_own_and_admin_can_publish_others() -> None:
    with _test_session() as db:
        _seed_minimal_tenant(db)
        db.add(
            AgentProfile(
                id="agent_overall", tenant_id="tenant_demo", name="整体智能体", is_overall=True
            )
        )
        db.commit()

        author = User(
            id="user_author", tenant_id="tenant_demo", username="author", password_hash="x"
        )
        other = User(
            id="user_other_member",
            tenant_id="tenant_demo",
            username="other_member",
            password_hash="x",
        )
        admin = _admin_user()

        import_general_skill(
            GeneralSkillImportRequest(
                tenant_id="tenant_demo",
                name="作者天气技能",
                slug="author-weather",
                markdown=WEATHER_SKILL_MD,
            ),
            db,
            author,
        )
        import_general_skill(
            GeneralSkillImportRequest(
                tenant_id="tenant_demo",
                name="他人天气技能",
                slug="other-weather",
                markdown=WEATHER_SKILL_MD,
            ),
            db,
            other,
        )

        own_published = publish_general_skill(
            "other-weather", "tenant_demo", db, current_user=other
        )
        assert own_published.status == "published"

        admin_published = publish_general_skill(
            "author-weather", "tenant_demo", db, current_user=admin
        )
        assert admin_published.status == "published"


def test_scene_layer_prompt_contract_mentions_general_skill_tools() -> None:
    prompt_dir = Path(__file__).resolve().parents[1] / "app" / "llm" / "prompts"

    router_prompt = (prompt_dir / "router_prompt.md").read_text(encoding="utf-8")
    step_prompt = (prompt_dir / "step_agent_general_skill_rules.md").read_text(encoding="utf-8")
    reflection_prompt = (prompt_dir / "reflection_prompt.md").read_text(encoding="utf-8")

    assert "Router 只决定场景化技能和任务执行顺序" in router_prompt
    assert "通用技能是场景内第二层能力" in step_prompt
    assert "target_tool_name 指向该通用技能工具" in reflection_prompt


def test_general_skill_runner_repairs_failed_code(monkeypatch) -> None:
    calls: list[str] = []

    def fake_init(self, model_config):  # noqa: ANN001
        return None

    def fake_generate_json(self, system_prompt, payload):  # noqa: ANN001
        prompt_text = _system_and_stage_instructions(system_prompt, payload)
        if "代码修复器" in prompt_text:
            calls.append("repair")
            return {
                "code": (
                    "import json\n"
                    "payload=json.loads(input())\n"
                    "print(json.dumps({'success': True, 'city': '北京', 'weather': '晴', 'query': payload['query']}, ensure_ascii=False))\n"
                ),
                "rationale": "修复失败输出",
            }
        if "通用技能执行器" in prompt_text:
            calls.append("runner")
            return {
                "code": "import json\nprint(json.dumps({'success': False, 'error': 'first_fail'}, ensure_ascii=False))\n",
                "rationale": "首次尝试失败",
            }
        if "通用技能结果回复器" in prompt_text:
            calls.append("reply")
            assert payload["structured_result"]["success"] is True
            return {"reply": "北京今天晴。"}
        raise AssertionError("unexpected prompt")

    monkeypatch.setattr(LLMClient, "__init__", fake_init)
    monkeypatch.setattr(LLMClient, "generate_json", fake_generate_json)

    skill = GeneralSkill(
        tenant_id="tenant_demo",
        slug="weather-zh",
        name="中国城市天气",
        description="中国城市天气查询工具",
        homepage="https://www.weather.com.cn/",
        skill_markdown=WEATHER_SKILL_MD,
        status="published",
    )
    model_config = ModelConfig(
        tenant_id="tenant_demo",
        name="Fake model",
        api_key_encrypted=encrypt_secret("test-key"),
        model="fake",
        is_default=True,
        enabled=True,
    )

    events: list[dict] = []

    response = GeneralSkillRunner().run(
        skill, "北京今天天气怎么样", model_config, max_attempts=2, event_sink=events.append
    )

    assert response.reply == "北京今天晴。"
    assert response.structured_result["success"] is True
    assert calls == ["runner", "repair", "reply"]
    assert any(item["phase"] == "reflection_retrying" for item in response.execution_trace)
    assert any(item["phase"] == "stdout_chunk" and "first_fail" in item["text"] for item in events)


def test_general_skill_runner_materializes_folder_package(monkeypatch) -> None:
    calls: list[str] = []

    def fake_init(self, model_config):  # noqa: ANN001
        return None

    def fake_generate_json(self, system_prompt, payload):  # noqa: ANN001
        prompt_text = _system_and_stage_instructions(system_prompt, payload)
        if "通用技能执行器" in prompt_text:
            calls.append("runner")
            assert payload["skill"]["package"]["file_count"] == 2
            assert [item["path"] for item in payload["skill"]["package"]["files"]] == [
                "SKILL.md",
                "data/city.txt",
            ]
            return {
                "code": (
                    "import json\n"
                    "from pathlib import Path\n"
                    "payload=json.loads(input())\n"
                    "city=(Path(payload['skill_workspace'])/'data'/'city.txt').read_text(encoding='utf-8').strip()\n"
                    "print(json.dumps({'success': True, 'city': city, 'files': payload['skill_files']}, ensure_ascii=False))\n"
                ),
                "rationale": "读取技能目录里的数据文件。",
            }
        if "通用技能运行结果审查器" in prompt_text:
            calls.append("review")
            assert payload["structured_result"]["city"] == "北京"
            return {
                "result_sufficient": True,
                "needs_retry": False,
                "terminal": False,
                "reason": "目录文件已读取成功。",
            }
        if "通用技能结果回复器" in prompt_text:
            calls.append("reply")
            assert payload["structured_result"]["city"] == "北京"
            return {"reply": "已读取目录技能，城市是北京。"}
        raise AssertionError("unexpected prompt")

    monkeypatch.setattr(LLMClient, "__init__", fake_init)
    monkeypatch.setattr(LLMClient, "generate_json", fake_generate_json)

    skill = GeneralSkill(
        tenant_id="tenant_demo",
        slug="folder-weather",
        name="目录天气技能",
        description="读取目录内数据",
        skill_markdown="# 目录天气技能\n读取 data/city.txt。",
        skill_files_json=[
            {"path": "SKILL.md", "content": "# 目录天气技能\n读取 data/city.txt。"},
            {"path": "data/city.txt", "content": "北京"},
        ],
        status="published",
    )
    model_config = ModelConfig(
        tenant_id="tenant_demo",
        name="Fake model",
        api_key_encrypted=encrypt_secret("test-key"),
        model="fake",
        is_default=True,
        enabled=True,
    )

    response = GeneralSkillRunner().run(skill, "查一下目录里的城市", model_config, max_attempts=1)

    assert response.reply == "已读取目录技能，城市是北京。"
    assert response.structured_result["city"] == "北京"
    assert response.structured_result["files"] == ["SKILL.md", "data/city.txt"]
    assert calls == ["runner", "review", "reply"]


def test_general_skill_runner_executes_bash_package_command(monkeypatch) -> None:
    calls: list[str] = []

    def fake_init(self, model_config):  # noqa: ANN001
        return None

    def fake_generate_json(self, system_prompt, payload):  # noqa: ANN001
        prompt_text = _system_and_stage_instructions(system_prompt, payload)
        if "通用技能执行器" in prompt_text:
            calls.append("runner")
            assert payload["skill"]["package"]["file_count"] == 2
            assert payload["runtime"]["languages"] == ["bash", "python"]
            return {
                "runtime": "bash",
                "code": 'set -euo pipefail\ncd "$SKILL_WORKSPACE"\nprintf \'%s\\n\' "$ARGUMENTS" | python3 scripts/weather.py\n',
                "rationale": "技能声明 allowed-tools: Bash，并给出了调用 scripts/weather.py 的命令。",
            }
        if "通用技能运行结果审查器" in prompt_text:
            calls.append("review")
            assert payload["structured_result"]["city"] == "北京"
            return {
                "result_sufficient": True,
                "needs_retry": False,
                "terminal": False,
                "reason": "Bash 已调用包内脚本并得到结果。",
            }
        if "通用技能结果回复器" in prompt_text:
            calls.append("reply")
            return {"reply": "北京今天晴。"}
        raise AssertionError("unexpected prompt")

    monkeypatch.setattr(LLMClient, "__init__", fake_init)
    monkeypatch.setattr(LLMClient, "generate_json", fake_generate_json)

    skill = GeneralSkill(
        tenant_id="tenant_demo",
        slug="weather-zh",
        name="中国城市天气",
        description="中国城市天气查询工具",
        skill_markdown=(
            "---\n"
            "allowed-tools: Bash\n"
            "---\n"
            '```bash\nprintf \'%s\\n\' "$ARGUMENTS" | python3 "scripts/weather.py"\n```\n'
        ),
        skill_files_json=[
            {
                "path": "SKILL.md",
                "content": '---\nallowed-tools: Bash\n---\n```bash\nprintf \'%s\\n\' "$ARGUMENTS" | python3 "scripts/weather.py"\n```\n',
            },
            {
                "path": "scripts/weather.py",
                "content": (
                    "import json, sys\n"
                    "query=sys.stdin.read().strip()\n"
                    "print(json.dumps({'success': True, 'city': '北京', 'query': query}, ensure_ascii=False))\n"
                ),
            },
        ],
        status="published",
    )
    model_config = ModelConfig(
        tenant_id="tenant_demo",
        name="Fake model",
        api_key_encrypted=encrypt_secret("test-key"),
        model="fake",
        is_default=True,
        enabled=True,
    )

    response = GeneralSkillRunner().run(skill, "北京今天天气怎么样", model_config, max_attempts=1)

    assert response.reply == "北京今天晴。"
    assert response.structured_result["city"] == "北京"
    assert calls == ["runner", "review", "reply"]
    plan_events = [item for item in response.execution_trace if item["phase"] == "plan_created"]
    assert plan_events[0]["runtime"] == "bash"


def test_general_skill_runner_has_requests_in_runtime(monkeypatch) -> None:
    calls: list[str] = []

    def fake_init(self, model_config):  # noqa: ANN001
        return None

    def fake_generate_json(self, system_prompt, payload):  # noqa: ANN001
        prompt_text = _system_and_stage_instructions(system_prompt, payload)
        if "通用技能执行器" in prompt_text:
            calls.append("runner")
            return {
                "runtime": "python",
                "code": (
                    "import json\n"
                    "import requests\n"
                    "payload=json.loads(input())\n"
                    "print(json.dumps({"
                    "'success': True, "
                    "'query': payload['query'], "
                    "'requests_available': bool(requests.__version__)"
                    "}, ensure_ascii=False))\n"
                ),
                "rationale": "验证通用技能运行环境包含 requests。",
            }
        if "通用技能运行结果审查器" in prompt_text:
            calls.append("review")
            assert payload["structured_result"]["requests_available"] is True
            return {
                "result_sufficient": True,
                "needs_retry": False,
                "terminal": False,
                "reason": "requests 可用。",
            }
        if "通用技能结果回复器" in prompt_text:
            calls.append("reply")
            return {"reply": "requests 可用。"}
        raise AssertionError("unexpected prompt")

    monkeypatch.setattr(LLMClient, "__init__", fake_init)
    monkeypatch.setattr(LLMClient, "generate_json", fake_generate_json)

    skill = GeneralSkill(
        tenant_id="tenant_demo",
        slug="runtime-check",
        name="运行环境检查",
        description="检查基础库",
        skill_markdown="# 运行环境检查\n需要 requests。",
        status="published",
    )
    model_config = ModelConfig(
        tenant_id="tenant_demo",
        name="Fake model",
        api_key_encrypted=encrypt_secret("test-key"),
        model="fake",
        is_default=True,
        enabled=True,
    )

    response = GeneralSkillRunner().run(skill, "检查 requests", model_config, max_attempts=1)

    assert response.reply == "requests 可用。"
    assert response.structured_result["requests_available"] is True
    assert calls == ["runner", "review", "reply"]


def test_general_skill_prompt_rejects_unlisted_external_apis() -> None:
    prompt = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "llm"
        / "prompts"
        / "general_skill_runner_prompt.md"
    ).read_text(encoding="utf-8")

    assert "不要自行发明第三方接口" in prompt
    assert "runtime=`bash`" in prompt


def test_general_skill_repair_prompt_uses_artifact_directory_contract() -> None:
    prompt = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "llm"
        / "prompts"
        / "general_skill_repair_prompt.md"
    ).read_text(encoding="utf-8")

    assert "`ARTIFACT_DIR`" in prompt
    assert "artifact_dir" in prompt
    assert "`OUTPUT_DIR`" not in prompt
    assert "output_dir" not in prompt


def test_general_skill_runner_reflects_failed_initial_plan(monkeypatch) -> None:
    calls: list[str] = []

    def fake_init(self, model_config):  # noqa: ANN001
        return None

    def fake_generate_json(self, system_prompt, payload):  # noqa: ANN001
        prompt_text = _system_and_stage_instructions(system_prompt, payload)
        if "代码修复器" in prompt_text:
            calls.append("repair")
            assert (
                payload["previous_attempts"][0]["structured_result"]["error"]
                == "plan_generation_failed"
            )
            return {
                "code": (
                    "import json\n"
                    "payload=json.loads(input())\n"
                    "print(json.dumps({'success': True, 'city': '廊坊', 'weather': '多云', 'query': payload['query']}, ensure_ascii=False))\n"
                ),
                "rationale": "重新输出合法 runner JSON",
            }
        if "通用技能执行器" in prompt_text:
            calls.append("runner_failed")
            raise LLMError("Model did not return valid JSON after retry")
        if "通用技能结果回复器" in prompt_text:
            calls.append("reply")
            assert payload["structured_result"]["success"] is True
            return {"reply": "廊坊今天多云。"}
        raise AssertionError("unexpected prompt")

    monkeypatch.setattr(LLMClient, "__init__", fake_init)
    monkeypatch.setattr(LLMClient, "generate_json", fake_generate_json)

    skill = GeneralSkill(
        tenant_id="tenant_demo",
        slug="weather-zh",
        name="中国城市天气",
        description="中国城市天气查询工具",
        homepage="https://www.weather.com.cn/",
        skill_markdown=WEATHER_SKILL_MD,
        status="published",
    )
    model_config = ModelConfig(
        tenant_id="tenant_demo",
        name="Fake model",
        api_key_encrypted=encrypt_secret("test-key"),
        model="fake",
        is_default=True,
        enabled=True,
    )

    response = GeneralSkillRunner().run(skill, "廊坊天气", model_config, max_attempts=2)

    assert response.reply == "廊坊今天多云。"
    assert response.structured_result["success"] is True
    assert calls == ["runner_failed", "repair", "reply"]
    assert any(item["phase"] == "plan_failed" for item in response.execution_trace)
    assert any(item["phase"] == "reflection_retrying" for item in response.execution_trace)


def test_general_skill_runner_stops_on_non_retryable_failure(monkeypatch) -> None:
    calls: list[str] = []

    def fake_init(self, model_config):  # noqa: ANN001
        return None

    def fake_generate_json(self, system_prompt, payload):  # noqa: ANN001
        prompt_text = _system_and_stage_instructions(system_prompt, payload)
        if "通用技能执行器" in prompt_text:
            calls.append("runner")
            return {
                "code": (
                    "import json\n"
                    "print(json.dumps({"
                    "'success': False, "
                    "'error': 'source_unavailable', "
                    "'message': '天气源不可用', "
                    "'attempted_urls': ['https://example.invalid/weather'], "
                    "'exception_type': 'TimeoutError', "
                    "'exception_message': 'timed out', "
                    "'retryable': False"
                    "}, ensure_ascii=False))\n"
                ),
                "rationale": "返回不可自动修复的失败",
            }
        if "代码修复器" in prompt_text:
            calls.append("repair")
            raise AssertionError("non-retryable failure should not call repair")
        if "通用技能运行结果审查器" in prompt_text:
            calls.append("review")
            return {
                "result_sufficient": False,
                "needs_retry": True,
                "terminal": False,
                "reason": "模型错误地建议重试",
            }
        if "通用技能结果回复器" in prompt_text:
            calls.append("reply")
            assert payload["structured_result"]["retryable"] is False
            return {"reply": "当前天气源不可用，建议稍后再试。"}
        raise AssertionError("unexpected prompt")

    monkeypatch.setattr(LLMClient, "__init__", fake_init)
    monkeypatch.setattr(LLMClient, "generate_json", fake_generate_json)

    skill = GeneralSkill(
        tenant_id="tenant_demo",
        slug="weather-zh",
        name="中国城市天气",
        description="中国城市天气查询工具",
        homepage="https://www.weather.com.cn/",
        skill_markdown=WEATHER_SKILL_MD,
        status="published",
    )
    model_config = ModelConfig(
        tenant_id="tenant_demo",
        name="Fake model",
        api_key_encrypted=encrypt_secret("test-key"),
        model="fake",
        is_default=True,
        enabled=True,
    )

    response = GeneralSkillRunner().run(skill, "北京今天天气怎么样", model_config, max_attempts=10)

    assert response.reply == "当前天气源不可用，建议稍后再试。"
    assert calls == ["runner", "review", "reply"]
    assert any(item["phase"] == "reflection_stopped" for item in response.execution_trace)


def test_list_my_general_skills_cross_scope_only_mine() -> None:
    """「我创建的技能」跨作用域（广场 + 挂在员工下）列出，且只列自己创建的。"""
    with _test_session() as db:
        _seed_minimal_tenant(db)
        db.add(
            AgentProfile(
                id="agent_overall", tenant_id="tenant_demo", name="整体智能体", is_overall=True
            )
        )
        db.add(
            AgentProfile(
                id="agent_private", tenant_id="tenant_demo", name="私有员工", is_overall=False
            )
        )
        db.commit()

        user_a = User(id="user_a", tenant_id="tenant_demo", username="a", password_hash="x")
        user_b = User(id="user_b", tenant_id="tenant_demo", username="b", password_hash="x")

        a_gallery = GeneralSkill(
            id="genskill_a_gallery",
            tenant_id="tenant_demo",
            slug="a-gallery",
            name="A 的广场技能",
            skill_markdown="# A 广场\n",
            status="published",
            metadata_json={"owner_user_id": "user_a"},
        )
        a_private = GeneralSkill(
            id="genskill_a_private",
            tenant_id="tenant_demo",
            slug="a-private",
            name="A 的私有技能",
            skill_markdown="# A 私有\n",
            status="published",
            metadata_json={"owner_user_id": "user_a", "owner_agent_id": "agent_private"},
        )
        b_gallery = GeneralSkill(
            id="genskill_b_gallery",
            tenant_id="tenant_demo",
            slug="b-gallery",
            name="B 的广场技能",
            skill_markdown="# B 广场\n",
            status="published",
            metadata_json={"owner_user_id": "user_b"},
        )
        db.add(a_gallery)
        db.add(a_private)
        db.add(b_gallery)
        db.add(
            AgentResourceBinding(
                tenant_id="tenant_demo",
                agent_id="agent_private",
                resource_type="general_skill",
                resource_id=a_private.id,
                status="active",
            )
        )
        db.commit()

        mine_a = _list_my_general_skills(db, "tenant_demo", user_a)
        assert {row.slug for row in mine_a} == {"a-gallery", "a-private"}

        mine_b = _list_my_general_skills(db, "tenant_demo", user_b)
        assert {row.slug for row in mine_b} == {"b-gallery"}


def test_list_my_general_skills_private_status_mapping() -> None:
    """挂在员工下的技能：绑定 inactive → 返回 archived；active 且 published → published。"""
    with _test_session() as db:
        _seed_minimal_tenant(db)
        db.add(
            AgentProfile(
                id="agent_overall", tenant_id="tenant_demo", name="整体智能体", is_overall=True
            )
        )
        db.add(
            AgentProfile(
                id="agent_private", tenant_id="tenant_demo", name="私有员工", is_overall=False
            )
        )
        db.commit()

        user_a = User(id="user_a", tenant_id="tenant_demo", username="a", password_hash="x")

        active_skill = GeneralSkill(
            id="genskill_active",
            tenant_id="tenant_demo",
            slug="a-active",
            name="A 活跃私有",
            skill_markdown="# active\n",
            status="published",
            metadata_json={"owner_user_id": "user_a", "owner_agent_id": "agent_private"},
        )
        inactive_skill = GeneralSkill(
            id="genskill_inactive",
            tenant_id="tenant_demo",
            slug="a-inactive",
            name="A 停用私有",
            skill_markdown="# inactive\n",
            status="published",
            metadata_json={"owner_user_id": "user_a", "owner_agent_id": "agent_private"},
        )
        db.add(active_skill)
        db.add(inactive_skill)
        db.add(
            AgentResourceBinding(
                tenant_id="tenant_demo",
                agent_id="agent_private",
                resource_type="general_skill",
                resource_id=active_skill.id,
                status="active",
            )
        )
        db.add(
            AgentResourceBinding(
                tenant_id="tenant_demo",
                agent_id="agent_private",
                resource_type="general_skill",
                resource_id=inactive_skill.id,
                status="inactive",
            )
        )
        db.commit()

        mine = _list_my_general_skills(db, "tenant_demo", user_a)
        by_slug = {row.slug: row.status for row in mine}
        assert by_slug["a-active"] == "published"
        assert by_slug["a-inactive"] == "archived"


def test_list_my_general_skills_private_deleted_excluded() -> None:
    """宿主员工绑定 status=deleted 的私有技能不出现在维护列表里。"""
    with _test_session() as db:
        _seed_minimal_tenant(db)
        db.add(
            AgentProfile(
                id="agent_private", tenant_id="tenant_demo", name="私有员工", is_overall=False
            )
        )
        db.commit()

        user_a = User(id="user_a", tenant_id="tenant_demo", username="a", password_hash="x")
        deleted_skill = GeneralSkill(
            id="genskill_deleted",
            tenant_id="tenant_demo",
            slug="a-deleted",
            name="A 被移除私有",
            skill_markdown="# deleted\n",
            status="published",
            metadata_json={"owner_user_id": "user_a", "owner_agent_id": "agent_private"},
        )
        db.add(deleted_skill)
        db.add(
            AgentResourceBinding(
                tenant_id="tenant_demo",
                agent_id="agent_private",
                resource_type="general_skill",
                resource_id=deleted_skill.id,
                status="deleted",
            )
        )
        db.commit()

        assert _list_my_general_skills(db, "tenant_demo", user_a) == []


def test_list_my_general_skills_gallery_ghost_excluded() -> None:
    """广场技能在 overall 员工上的绑定 status=deleted（用户删过的）→ 不回显成幽灵行。"""
    with _test_session() as db:
        _seed_minimal_tenant(db)
        db.add(
            AgentProfile(
                id="agent_overall", tenant_id="tenant_demo", name="整体智能体", is_overall=True
            )
        )
        db.commit()

        user_a = User(id="user_a", tenant_id="tenant_demo", username="a", password_hash="x")
        ghost = GeneralSkill(
            id="genskill_ghost",
            tenant_id="tenant_demo",
            slug="a-ghost",
            name="A 已删广场技能",
            skill_markdown="# ghost\n",
            status="published",
            metadata_json={"owner_user_id": "user_a"},
        )
        visible = GeneralSkill(
            id="genskill_visible",
            tenant_id="tenant_demo",
            slug="a-visible",
            name="A 可见广场技能",
            skill_markdown="# visible\n",
            status="published",
            metadata_json={"owner_user_id": "user_a"},
        )
        db.add(ghost)
        db.add(visible)
        db.add(
            AgentResourceBinding(
                tenant_id="tenant_demo",
                agent_id="agent_overall",
                resource_type="general_skill",
                resource_id=ghost.id,
                status="deleted",
            )
        )
        db.add(
            AgentResourceBinding(
                tenant_id="tenant_demo",
                agent_id="agent_overall",
                resource_type="general_skill",
                resource_id=visible.id,
                status="active",
            )
        )
        db.commit()

        mine = _list_my_general_skills(db, "tenant_demo", user_a)
        assert {row.slug for row in mine} == {"a-visible"}


def test_list_my_general_skills_requires_auth() -> None:
    """mine=1 且未登录（current_user=None）必须 401。"""
    with _test_session() as db:
        _seed_minimal_tenant(db)
        with pytest.raises(HTTPException) as auth_error:
            list_general_skills("tenant_demo", db, mine=True, current_user=None)
        assert auth_error.value.status_code == 401


def _seed_minimal_tenant(db: Session) -> None:
    db.add(Tenant(id="tenant_demo", name="Demo"))
    db.add(
        User(
            id="user_demo",
            tenant_id="tenant_demo",
            username="user_demo",
            password_hash=hash_password("demo"),
        )
    )
    db.add(
        ModelConfig(
            tenant_id="tenant_demo",
            name="Fake model",
            api_key_encrypted=encrypt_secret("test-key"),
            model="fake",
            is_default=True,
            enabled=True,
        )
    )
    db.commit()


def _purchase_scene_skill() -> Skill:
    return Skill(
        tenant_id="tenant_demo",
        skill_id="purchase",
        name="购买商品流程",
        description="帮助用户购买商品。",
        status="published",
        content_json={
            "business_domain": "commerce",
            "trigger_intents": ["购买", "下单"],
            "required_info": ["product_id"],
            "steps": [
                {
                    "step_id": "collect_product",
                    "name": "收集商品信息",
                    "instruction": "收集用户想购买的商品。",
                    "expected_user_info": ["product_id"],
                    "allowed_actions": ["ask_user", "continue_flow"],
                }
            ],
        },
    )


def _test_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def test_download_general_skill_package_returns_zip_with_files_and_directories() -> None:
    """下载端点：返回 application/zip，包含文件与显式目录项，响应头带中文名的 RFC 5987 编码。"""
    with _test_session() as db:
        _seed_minimal_tenant(db)
        # is_open_gallery_resource 以 overall agent 的绑定为准，没有它广场技能一律不可见
        db.add(
            AgentProfile(
                id="agent_overall", tenant_id="tenant_demo", name="整体智能体", is_overall=True
            )
        )
        db.commit()
        imported = import_general_skill(
            GeneralSkillImportRequest(
                tenant_id="tenant_demo",
                name="目录技能包",
                slug="package-skill",
                files=[
                    {"path": "SKILL.md", "content": "# 目录技能包\n\n见 references/a.md\n"},
                    {"path": "references/a.md", "content": "参考内容\n"},
                ],
                directories=["assets"],
            ),
            db,
            _admin_user(),
        )

        response = download_general_skill_package(imported.slug, "tenant_demo", None, db)

        assert response.media_type == "application/zip"
        disposition = response.headers["content-disposition"]
        # slug 走 filename= 兜底（纯 ASCII，不会炸 latin-1 头），真实中文名走 filename*=
        assert 'filename="package-skill.zip"' in disposition
        assert "filename*=UTF-8''" in disposition
        assert quote("目录技能包.zip", safe="") in disposition

        with ZipFile(BytesIO(response.body)) as archive:
            names = archive.namelist()
            assert "SKILL.md" in names
            assert "references/a.md" in names
            # 空目录无法从文件列表推导，必须显式写成目录项
            assert "assets/" in names
            assert archive.read("SKILL.md").decode("utf-8") == imported.skill_markdown
            assert archive.read("references/a.md").decode("utf-8") == "参考内容\n"


def test_download_general_skill_package_is_reimportable() -> None:
    """导出的 zip 与导入格式同构：解出来的文件集合与入库时逐字节一致。"""
    with _test_session() as db:
        _seed_minimal_tenant(db)
        db.add(
            AgentProfile(
                id="agent_overall", tenant_id="tenant_demo", name="整体智能体", is_overall=True
            )
        )
        db.commit()
        imported = import_general_skill(
            GeneralSkillImportRequest(
                tenant_id="tenant_demo",
                name="回灌技能",
                slug="roundtrip-skill",
                files=[
                    {"path": "SKILL.md", "content": "# 回灌技能\n\n见 references/b.md\n"},
                    {"path": "references/b.md", "content": "B\n"},
                    {"path": "scripts/run.py", "content": "print('hi')\n"},
                ],
            ),
            db,
            _admin_user(),
        )

        stored = db.get(GeneralSkill, imported.id)
        assert stored is not None
        roundtripped = _files_from_zip(_general_skill_package_archive(stored))

        assert {file.path: file.content for file in roundtripped} == {
            file.path: file.content for file in imported.skill_files
        }


def test_download_general_skill_package_hides_invisible_skill() -> None:
    """私有技能不带 agent_id 下载 → 404；不存在的 slug → 404。"""
    with _test_session() as db:
        _seed_minimal_tenant(db)
        # 特意把 overall agent 也建出来：这样 404 只可能来自「私有技能不进广场」，
        # 而不是「租户压根没有广场」这条更弱的路径。
        db.add(
            AgentProfile(
                id="agent_overall", tenant_id="tenant_demo", name="整体智能体", is_overall=True
            )
        )
        db.add(
            AgentProfile(
                id="agent_private", tenant_id="tenant_demo", name="私有员工", is_overall=False
            )
        )
        db.commit()
        private = import_general_skill(
            GeneralSkillImportRequest(
                tenant_id="tenant_demo",
                name="私有技能",
                slug="private-skill",
                markdown="# 私有技能\n",
                agent_id="agent_private",
            ),
            db,
            _admin_user(),
        )

        with pytest.raises(HTTPException) as invisible:
            download_general_skill_package(private.slug, "tenant_demo", None, db)
        assert invisible.value.status_code == 404
        assert "not visible in open gallery" in str(invisible.value.detail)

        with pytest.raises(HTTPException) as missing:
            download_general_skill_package("no-such-skill", "tenant_demo", None, db)
        assert missing.value.status_code == 404
        assert "not found" in str(missing.value.detail)


def test_list_general_skills_include_files_false_strips_package_but_keeps_metadata() -> None:
    """广场列表传 include_files=0：只裁文件包，元信息与可见性一条都不能少。

    这是性能回归的守卫 —— skill_files_json 单租户就有 MB 级体量，广场卡片只用元信息。
    """
    with _test_session() as db:
        _seed_minimal_tenant(db)
        db.add(
            AgentProfile(
                id="agent_overall", tenant_id="tenant_demo", name="整体智能体", is_overall=True
            )
        )
        db.commit()

        imported = import_general_skill(
            GeneralSkillImportRequest(
                tenant_id="tenant_demo",
                name="天气技能",
                slug="weather-zh",
                markdown=WEATHER_SKILL_MD,
            ),
            db,
            _admin_user(),
        )

        full = list_general_skills("tenant_demo", db)
        slim = list_general_skills("tenant_demo", db, include_files=False)

        # 行数 / 顺序 / id / 名称 / 描述必须完全一致：裁剪只能动文件包
        assert [row.id for row in slim] == [row.id for row in full] == [imported.id]
        assert [row.name for row in slim] == [row.name for row in full]
        assert [row.slug for row in slim] == [row.slug for row in full]
        assert slim[0].skill_markdown == full[0].skill_markdown

        # 文件包被裁掉，全量路径仍在
        assert slim[0].skill_files == []
        assert slim[0].skill_directories == []
        assert full[0].skill_files


def test_list_general_skills_include_files_false_also_applies_on_agent_scoped_path() -> None:
    """按 agent_id 走（非 overall 的员工作用域）时也必须尊重 include_files=0。

    回归点：这条分支早期漏传了 include_files，导致「传了裁剪参数仍把 MB 级文件包拉回来」。
    """
    with _test_session() as db:
        _seed_minimal_tenant(db)
        db.add(
            AgentProfile(
                id="agent_overall", tenant_id="tenant_demo", name="整体智能体", is_overall=True
            )
        )
        db.add(
            AgentProfile(
                id="agent_worker", tenant_id="tenant_demo", name="员工", is_overall=False
            )
        )
        db.commit()

        imported = import_general_skill(
            GeneralSkillImportRequest(
                tenant_id="tenant_demo",
                name="天气技能",
                slug="weather-zh",
                markdown=WEATHER_SKILL_MD,
                agent_id="agent_worker",
            ),
            db,
            _admin_user(),
        )

        full = list_general_skills("tenant_demo", db, agent_id="agent_worker")
        slim = list_general_skills(
            "tenant_demo", db, agent_id="agent_worker", include_files=False
        )

        assert [row.id for row in full] == [imported.id]
        assert [row.id for row in slim] == [imported.id]
        assert full[0].skill_files
        assert slim[0].skill_files == []
        assert slim[0].skill_directories == []

