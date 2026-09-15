"""Shared model-facing knowledge result; no storage or Provider dependencies."""
from typing import Any

_MODEL_EXCERPT_CHARS = 1200
_MODEL_MAX_ITEMS = 8


def model_facing_knowledge(payload: dict[str, Any], citations: list[dict[str, Any]]) -> dict[str, Any]:
    """Keep full evidence outside the transcript and expose the same labelled view."""
    by_identity: dict[str, str] = {}
    for citation in citations:
        for key in ('chunk_id', 'concept_id'):
            if citation.get(key):
                by_identity[str(citation[key])] = str(citation.get('label') or '')
    items: list[dict[str, Any]] = []
    for raw in (payload.get('evidence_pack'), payload.get('chunks'), payload.get('selected_concepts')):
        if not isinstance(raw, list) or not raw:
            continue
        for item in raw:
            if not isinstance(item, dict):
                continue
            ident = str(item.get('chunk_id') or item.get('id') or item.get('concept_id') or '')
            text = str(item.get('content') or item.get('text') or item.get('excerpt') or item.get('summary') or '')
            if not text.strip():
                continue
            items.append({
                'label': by_identity.get(ident) or f'[{len(items) + 1}]',
                'title': item.get('title') or item.get('section_title') or item.get('document_title') or '',
                'source': item.get('source_path') or item.get('document_id') or item.get('knowledge_base_id') or '',
                'excerpt': text[:_MODEL_EXCERPT_CHARS],
            })
            if len(items) >= _MODEL_MAX_ITEMS:
                break
        if items:
            break
    if not items:
        for bucket in payload.get('selected_buckets') or []:
            if isinstance(bucket, dict) and (bucket.get('summary') or bucket.get('title')):
                items.append({'label': f'[{len(items) + 1}]', 'title': bucket.get('title') or '',
                    'source': bucket.get('knowledge_base_id') or '',
                    'excerpt': str(bucket.get('summary') or '')[:_MODEL_EXCERPT_CHARS]})
                if len(items) >= _MODEL_MAX_ITEMS:
                    break
    return {'query': payload.get('query'), 'hit_count': len(items), 'results': items,
        'citations': [{'label': c.get('label'), 'title': c.get('title'),
                       'source': c.get('source_path') or c.get('document_id') or ''} for c in citations],
        'instruction': '回答时用对应的 [N] 标注引用；没有命中的内容不要臆造。'}
