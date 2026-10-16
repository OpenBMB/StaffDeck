"""Wire capability descriptions contain no optional implementation imports."""
PREFIX = "/api/km/v1"
FULL = {
    "list_spaces": ("GET", "/spaces"), "create_space": ("POST", "/spaces"),
    "list_bindings": ("GET", "/bindings"), "bind_base": ("POST", "/knowledge-bases/{knowledge_base_id}/bindings"),
    "update_binding": ("PUT", "/knowledge-bases/{knowledge_base_id}/binding"),
    "list_bases": ("GET", "/knowledge-bases"), "create_base": ("POST", "/knowledge-bases"),
    "get_base": ("GET", "/knowledge-bases/{knowledge_base_id}"), "update_base": ("PUT", "/knowledge-bases/{knowledge_base_id}"),
    "delete_base": ("DELETE", "/knowledge-bases/{knowledge_base_id}"),
    "list_versions": ("GET", "/knowledge-bases/{knowledge_base_id}/versions"),
    **{name: ("POST", "/knowledge-bases/{knowledge_base_id}/versions/" + action) for name, action in
       (("copy_version", "copy"), ("sync_base", "sync"), ("publish_version", "publish"), ("rollback_version", "rollback"))},
    "update_branch": ("PUT", "/knowledge-bases/{knowledge_base_id}/branch"),
    "list_documents": ("GET", "/knowledge-bases/{knowledge_base_id}/documents"),
    "import_document": ("POST", "/knowledge-bases/{knowledge_base_id}/documents"),
    "get_document": ("GET", "/documents/{document_id}"), "update_document": ("PUT", "/documents/{document_id}"),
    "delete_document": ("DELETE", "/documents/{document_id}"),
    "list_document_buckets": ("GET", "/documents/{document_id}/buckets"),
    "list_document_chunks": ("GET", "/documents/{document_id}/chunks"),
    "list_bucket_chunks": ("GET", "/buckets/{bucket_id}/chunks"),
    **{verb + "_" + name: (method, "/" + name + "s/{" + name + "_id}") for name in ("bucket", "chunk", "concept") for verb, method in (("get", "GET"), ("update", "PUT"))},
    "get_job": ("GET", "/jobs/{job_id}"), "list_jobs": ("GET", "/jobs"), "cancel_job": ("POST", "/jobs/{job_id}/cancel"),
    "query": ("POST", "/query"), "resolve_citation": ("GET", "/citations/{chunk_id}"),
    "list_okf_concepts": ("GET", "/knowledge-bases/{knowledge_base_id}/concepts"),
    "get_okf_concept": ("GET", "/knowledge-bases/{knowledge_base_id}/concepts/{concept_id:path}"),
    "upsert_okf_concept": ("PUT", "/knowledge-bases/{knowledge_base_id}/concepts/{concept_id:path}"),
    "export_okf": ("GET", "/knowledge-bases/{knowledge_base_id}/okf"), "import_okf": ("POST", "/knowledge-bases/{knowledge_base_id}/okf"),
    "lint_okf": ("POST", "/knowledge-bases/{knowledge_base_id}/okf/lint"),
    "list_discoveries": ("GET", "/knowledge-bases/{knowledge_base_id}/suggestions"), "get_discovery": ("GET", "/suggestions/{suggestion_id}"),
    **{name + "_discovery": ("POST", "/suggestions/{suggestion_id}/" + action) for name, action in
       (("confirm", "review"), ("review", "review"), ("reject", "reject"), ("export", "export"), ("publish", "publish"))},
}


def describe(operations):
    return {name: {"method": method, "path": PREFIX + path} for name, (method, path) in operations.items()}
