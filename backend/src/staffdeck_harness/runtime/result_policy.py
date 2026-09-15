"""Shared final capability projection. Transport completion is not output approval."""
from staffdeck_harness.contracts.invocation import ModuleResult
from dataclasses import replace
from app.knowledge.citations import normalize_knowledge_citations


def project_result(run_hooks, invocation, result):
    result = replace(result, citations=tuple(normalize_knowledge_citations(result.citations)))
    if run_hooks is None:
        return result
    decision = run_hooks('post_tool', invocation, result)
    if decision.kind == 'deny':
        return ModuleResult.fail('POST_TOOL_DENIED', decision.reason or 'result refused by policy')
    if decision.replacement is not None:
        if not isinstance(decision.replacement, ModuleResult):
            return ModuleResult.fail('POST_TOOL_DENIED', 'hook replacement must be a ModuleResult')
        return replace(decision.replacement, citations=tuple(normalize_knowledge_citations(decision.replacement.citations)))
    return result
