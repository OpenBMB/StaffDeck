"""通用技能产物自动补登:模型未在结果 JSON 声明 artifacts 时,扫描 artifact_dir 兜底。"""

import json
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.core.capability_manifest import (
    CapabilityDescriptor,
    CapabilityManifest,
    general_skill_snapshot_digest,
)
from app.core.harness_capability_invoker import HarnessCapabilityInvoker
from app.db.models import ChatSession, GeneralSkill, ModelConfig, Tenant, User
from app.general_skills.runner import GeneralSkillRunner
from app.general_skills.schema import GeneralSkillExecutionPlan, GeneralSkillRunResponse


def _test_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id="tenant-demo", name="Demo"))
        db.add(
            User(
                id="user-1",
                tenant_id="tenant-demo",
                username="user-1",
                password_hash="x",
            )
        )
        db.commit()
    return engine


def _model_config() -> ModelConfig:
    return ModelConfig(
        id="model-test",
        tenant_id="tenant-demo",
        name="测试模型",
        api_key_encrypted="test",
        model="test-model",
    )


def _chat_session() -> ChatSession:
    return ChatSession(id="session-1", tenant_id="tenant-demo", user_id="user-1")


def _skill_and_invoker(engine, tmp_path: Path, monkeypatch, *, slug: str = "ppt-maker"):
    skill = GeneralSkill(
        id=f"gs-{slug}",
        tenant_id="tenant-demo",
        slug=slug,
        name="PPT 生成",
        description="生成 PPT 文件",
        skill_markdown="# PPT\n",
        status="published",
    )
    descriptor = CapabilityDescriptor(
        capability_id=skill.id,
        name=f"general_skill.{slug}",
        kind="general_skill",
        metadata={
            "slug": skill.slug,
            "content_digest": general_skill_snapshot_digest(skill),
        },
    )
    with Session(engine) as db:
        db.add(skill)
        db.commit()
        invoker = HarnessCapabilityInvoker(
            db,
            tenant_id="tenant-demo",
            session=_chat_session(),
            task_frame_id="task-artifacts",
            model_config=_model_config(),
            manifest=CapabilityManifest(available=[descriptor]),
            active_skill=None,
            active_step_id=None,
            agent_id=None,
        )
        # 先 read 过闸(execute 前置要求)
        read = invoker._invoke_general_skill(
            skill.id, descriptor.metadata, {"query": "做个 PPT", "operation": "read"}
        )
        assert read["success"] is True
        return invoker, skill, descriptor


def _fake_runner_run(tmp_workspace_artifact_dir: str, payload: dict):
    def fake_run(self, skill, query, model_config, user_id, **kwargs):  # noqa: ANN001
        workspace_root = Path(kwargs["workspace_root"])
        artifact_dir = workspace_root / tmp_workspace_artifact_dir
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / "季度汇报.pptx").write_bytes(b"pk-ppt-bytes")
        return GeneralSkillRunResponse(
            skill_slug=skill.slug,
            operation="execute",
            execution_trace=[],
            generated_code="",
            stdout="",
            stderr="",
            structured_result=payload,
            artifacts=list(payload.get("artifacts") or []),
            reply="已生成",
        )

    return fake_run


def test_runner_records_workspace_relative_artifact_dir(tmp_path, monkeypatch) -> None:
    """runner 在 structured 里回写工作区相对 artifact_dir,供 invoker 兜底扫描。"""
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path / "data"))

    def fake_sandboxed_process(*_args, **kwargs):  # noqa: ANN001
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"success": True}).encode(),
            stderr=b"",
            timed_out=False,
        )

    monkeypatch.setattr(
        "app.general_skills.runner.run_sandboxed_process", fake_sandboxed_process
    )
    skill = GeneralSkill(
        tenant_id="tenant-demo",
        slug="demo",
        name="Demo",
        skill_markdown="# Demo",
        status="published",
    )
    plan = GeneralSkillExecutionPlan(runtime="python", code="print(1)")
    workspace = tmp_path / "task-ws"
    _, _, structured = GeneralSkillRunner()._execute_plan(
        skill, "q", plan, "user-1", [], workspace_root=workspace
    )
    artifact_dir = structured.get("artifact_dir") or ""
    assert artifact_dir.startswith("general_skill_")
    assert artifact_dir.endswith("/artifacts")
    assert not artifact_dir.startswith("/")
    # 无 workspace_root(试运行路径)不带该字段
    _, _, structured_no_ws = GeneralSkillRunner()._execute_plan(skill, "q", plan, "user-1", [])
    assert "artifact_dir" not in structured_no_ws


def test_runner_artifact_dir_overrides_model_reported_value(tmp_path, monkeypatch) -> None:
    """模型在输出 JSON 自报 artifact_dir(如共享目录 attachments)不得劫持兜底扫描目录。"""
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path / "data"))

    def fake_sandboxed_process(*_args, **kwargs):  # noqa: ANN001
        # 模型自报共享目录,试图让兜底扫描登记别人的文件
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"success": True, "artifact_dir": "attachments"}).encode(),
            stderr=b"",
            timed_out=False,
        )

    monkeypatch.setattr(
        "app.general_skills.runner.run_sandboxed_process", fake_sandboxed_process
    )
    skill = GeneralSkill(
        tenant_id="tenant-demo",
        slug="demo",
        name="Demo",
        skill_markdown="# Demo",
        status="published",
    )
    plan = GeneralSkillExecutionPlan(runtime="python", code="print(1)")
    workspace = tmp_path / "task-ws"
    _, _, structured = GeneralSkillRunner()._execute_plan(
        skill, "q", plan, "user-1", [], workspace_root=workspace
    )
    # 强制覆盖为本次运行的真实产物目录,模型自报值被丢弃
    assert structured["artifact_dir"].startswith("general_skill_")
    assert structured["artifact_dir"] != "attachments"


