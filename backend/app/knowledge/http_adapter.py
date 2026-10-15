"""Host policy and legacy projections for the independent KM HTTP owner."""

from __future__ import annotations

import inspect
import os
from functools import wraps
from typing import Any, get_type_hints
from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit

import httpx
from fastapi import HTTPException, Response
from sqlmodel import select

from app.db.models import AgentProfile, User
from app.knowledge import schema
from app.security.auth import ensure_current_user_tenant
from app.security.permissions import (
    ensure_agent_scope_manager,
    ensure_open_gallery_admin,
    require_agent_scope_viewer,
)


def enabled() -> bool:
    return bool(os.getenv("STAFFDECK_KM_URL"))


class StaffDeckKnowledgeAdapter:
    def __init__(self, db, tenant_id, agent_id=None, user=None, *, write=False):
        if not enabled():
            raise HTTPException(503, "Independent knowledge service is not configured")
        self.db, self.tenant_id = db, tenant_id
        self.user = user or db.get(User, db.info.get("staffdeck_actor_id", ""))
        if self.user is None:
            raise HTTPException(401, "Not authenticated")
        from app.security.tenant import ensure_tenant
        ensure_tenant(db, tenant_id)
        ensure_current_user_tenant(tenant_id, self.user)
        if write:
            if agent_id:
                agent = ensure_agent_scope_manager(db, tenant_id, agent_id, self.user)
            else:
                ensure_open_gallery_admin(tenant_id, self.user)
                agent = None
        else:
            require_agent_scope_viewer(tenant_id, agent_id, self.user, db)
            agent = db.get(AgentProfile, agent_id) if agent_id else None
        if agent is None:
            agent = db.exec(select(AgentProfile).where(
                AgentProfile.tenant_id == tenant_id, AgentProfile.is_overall == True,
            )).first()
        scope_id = agent.id if agent else "gallery"
        self.space_id = f"sd:{tenant_id}:{scope_id}"
        self.space_name = agent.name if agent else "开放广场"
        self.agent_id = agent.id if agent else None
        self.url = os.getenv("STAFFDECK_KM_URL", "").rstrip("/")
        token = os.getenv("STAFFDECK_KM_BEARER", "")
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}

    def call(self, method, path, body=None, *, scoped=True, raw=False, params=None):
        parts = urlsplit(path)
        params = {**dict(parse_qsl(parts.query, keep_blank_values=True)), **(params or {})}
        if scoped:
            params["source_space_id" if parts.path.endswith("/bindings") and method == "POST"
                   else "space_id"] = self.space_id
        path = urlunsplit((parts.scheme, parts.netloc, parts.path, "", parts.fragment))
        try:
            response = httpx.request(
                method, self.url + "/api/km/v1" + path, json=body, params=params,
                headers=self.headers, timeout=120,
            )
        except httpx.HTTPError as exc:
            raise HTTPException(503, "Knowledge service unavailable") from exc
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            if response.status_code == 404 and detail in {"Space not found", "Knowledge binding not found"} and parts.path.startswith("/knowledge-bases/"):
                identifier = parts.path.split("/")[2]
                spaces = httpx.request("GET", self.url + "/api/km/v1/spaces", headers=self.headers, timeout=120)
                exists = False
                if spaces.is_success:
                    for space_row in spaces.json():
                        response_rows = httpx.request("GET", self.url + "/api/km/v1/knowledge-bases", params={"space_id": space_row["id"]}, headers=self.headers, timeout=120)
                        if response_rows.is_success and any(row["id"] == identifier for row in response_rows.json()):
                            exists = True
                            break
                detail = "Knowledge base version not visible" if exists else "Knowledge base not found"
            if response.status_code == 404 and detail == "Resource not found":
                collection = parts.path.strip("/").split("/")[0]
                noun = {"knowledge-bases": "base", "documents": "document", "buckets": "bucket",
                        "chunks": "chunk", "jobs": "ingest job", "suggestions": "discovery"}.get(collection)
                if noun:
                    detail = f"Knowledge {noun} not found"
            raise HTTPException(response.status_code, detail)
        if raw:
            return response
        value = response.json()
        rows = value if isinstance(value, list) else [value]
        for row in rows:
            if isinstance(row, dict) and row.get("tenant_id") not in {None, self.tenant_id}:
                raise HTTPException(503, "Knowledge namespace configuration mismatch")
        return value

    def ensure_space(self):
        spaces = self.call("GET", "/spaces", scoped=False)
        if any(row["id"] == self.space_id for row in spaces):
            return
        self.call("POST", "/spaces", {"id": self.space_id, "name": self.space_name}, scoped=False)

    def resource_bindings(self):
        from app.agents.schema import AgentResourceBindingRead

        self.ensure_space()
        return [AgentResourceBindingRead(
            id=row["id"], tenant_id=self.tenant_id, agent_id=self.agent_id or "gallery",
            resource_type="knowledge_base", resource_id=row["knowledge_base_id"],
            status="deleted" if row["hidden"] else row["branch"]["status"], metadata=row.get("metadata", {}),
            created_at=row["created_at"], updated_at=row["branch"]["updated_at"],
        ) for row in self.call("GET", "/bindings")]

    def visible_versions(self, include_inactive=False):
        from types import SimpleNamespace

        self.ensure_space()
        bindings = {row["knowledge_base_id"]: row for row in self.call("GET", "/bindings")}
        result = {}
        for row in self.call("GET", "/knowledge-bases"):
            branch = bindings[row["id"]]["branch"]
            if not include_inactive and (row["status"] != "active" or branch["status"] != "active"):
                continue
            versions = self.call("GET", f"/knowledge-bases/{row['id']}/versions")
            head = next(version for version in versions if version["id"] == branch["head_version_id"])
            result[row["id"]] = SimpleNamespace(**{
                **head, "capability_scope": row.get("capability_scope", "general"),
                "metadata_json": row.get("metadata", {}),
            })
        return result

    def resource_projection(self, identifier):
        from types import SimpleNamespace

        from staffdeck_harness.contracts.security import ResourceRef

        row = self.call("GET", f"/knowledge-bases/{identifier}")
        bindings = self.call("GET", "/bindings")
        bound = next(item for item in bindings if item["knowledge_base_id"] == identifier)
        overall = self.db.exec(select(AgentProfile).where(
            AgentProfile.tenant_id == self.tenant_id, AgentProfile.is_overall == True,
        )).first()
        gallery_id = f"sd:{self.tenant_id}:{overall.id if overall else 'gallery'}"
        spaces = self.call("GET", "/spaces", scoped=False)
        public = False
        if any(item["id"] == gallery_id for item in spaces):
            public = any(item["knowledge_base_id"] == identifier and not item["hidden"]
                         and item["branch"]["status"] == "active"
                         for item in self.call("GET", "/bindings", scoped=False, params={"space_id": gallery_id}))
        metadata = row.get("metadata", {})
        resource = SimpleNamespace(**{**row, "metadata_json": metadata})
        resource_binding = SimpleNamespace(
            id=bound["id"], tenant_id=self.tenant_id, agent_id=self.agent_id,
            resource_id=identifier, resource_type="knowledge_base", status=bound["branch"]["status"],
            metadata_json={}, created_at=bound["created_at"], updated_at=bound["branch"]["updated_at"],
        )
        ref = ResourceRef("knowledge_base", identifier, self.tenant_id, attributes={
            "owner_user_id": metadata.get("owner_user_id") or metadata.get("owner_id"),
            "status": row["status"], "enabled": row["status"] == "active",
            "binding_status": resource_binding.status,
            "private_to_agent": not public, "open_gallery": public,
            "agent_is_overall": bool(overall and self.agent_id == overall.id),
            "shared_user_ids": tuple(metadata.get("shared_user_ids") or ()),
            "capability_scope": row.get("capability_scope", "general"),
        })
        return resource, resource_binding, ref

    def update_resource_bindings(self, resources):
        self.ensure_space()
        existing = {row.resource_id: row for row in self.resource_bindings()}
        desired = {row.resource_id: row for row in resources}
        for identifier, item in desired.items():
            if identifier not in existing:
                gallery = StaffDeckKnowledgeAdapter(self.db, self.tenant_id, None, self.user)
                gallery.require_base(identifier, "use")
                gallery.call("POST", f"/knowledge-bases/{identifier}/bindings", {
                    "space_id": self.space_id,
                })
            self.call("PUT", f"/knowledge-bases/{identifier}/binding", {
                "status": item.status, "hidden": item.status == "deleted", "metadata": item.metadata,
            })
        for identifier in existing.keys() - desired.keys():
            self.call("PUT", f"/knowledge-bases/{identifier}/binding", {"hidden": True})

    def require_base(self, kb_id, action="view"):
        from app.security.module_policy import require_resource
        from staffdeck_harness.contracts.security import ResourceRef
        self.call("GET", f"/knowledge-bases/{kb_id}")
        require_resource(self.user, ResourceRef(type="knowledge_base", id=kb_id,
                         tenant_id=self.tenant_id), action, module="knowledge", local_checked=True)

    def base_read(self, row):
        full = self.call("GET", f"/knowledge-bases/{row['id']}")
        documents = self.call("GET", f"/knowledge-bases/{row['id']}/documents")
        versions = self.call("GET", f"/knowledge-bases/{row['id']}/versions")
        branch = full["branch"]
        version_map = {item["id"]: item["version"] for item in versions}
        head = next(item for item in versions if item["id"] == branch["head_version_id"])
        employee = self.db.get(AgentProfile, self.agent_id) if self.agent_id else None
        if employee and not employee.is_overall:
            row = {**row, **{field: head[field] for field in ("name", "description", "capability_scope")},
                   "metadata": {**row.get("metadata", {}), **head.get("metadata", {})},
                   "status": "active" if branch["status"] == "active" else "archived"}
        metadata = dict(row.get("metadata", {}))
        for key in ("owner_id", "owner_name", "published_version_id"):
            metadata.pop(key, None)
        return schema.KnowledgeBaseRead.model_validate({
            **row, "capability_scope": row.get("capability_scope", "general"),
            "metadata": metadata,
            "version": version_map.get(branch["head_version_id"]),
            "branch_head_version": version_map.get(branch["head_version_id"]),
            "branch_base_version": version_map.get(branch["base_version_id"]),
            "branch_sync_state": "diverged" if branch["sync_state"] == "modified" else branch["sync_state"],
            "document_count": len(documents),
            "bucket_count": sum(item["bucket_count"] for item in documents),
            "chunk_count": sum(item["chunk_count"] for item in documents),
        })

    def handle(self, name: str, values: dict[str, Any]):
        request = values.get("request")
        kb_id = values.get("knowledge_base_id")
        if name == "import_okf_bundle":
            import base64
            from knowledge_core.okf import parse_okf_bundle
            try:
                parse_okf_bundle(request.filename, base64.b64decode(request.content_base64))
            except Exception as exc:
                raise HTTPException(400, f"OKF import failed: {exc}") from exc
        if name == "list_knowledge_bases":
            self.ensure_space()
            rows = self.call("GET", "/knowledge-bases")
            return [self.base_read(row) for row in sorted(rows, key=lambda row: row["updated_at"], reverse=True)]
        if name == "create_knowledge_base":
            self.ensure_space()
            from app.agents.branching import agent_private_metadata, open_gallery_metadata, user_creator_metadata
            employee = self.db.get(AgentProfile, self.agent_id) if self.agent_id else None
            metadata = user_creator_metadata(self.user, request.metadata)
            metadata = agent_private_metadata(self.agent_id, metadata) if employee and not employee.is_overall else open_gallery_metadata(metadata)
            row = self.call("POST", "/knowledge-bases", {
                "space_id": self.space_id, "name": request.name,
                "description": request.description,
                "capability_scope": request.capability_scope, "metadata": metadata,
            }, scoped=False)
            return self.base_read(row).model_copy(update={"branch_head_version": None,
                                                        "branch_base_version": None,
                                                        "branch_sync_state": None})
        if kb_id and name not in {"list_documents", "list_discoveries"}:
            if name == "sync_knowledge_base_from_overall":
                try:
                    self.call("GET", f"/knowledge-bases/{kb_id}")
                except HTTPException as exc:
                    if exc.status_code != 404: raise
                    gallery = StaffDeckKnowledgeAdapter(self.db, self.tenant_id, None, self.user)
                    gallery.call("POST", f"/knowledge-bases/{kb_id}/bindings", {"space_id": self.space_id})
            self.require_base(kb_id, "manage" if request is not None else "view")
        if name == "get_knowledge_base":
            return self.base_read(self.call("GET", f"/knowledge-bases/{kb_id}"))
        if name == "update_knowledge_base":
            row = self.call("GET", f"/knowledge-bases/{kb_id}")
            body = {key: getattr(request, key) for key in ("name", "description", "status", "capability_scope", "metadata")
                    if getattr(request, key) is not None}
            body["expected_updated_at"] = row["updated_at"]
            employee = self.db.get(AgentProfile, self.agent_id) if self.agent_id else None
            if employee and not employee.is_overall and any(getattr(request, key) is not None for key in ("name", "description", "capability_scope", "metadata")):
                if any(getattr(request, key) is not None for key in ("name", "description", "capability_scope", "metadata")):
                    self.copy_legacy_branch(kb_id)
                    row = self.call("GET", f"/knowledge-bases/{kb_id}")
                versions = self.call("GET", f"/knowledge-bases/{kb_id}/versions")
                head = next(item for item in versions if item["id"] == row["branch"]["head_version_id"])
                body["expected_updated_at"] = head["updated_at"]
                self.call("PUT", f"/knowledge-bases/{kb_id}/branch", body)
                updated = self.call("GET", f"/knowledge-bases/{kb_id}")
            elif employee and not employee.is_overall and request.status is not None:
                self.call("PUT", f"/knowledge-bases/{kb_id}/binding", {"status": "active" if request.status == "active" else "inactive"})
                updated = self.call("GET", f"/knowledge-bases/{kb_id}")
            else:
                updated = self.call("PUT", f"/knowledge-bases/{kb_id}", body)
            return self.base_read(updated)
        if name == "delete_knowledge_base":
            return self.call("DELETE", f"/knowledge-bases/{kb_id}")
        if name == "list_knowledge_base_versions":
            rows = self.call("GET", f"/knowledge-bases/{kb_id}/versions")
            branch = self.call("GET", f"/knowledge-bases/{kb_id}")["branch"]
            fields = ("id", "version", "name", "description", "status", "capability_scope", "created_at", "updated_at")
            return [{**{key: row[key] for key in fields}, "is_head": row["id"] == branch["head_version_id"],
                     "is_base": row["id"] == branch["base_version_id"]} for row in sorted(rows, key=lambda row: row["updated_at"], reverse=True)]
        if name in {"sync_knowledge_base_from_overall", "promote_knowledge_base_to_overall", "rollback_knowledge_base"}:
            operation = {"sync_knowledge_base_from_overall": "sync",
                         "promote_knowledge_base_to_overall": "publish",
                         "rollback_knowledge_base": "rollback"}[name]
            body = {}
            if operation == "sync":
                base = self.call("GET", f"/knowledge-bases/{kb_id}")
                overall_agent = self.db.exec(select(AgentProfile).where(AgentProfile.tenant_id == self.tenant_id, AgentProfile.is_overall == True)).first()
                if overall_agent is None or base["status"] != "active" or base.get("metadata", {}).get("private_to_agent"):
                    raise HTTPException(400, "Disabled knowledge base cannot be learned from the open gallery")
            if operation == "publish":
                base = self.call("GET", f"/knowledge-bases/{kb_id}")
                versions = self.call("GET", f"/knowledge-bases/{kb_id}/versions")
                published = next(row for row in versions if row["id"] == base["branch"]["base_version_id"])
                from app.agents.branching import next_global_version
                self.call("POST", f"/knowledge-bases/{kb_id}/versions/copy", {
                    "version_label": next_global_version(published["version"]),
                })
            if operation == "rollback":
                rows = self.call("GET", f"/knowledge-bases/{kb_id}/versions")
                version = next((row for row in rows if row["version"] == request.version), None)
                if version is None:
                    base = self.call("GET", f"/knowledge-bases/{kb_id}")
                    created = self.call("POST", f"/knowledge-bases/{kb_id}/versions/copy", {
                        "version_label": request.version, "empty": True, "name": base["name"],
                        "description": base["description"],
                    })
                    version = {"id": created["head_version_id"]}
                body["version_id"] = version["id"]
            branch = self.call("POST", f"/knowledge-bases/{kb_id}/versions/{operation}", body)
            if name == "promote_knowledge_base_to_overall":
                overall = StaffDeckKnowledgeAdapter(self.db, self.tenant_id, None, self.user, write=True)
                overall.ensure_space()
                self.call("POST", f"/knowledge-bases/{kb_id}/bindings", {"space_id": overall.space_id})
                overall.call("POST", f"/knowledge-bases/{kb_id}/versions/sync", {})
                current = self.call("GET", f"/knowledge-bases/{kb_id}")
                head = next(row for row in self.call("GET", f"/knowledge-bases/{kb_id}/versions")
                    if row["id"] == current["branch"]["head_version_id"])
                overall.call("PUT", f"/knowledge-bases/{kb_id}", {
                    "expected_updated_at": current["updated_at"],
                    **{key: head[key] for key in ("name", "description", "capability_scope")},
                    "metadata": {**current.get("metadata", {}), "private_to_agent": False, "open_gallery": True},
                })
            head = next(row for row in self.call("GET", f"/knowledge-bases/{kb_id}/versions")
                        if row["id"] == branch["head_version_id"])
            return {"status": {"sync": "synced", "publish": "promoted", "rollback": "rolled_back"}[operation],
                    "knowledge_base_id": kb_id,
                    "version" if operation == "publish" else "head_version": head["version"]}
        if name in {"upload_document", "upload_document_for_public_host", "import_okf_bundle"}:
            self.ensure_space()
            kb_id = request.knowledge_base_id
            if not kb_id:
                row = self.call("POST", "/knowledge-bases", {
                    "space_id": self.space_id, "name": request.filename.rsplit(".", 1)[0],
                }, scoped=False)
                kb_id = row["id"]
            self.require_base(kb_id, "manage")
            employee = self.db.get(AgentProfile, self.agent_id) if self.agent_id else None
            if employee and not employee.is_overall:
                self.copy_legacy_branch(kb_id)
            body = {"space_id": self.space_id, "filename": request.filename,
                    "content_base64": request.content_base64,
                    "title": getattr(request, "title", None)}
            if name != "import_okf_bundle":
                from app.agents.branching import user_creator_metadata
                body.update(capability_scope=request.capability_scope,
                            metadata=user_creator_metadata(self.user, request.metadata))
                body["metadata"].pop("_pilotdeck_host", None)
                body["metadata"].pop("capability_context", None)
                if name == "upload_document_for_public_host":
                    body["metadata"]["capability_context"] = {"public_origin": values["host_origin"]}
            row = self.call("POST", f"/knowledge-bases/{kb_id}/" + (
                "okf" if name == "import_okf_bundle" else "documents"), body, scoped=False)
            if name == "import_okf_bundle":
                base = self.call("GET", f"/knowledge-bases/{kb_id}")
                version = next(item for item in self.call("GET", f"/knowledge-bases/{kb_id}/versions")
                               if item["id"] == base["branch"]["head_version_id"])
                return {**row, "status": "imported", "knowledge_base_id": kb_id,
                        "knowledge_base_version_id": version["id"], "version": version["version"]}
            return self.job_read(row)
        if name == "list_documents":
            try:
                bases = [self.call("GET", f"/knowledge-bases/{kb_id}")] if kb_id else self.call("GET", "/knowledge-bases")
            except HTTPException as exc:
                if exc.status_code == 404:
                    return []
                raise
            rows = []
            for base in bases:
                self.require_base(base["id"])
                rows.extend(self.call("GET", f"/knowledge-bases/{base['id']}/documents"))
            return [self.document_read(row) for row in sorted(rows, key=lambda row: row["created_at"], reverse=True)]
        if name in {"get_document", "get_job"}:
            collection, key, model = (
                ("documents", "document_id", schema.KnowledgeDocumentRead) if name == "get_document"
                else ("jobs", "job_id", schema.KnowledgeIngestJobRead)
            )
            row = self.call("GET", f"/{collection}/{values[key]}")
            self.require_base(row["knowledge_base_id"])
            return self.document_read(row) if name == "get_document" else self.job_read(row)
        if name == "list_jobs":
            params = f"?limit={values.get('limit', 20)}"
            if values.get("status"):
                params += "&status=" + quote(values["status"])
            return [self.job_read(row)
                    for row in self.call("GET", "/jobs" + params)]
        if name == "cancel_job":
            row = self.call("GET", f"/jobs/{values['job_id']}")
            self.require_base(row["knowledge_base_id"], "manage")
            return self.job_read(self.call("POST", f"/jobs/{row['id']}/cancel"))
        if name == "get_document_buckets":
            rows = self.call("GET", f"/documents/{values['document_id']}/buckets")
            return [schema.KnowledgeBucketRead.model_validate({**row, "chunk_count": len(self.call("GET", f"/buckets/{row['id']}/chunks"))}) for row in rows]
        if name == "get_bucket_chunks":
            return [schema.KnowledgeChunkRead.model_validate(row) for row in self.call(
                "GET", f"/buckets/{values['bucket_id']}/chunks")]
        if name == "update_document":
            row = self.call("GET", f"/documents/{values['document_id']}")
            self.require_base(row["knowledge_base_id"], "manage")
            if request.expected_updated_at and request.expected_updated_at != row["updated_at"]:
                raise HTTPException(409, "文档已被其他人修改，请刷新后再保存。")
            employee = self.db.get(AgentProfile, self.agent_id) if self.agent_id else None
            if employee and not employee.is_overall:
                self.copy_legacy_branch(row["knowledge_base_id"])
                documents = self.call("GET", f"/knowledge-bases/{row['knowledge_base_id']}/documents")
                row = next((item for item in documents if item["filename"] == row["filename"] and item["file_type"] == row["file_type"]), None)
                if row is None:
                    raise HTTPException(404, "Knowledge document branch copy not found")
            body = {key: getattr(request, key) for key in ("content_md", "title", "status", "metadata")
                    if getattr(request, key) is not None}
            body["expected_updated_at"] = row["updated_at"]
            return self.document_read(self.call("PUT", f"/documents/{row['id']}", body))
        if name in {"update_bucket", "update_chunk"}:
            layer, key, model = ("buckets", "bucket_id", schema.KnowledgeBucketRead) if name == "update_bucket" else ("chunks", "chunk_id", schema.KnowledgeChunkRead)
            row = self.call("GET", f"/{layer}/{values[key]}")
            self.require_base(row["knowledge_base_id"], "manage")
            body = {"expected_updated_at": row["updated_at"], "refresh_sources": True}
            if getattr(request, "summary", None) is not None:
                body["summary"] = request.summary
            if request.metadata is not None:
                body["metadata"] = request.metadata
            if getattr(request, "title", None) is not None:
                body["title"] = request.title
            if getattr(request, "content", None) is not None:
                body["content_md"] = request.content
            if getattr(request, "metadata", None) and request.metadata.get("content"):
                body["content_md"] = request.metadata["content"]
            result = self.call("PUT", f"/{layer}/{row['id']}", body)
            if layer == "buckets":
                result["chunk_count"] = len(self.call("GET", f"/buckets/{row['id']}/chunks"))
            return model.model_validate(result)
        if name == "search_knowledge":
            self.ensure_space()
            from app.knowledge.public_host_selection import public_host_retrieval_selected
            if public_host_retrieval_selected() and request.model_config_id:
                raise HTTPException(400, "Public host retrieval cannot select an SD model")
            if request.model_config_id:
                from app.api.knowledge import _get_request_model
                _get_request_model(self.db, self.tenant_id, request.model_config_id)
            versions = self.visible_versions()
            permitted = []
            for base_id, version in versions.items():
                if request.knowledge_base_ids and base_id not in request.knowledge_base_ids:
                    continue
                if request.knowledge_base_version_ids and version.id not in request.knowledge_base_version_ids:
                    continue
                try:
                    self.require_base(base_id, "use")
                except HTTPException as exc:
                    if exc.status_code != 403:
                        raise
                    continue
                permitted.append(base_id)
            if not permitted:
                trace = [{"phase": "no_visible_knowledge", "message": "当前范围没有可见知识"}]
                return schema.KnowledgeSearchResponse(trace=trace, route_trace=trace)
            result = self.call("POST", "/query", {
                **{key: getattr(request, key) for key in (
                    "desired_evidence", "scope", "document_ids", "max_bucket_rounds",
                    "budget_tokens", "max_depth", "need_evidence_pack",
                )},
                "space_id": self.space_id, "query": request.query,
                "query_type": {"tool_discovery": "interface_discovery", "skill_discovery": "procedure_discovery"}.get(request.query_type, request.query_type),
                "mode": "discovery" if request.mode in {"workbench", "skill_discovery"} else request.mode,
                "knowledge_base_ids": permitted,
                "version_ids": request.knowledge_base_version_ids,
                "max_chunks": request.max_chunks, "max_buckets": request.max_buckets,
                "use_model": not public_host_retrieval_selected(),
                "model_id": request.model_config_id,
            }, scoped=False)
            return schema.KnowledgeSearchResponse.model_validate(result)
        if name in {"list_okf_concepts", "get_okf_concept", "upsert_okf_concept"}:
            rows = sorted(self.call("GET", f"/knowledge-bases/{kb_id}/concepts", params={"materialize": "true"}),
                          key=lambda row: (row["concept_type"], row["concept_id"]))
            if name == "list_okf_concepts":
                return [schema.KnowledgeConceptRead.model_validate(row) for row in rows
                        if not values.get("concept_type") or row["concept_type"] == values["concept_type"]]
            row = next((row for row in rows if row["concept_id"] == values["concept_id"]), None)
            if name == "upsert_okf_concept":
                from urllib.parse import quote
                employee = self.db.get(AgentProfile, self.agent_id) if self.agent_id else None
                if employee and not employee.is_overall:
                    self.copy_legacy_branch(kb_id)
                    rows = self.call("GET", f"/knowledge-bases/{kb_id}/concepts")
                    row = next((item for item in rows if item["concept_id"] == values["concept_id"]), None)
                return schema.KnowledgeConceptRead.model_validate(self.call("PUT",
                    f"/knowledge-bases/{kb_id}/concepts/{quote(values['concept_id'], safe='/')}", {
                        "content_md": request.content_md, "document_id": request.document_id,
                        "status": request.status,
                        "expected_updated_at": row["updated_at"] if row else None,
                    }))
            if row is None:
                raise HTTPException(404, "Knowledge concept not found")
            return schema.KnowledgeConceptRead.model_validate(row)
        if name == "export_okf":
            response = self.call("GET", f"/knowledge-bases/{kb_id}/okf", raw=True)
            base = self.call("GET", f"/knowledge-bases/{kb_id}")
            version = next(row for row in self.call("GET", f"/knowledge-bases/{kb_id}/versions")
                           if row["id"] == base["branch"]["head_version_id"])
            return Response(response.content, media_type="application/zip",
                            headers={"Content-Disposition": f'attachment; filename="{kb_id}-okf-{version["version"]}.zip"'})
        if name == "lint_okf":
            return {"status": "ok", **self.call("POST", f"/knowledge-bases/{kb_id}/okf/lint", params={"materialize": "true"})}
        if name == "list_discoveries":
            try:
                bases = [self.call("GET", f"/knowledge-bases/{kb_id}")] if kb_id else self.call("GET", "/knowledge-bases")
            except HTTPException as exc:
                if exc.status_code == 404:
                    return []
                raise
            rows = [item for base in bases for item in self.call(
                "GET", f"/knowledge-bases/{base['id']}/suggestions")]
            return [schema.KnowledgeDiscoveryRead.model_validate({**row,
                     "suggestion_type": {"procedure": "skill", "interface": "tool"}.get(row["suggestion_type"], row["suggestion_type"])})
                    for row in sorted(rows, key=lambda item: item["created_at"], reverse=True)
                    if not values.get("status") or row["status"] == values["status"]]
        if name == "reject_discovery":
            self.call("POST", f"/suggestions/{values['suggestion_id']}/reject")
            return {"status": "rejected"}
        if name == "confirm_discovery":
            row = self.call("GET", f"/suggestions/{values['suggestion_id']}")
            self.require_base(row["knowledge_base_id"], "manage")
            if row["status"] == "confirmed":
                raise HTTPException(409, "只有待处理建议可以确认，当前状态为 confirmed。")
            if row["status"] == "pending":
                self.call("POST", f"/suggestions/{values['suggestion_id']}/review")
            return {"status": "confirmed", "result": self.call("POST", f"/suggestions/{values['suggestion_id']}/publish")}
        raise HTTPException(501, f"KM compatibility operation not implemented: {name}")

    def document_read(self, row):
        metadata = dict(row.get("metadata", {}))
        for key in ("creator_id", "km_model_id", "capability_scope"):
            metadata.pop(key, None)
        if metadata.get("created_by_username"):
            metadata["creator_name"] = metadata["created_by_username"]
        if "online_edited_at" not in metadata:
            metadata.pop("raw_text", None)
        return schema.KnowledgeDocumentRead.model_validate({**row, "metadata": metadata})

    def copy_legacy_branch(self, kb_id):
        row = self.call("GET", f"/knowledge-bases/{kb_id}")
        versions = self.call("GET", f"/knowledge-bases/{kb_id}/versions")
        head = next(item for item in versions if item["id"] == row["branch"]["head_version_id"])
        root = head["version"].partition("-branch.")[0]
        prefix = f"{root}-branch.{self.agent_id}."
        number = int(head["version"].removeprefix(prefix)) + 1 if head["version"].startswith(prefix) else 1
        return self.call("POST", f"/knowledge-bases/{kb_id}/versions/copy", {
            "version_label": prefix + str(number), "preserve_metadata": True,
            "name": row["name"], "description": row["description"],
        })

    def job_read(self, row):
        metadata = dict(row.get("metadata", {}))
        for key in ("content_base64", "_pilotdeck_host"):
            metadata.pop(key, None)
        nested = dict(metadata.get("metadata", {}))
        for key in ("creator_id", "km_model_id", "capability_scope"):
            nested.pop(key, None)
        if nested.get("created_by_username"):
            nested["creator_name"] = nested["created_by_username"]
        metadata["metadata"] = nested
        return schema.KnowledgeIngestJobRead.model_validate({**row, "metadata": metadata})


