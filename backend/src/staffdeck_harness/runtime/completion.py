"""Shared completion checks for model result proposals; never a business capability."""

import json


def native_result(text):
    """Optional v2 finish envelope in the native final output, with no MCP round trip."""
    from app.core.harness_agent import HarnessAction

    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(value, dict) or value.get("action") != "finish":
        return None
    return HarnessAction.model_validate(value).model_dump(mode="json")


def missing_capabilities(requirement, results, citations, evidence):
    from app.core.harness_agent import _missing_required_capabilities

    ids = {str(c["knowledge_base_id"]) for c in citations if c.get("knowledge_base_id")}
    for pack in evidence:
        for item in [*(pack.get("chunks") or []), *(pack.get("evidence_pack") or [])]:
            if isinstance(item, dict) and item.get("knowledge_base_id"):
                ids.add(str(item["knowledge_base_id"]))
    return _missing_required_capabilities(requirement, results, ids)