def test_undeclared_artifacts_auto_registered_from_artifact_dir(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path / "data"))
    engine = _test_engine()
    with Session(engine):
        invoker, skill, descriptor = _skill_and_invoker(engine, tmp_path, monkeypatch)
        payload = {
            "success": True,
            "artifact_dir": "general_skill_fake/artifacts",
            # 注意:没有 artifacts 声明
        }
        monkeypatch.setattr(
            "app.core.harness_capability_invoker.GeneralSkillRunner.run",
            _fake_runner_run("general_skill_fake/artifacts", payload),
        )
        result = invoker._invoke_general_skill(
            skill.id,
            descriptor.metadata,
            {"query": "做个 PPT", "operation": "execute"},
        )

    assert result["success"] is True
    artifacts = result["artifacts"]
    assert len(artifacts) == 1
    artifact = artifacts[0]
    assert artifact["path"] == "general_skill_fake/artifacts/季度汇报.pptx"
    assert artifact["display_name"] == "季度汇报.pptx"
    assert artifact["size"] == len(b"pk-ppt-bytes")
    assert artifact["sha256"]
    assert artifact["operation"] == "general_skill.execute"
    assert artifact["source"] == f"general_skill.{skill.slug}"


def test_declared_artifacts_skip_auto_scan(tmp_path, monkeypatch) -> None:
    """显式声明存在时不触发兜底扫描(不产生重复产物)。"""
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path / "data"))
    engine = _test_engine()
    with Session(engine):
        invoker, skill, descriptor = _skill_and_invoker(engine, tmp_path, monkeypatch)
        payload = {
            "success": True,
            "artifact_dir": "general_skill_fake/artifacts",
            # runner 归一化后的声明形态:工作区相对路径
            "artifacts": [
                {
                    "path": "general_skill_fake/artifacts/季度汇报.pptx",
                    "display_name": "季度汇报.pptx",
                },
            ],
        }
        monkeypatch.setattr(
            "app.core.harness_capability_invoker.GeneralSkillRunner.run",
            _fake_runner_run("general_skill_fake/artifacts", payload),
        )
        result = invoker._invoke_general_skill(
            skill.id,
            descriptor.metadata,
            {"query": "做个 PPT", "operation": "execute"},
        )

    assert result["success"] is True
    assert len(result["artifacts"]) == 1
    # 声明路径经归一化换算为工作区相对路径
    assert result["artifacts"][0]["path"].endswith("artifacts/季度汇报.pptx")


def test_failed_run_does_not_auto_register(tmp_path, monkeypatch) -> None:
    """失败运行不做兜底补登(半成品文件不应出现在下载区)。"""
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path / "data"))
    engine = _test_engine()
    with Session(engine):
        invoker, skill, descriptor = _skill_and_invoker(engine, tmp_path, monkeypatch)
        payload = {"success": False, "error": "boom", "artifact_dir": "general_skill_fake/artifacts"}
        monkeypatch.setattr(
            "app.core.harness_capability_invoker.GeneralSkillRunner.run",
            _fake_runner_run("general_skill_fake/artifacts", payload),
        )
        result = invoker._invoke_general_skill(
            skill.id,
            descriptor.metadata,
            {"query": "做个 PPT", "operation": "execute"},
        )

    assert result["success"] is False
    assert result["artifacts"] == []


def test_auto_declare_rejects_paths_outside_workspace(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path / "data"))
    engine = _test_engine()
    with Session(engine):
        invoker, _skill, _descriptor = _skill_and_invoker(engine, tmp_path, monkeypatch)
        assert invoker._auto_declare_artifacts({"artifact_dir": "../escape"}) == []
        assert invoker._auto_declare_artifacts({"artifact_dir": ""}) == []
        assert invoker._auto_declare_artifacts({}) == []
        assert invoker._auto_declare_artifacts({"artifact_dir": "not/exist"}) == []


def test_auto_declare_filters_cache_and_intermediate_files(tmp_path, monkeypatch) -> None:
    """缓存/中间文件(点开头、__pycache__、tmp/part/log 后缀)不登记为产出。"""
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path / "data"))
    engine = _test_engine()
    with Session(engine):
        invoker, _skill, _descriptor = _skill_and_invoker(engine, tmp_path, monkeypatch)
        artifact_dir = invoker.workspace_root / "general_skill_x/artifacts"
        (artifact_dir / "__pycache__").mkdir(parents=True)
        (artifact_dir / "report.pptx").write_bytes(b"pk")
        (artifact_dir / "cache.tmp").write_bytes(b"t")
        (artifact_dir / "run.log").write_bytes(b"l")
        (artifact_dir / ".hidden").write_bytes(b"h")
        (artifact_dir / "__pycache__" / "mod.pyc").write_bytes(b"c")
        declared = invoker._auto_declare_artifacts(
            {"artifact_dir": "general_skill_x/artifacts"}
        )
        paths = [item["path"] for item in declared]
        assert paths == ["general_skill_x/artifacts/report.pptx"]
