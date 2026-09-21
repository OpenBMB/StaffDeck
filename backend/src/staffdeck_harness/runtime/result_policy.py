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


def check_replay(run_hooks, invocation, result):
    """Approved cached output is immutable; replay guards can only pass or deny.

    Activation, pre_tool and PEP are checked by Host before cache access. Dynamic
    output eligibility belongs in replay_tool, not repeatable transformations.
    """
    safe = normalize_result(result)
    if run_hooks is None:
        return safe
    decision = run_hooks('replay_tool', invocation, normalize_result(safe))
    if decision.kind == 'deny':
        return ModuleResult.fail('POST_TOOL_DENIED', decision.reason or 'cached result refused by policy')
    if decision.kind != 'pass' or decision.replacement is not None:
        return ModuleResult.fail('REPLAY_POLICY_INVALID', 'replay_tool must only pass or deny; cached results cannot be transformed again')
    return safe
