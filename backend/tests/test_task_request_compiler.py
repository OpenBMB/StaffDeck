from app.core.task_request_compiler import CapabilityManifest, TaskRequestCompiler
from app.db.models import ChatSession
from app.session.session_schema import PlannedTaskFrame


def test_compiler_projects_materialized_attachments_into_manifest() -> None:
    requirement = TaskRequestCompiler().compile(
        PlannedTaskFrame(
            task_id="task-audit",
            kind="conversation",
            user_intent="继续生成审核报告",
            requirements=["读取审核材料"],
        ),
        ChatSession(id="session-1", tenant_id="tenant_demo"),
        None,
        CapabilityManifest(),
        attachments=[
            {
                "attachment_id": "file-1",
                "filename": "审核记录.pdf",
                "sha256": "c" * 64,
                "materialized": True,
                "workspace_path": "/workspace/attachments/file-1.pdf",
            }
        ],
        source_user_message="继续生成",
    )

    assert requirement.material_manifest[0].attachment_id == "file-1"
    assert requirement.material_manifest[0].status == "available"