def km_http_operation(function):
    signature = inspect.signature(function)
    @wraps(function)
    def wrapped(*args, **kwargs):
        if not enabled():
            raise HTTPException(503, "Independent knowledge service is not configured")
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        values = dict(bound.arguments)
        request = values.get("request")
        tenant_id = getattr(request, "tenant_id", None) or values.get("tenant_id")
        agent_id = values.get("agent_id")
        if not isinstance(agent_id, str):
            agent_id = getattr(request, "agent_id", None)
        if not agent_id:
            agent_id = values["db"].info.get("staffdeck_km_agent_id")
        user = values.get("current_user")
        if not isinstance(user, User):
            user = None
        write = function.__name__.startswith(("create_", "update_", "delete_", "upload_", "import_", "cancel_", "confirm_", "reject_", "sync_", "promote_", "rollback_", "upsert_"))
        adapter = StaffDeckKnowledgeAdapter(values["db"], tenant_id, agent_id, user, write=write)
        return adapter.handle(function.__name__, values)
    hints = get_type_hints(function)
    wrapped.__signature__ = signature.replace(
        parameters=[param.replace(annotation=hints.get(name, param.annotation))
                    for name, param in signature.parameters.items()],
        return_annotation=hints.get("return", signature.return_annotation),
    )
    return wrapped
