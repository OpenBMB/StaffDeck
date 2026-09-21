"""Shared final capability projection. Transport completion is not output approval."""
from staffdeck_harness.contracts.invocation import ModuleResult, normalize_result
from dataclasses import replace
from app.knowledge.citations import normalize_knowledge_citations


def project_result(run_hooks, invocation, result):
    result = normalize_result(result)
    if (result.error or {}).get('code') == 'RESULT_NOT_JSON_SAFE':
        return result
    result = replace(result, citations=tuple(normalize_knowledge_citations(result.citations)))
    if run_hooks is None:
        return normalize_result(result)
    decision = run_hooks('post_tool', invocation, result)
    if decision.kind == 'deny':
        return ModuleResult.fail('POST_TOOL_DENIED', decision.reason or 'result refused by policy')
    if decision.replacement is not None:
        if not isinstance(decision.replacement, ModuleResult):
            return ModuleResult.fail('POST_TOOL_DENIED', 'hook replacement must be a ModuleResult')
        replacement = normalize_result(decision.replacement)
        return normalize_result(replace(replacement, citations=tuple(normalize_knowledge_citations(replacement.citations))))
    return normalize_result(result)
