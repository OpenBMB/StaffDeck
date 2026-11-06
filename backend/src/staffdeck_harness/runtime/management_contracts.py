"""Public API-owned management contracts, shared by local routes and module adapters.

Opt in by operation name, never by a vendor's URL prefix. Execution endpoints
are deliberately absent. Models come from the public API, not copied schemas.
"""
from dataclasses import dataclass
from functools import lru_cache
from importlib import import_module
import json

from fastapi import HTTPException
from fastapi.routing import APIRoute
from pydantic import TypeAdapter, ValidationError


OPERATIONS = {
    ('staff', 'model_configs'): '''list_model_protocols list_model_configs create_model_config
        update_model_config delete_model_config set_default_model_config test_model_config'''.split(),
    ('staff', 'persona'): ['get_persona', 'update_persona'],
    # Runtime limits/context settings are served by the shared backend, not CP.
    ('staff', 'ui_config'): [],
    ('staff', 'agents'): '''create_agent get_agent update_agent unpublish_agent_from_gallery
        delete_agent get_agent_resources update_agent_resources import_agent_resources
        get_agent_skills sync_agent_skill_from_overall promote_agent_skill_to_overall
        rollback_agent_skill list_agent_skill_versions update_agent_models get_agent_models
        use_chat_agent'''.split(),
    ('tool', 'tools'): '''list_tools list_tool_buckets create_tool probe_tool get_tool update_tool
        delete_tool list_mcp_servers create_mcp_server get_mcp_server update_mcp_server
        delete_mcp_server discover_mcp_tools_adhoc discover_mcp_tools sync_mcp_tools'''.split(),
    ('sop', 'skills'): '''list_skills create_skill get_skill update_skill publish_skill archive_skill
        draft_skill delete_skill list_skill_versions get_skill_version delete_skill_version
        rollback_skill_version extract_skill_file distill_skill distill_skill_stream
        rewrite_skill_stream create_distill_job create_rewrite_job get_skill_stream_job
        stream_existing_skill_job cancel_skill_stream_job rewrite_skill'''.split(),
    ('skill', 'general_skills'): '''import_general_skill import_skillhub_skill import_clawhub_skill
        import_general_skill_package list_general_skills get_general_skill publish_general_skill
        archive_general_skill publish_general_skill_to_gallery delete_general_skill'''.split(),
    ('knowledge', 'knowledge_bases'): '''list_knowledge_bases create_knowledge_base get_knowledge_base
        update_knowledge_base list_knowledge_base_versions list_okf_concepts get_okf_concept
        upsert_okf_concept export_okf lint_okf delete_knowledge_base sync_knowledge_base_from_overall
        promote_knowledge_base_to_overall rollback_knowledge_base'''.split(),
    ('knowledge', 'knowledge'): '''upload_document import_okf_bundle list_jobs get_job cancel_job
        list_documents get_document update_document get_document_buckets update_bucket get_bucket_chunks
        update_chunk search_knowledge list_discoveries confirm_discovery reject_discovery'''.split(),
}


@dataclass(frozen=True)
class ManagementOperation:
    id: str
    domain: str
    route: APIRoute

    def validate_request(self, request):
        # Multipart data is interpreted by the operation-specific adapter. JSON
        # bodies are validated before any remote mutation can occur.
        field = self.route.body_field
        if field is None or 'application/json' not in request.headers.get('content-type', ''):
            return
        try:
            value = json.loads(request.body) if request.body else None
            _, errors = field.validate(value, {}, loc=('body',))
        except (ValueError, TypeError):
            raise HTTPException(422, '管理请求不是合法 JSON') from None
        if errors:
            raise HTTPException(422, '管理请求不符合公共接口契约')

    def validate_response(self, reply):
        from staffdeck_harness.contracts.runtime_services import ServiceResponse, StreamingServiceResponse
        if isinstance(reply, StreamingServiceResponse) or not 200 <= reply.status < 300:
            return reply
        model = self.route.response_model
        if model is None:
            return reply
        try:
            adapter = TypeAdapter(model)
            value = adapter.validate_json(reply.body)
            body = adapter.dump_json(value, by_alias=True)
        except (ValidationError, ValueError, TypeError) as exc:
            import logging
            issues = [{'field': list(item['loc']), 'type': item['type']} for item in exc.errors()] if isinstance(exc, ValidationError) else []
            logging.getLogger(__name__).warning('Management response contract rejected operation=%s issues=%s', self.id, issues)
            # Never include the upstream payload (which may contain credentials).
            # A committed mutation is not retried on a response contract failure.
            raise HTTPException(502, {'code': 'MANAGEMENT_RESPONSE_INVALID',
                'message': '模块返回不符合公共接口契约，请查询资源确认结果，勿重复提交',
                'operation': self.id}) from None
        headers = {k: v for k, v in reply.headers.items() if k.lower() not in {'content-length', 'content-type'}}
        return ServiceResponse(reply.status, {**headers, 'content-type': 'application/json'}, body)


@lru_cache(maxsize=1)
def operation_catalog():
    result = []
    for (domain, module), names in OPERATIONS.items():
        api = import_module('app.api.' + module)
        seen = set()
        for value in vars(api).values():
            if type(value).__name__ != 'APIRouter':
                continue
            for route in value.routes:
                if isinstance(route, APIRoute) and id(route) not in seen:
                    seen.add(id(route))
                    result.append(ManagementOperation(f'{domain}.{route.name}/v1', domain if route.name in names else '', route))
    return tuple(result)


def resolve_operation(method, path):
    for operation in operation_catalog():
        if method in operation.route.methods and operation.route.path_regex.fullmatch(path.rstrip('/')):
            return operation if operation.domain else None
    return None
