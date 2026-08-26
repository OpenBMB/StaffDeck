from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlmodel import Session

from app.audit_cases.service import AuditCaseService
from app.audit_cases.storage import read_case_blob
from app.core.harness_attachments import _write_workspace_bytes
from app.core.harness_session_cleanup import harness_task_workspace_path
from app.db.models import AuditCase


def _material_kind(filename: str, content_type: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf" or content_type == "application/pdf":
        return "pdf"
    if suffix in {".txt", ".md", ".csv", ".json", ".xml", ".html"} or content_type.startswith(
        "text/"
    ):
        return "text"
    return "binary"


def materialize_audit_case_materials(
    *, db: Session, case: AuditCase, session_id: str, task_frame_id: str
) -> list[dict[str, Any]]:
    """Materialize current project files into one isolated TaskFrame workspace.

    The database retains only controlled storage keys. The Harness receives
    project IDs, hashes, statuses, and ``/workspace`` paths; it never receives
    the host path used by the controlled storage layer.
    """

    workspace = harness_task_workspace_path(
        tenant_id=case.tenant_id,
        session_id=session_id,
        task_frame_id=task_frame_id,
        db=db,
    )
    descriptors: list[dict[str, Any]] = []
    for material in AuditCaseService(db).list_current_materials(case):
        filename = Path(material.filename).name or "material"
        relative = f"audit-case/{material.id}/{filename}"
        descriptor: dict[str, Any] = {
            "source": "audit_case",
            "audit_case_id": case.id,
            "material_id": material.id,
            "attachment_id": material.attachment_id,
            "material_type": material.material_type,
            "filename": material.filename,
            "content_type": material.content_type,
            "kind": _material_kind(material.filename, material.content_type),
            "size": material.size,
            "sha256": material.sha256,
            "version": material.version,
            "status": material.processing_status,
            "extraction_status": material.extraction_status,
            "workspace_path": f"/workspace/{relative}",
            "workspace_relative_path": relative,
            "materialized": False,
        }
        try:
            _write_workspace_bytes(workspace, relative, read_case_blob(material.storage_key))
            descriptor["materialized"] = True
            descriptor["note"] = "审核项目当前版本原文件已写入当前 TaskFrame 沙箱。"
            if material.extracted_text_storage_key:
                extracted_relative = f"audit-case/{material.id}/{filename}.extracted.txt"
                _write_workspace_bytes(
                    workspace,
                    extracted_relative,
                    read_case_blob(material.extracted_text_storage_key),
                )
                descriptor["extracted_text_path"] = f"/workspace/{extracted_relative}"
                descriptor["extracted_text_relative_path"] = extracted_relative
        except (OSError, ValueError) as exc:
            descriptor["error"] = f"项目材料写入失败：{type(exc).__name__}"
        descriptors.append(descriptor)
    return descriptors
