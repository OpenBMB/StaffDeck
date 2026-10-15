"""Content processing reuses the established section, bucket and evidence rules."""
from types import SimpleNamespace
from pathlib import Path
from uuid import uuid4
from . import algorithms as rules


class Processing:
    def __init__(self, model=None, config=None):
        self.model, self.config = model, config

    def process(self, filename, text, title=None):
        text = rules._normalize_text(text)
        sections = rules._build_section_nodes(text)
        title = title or rules._guess_title(text, 0) or filename
        card = rules._build_document_card(title, filename, Path(filename).suffix.lstrip("."), text, sections)
        specs = rules._structure_bucket_specs(sections)
        trace = []
        if self.model and self.config:
            try:
                prompt = (Path(__file__).parents[1] / "prompts/knowledge_bucket_prompt.md").read_text()
                call_id = uuid4().hex
                value = self.model.generate_json(self.config, prompt, {"sections": sections, "document_card": card})
                if not isinstance(value, dict) or not isinstance(value.get("buckets"), list): raise ValueError("Invalid model bucket result")
                normalized = rules._normalize_llm_bucket_specs(value["buckets"], sections)
                if not normalized: raise ValueError("Model returned no valid buckets")
                specs += normalized
                trace.append({"phase": "bucket_model", "model": self.config.model, "call_id": call_id, "valid_bucket_count": len(normalized)})
            except Exception as error:
                trace.append({"phase": "bucket_model_fallback", "error_type": type(error).__name__})
        specs = rules._unique_bucket_specs(specs) or rules._fallback_bucket_specs(rules._split_sections(text), sections)
        return text, title, card, specs, trace

    def layers(self, document_id, filename, text, card, specs, moment):
        from . import storage as s
        sections = {str(node['section_id']): node for node in rules._build_section_nodes(text)}
        buckets, chunks = [], []
        for index, spec in enumerate(specs):
            section_ids = [str(item) for item in spec.get('section_ids', []) if item]
            sources = [sections[item] for item in section_ids if item in sections]
            body = str(spec.get('content') or '') or '\n\n'.join(str(item.get('content') or '') for item in sources)
            body = body or text[:rules.BUCKET_SECTION_CHARS]
            bucket_id = s.identifier('kbucket')
            title = str(spec.get('title') or f'Knowledge topic {index + 1}')
            metadata = {
                'ingest_schema_version': rules.KNOWLEDGE_INGEST_SCHEMA_VERSION,
                'bucket_key': spec.get('bucket_key') or f'bucket_{index + 1}',
                'content': body[:rules.BUCKET_SECTION_CHARS],
                'bucket_type': spec.get('bucket_type') or 'structure',
                'concept_type': spec.get('concept_type') or 'Topic',
                'section_ids': section_ids,
                'section_paths': spec.get('section_paths') if isinstance(spec.get('section_paths'), list) else [],
                'applicable_query_types': spec.get('applicable_query_types') if isinstance(spec.get('applicable_query_types'), list) else [],
                'quality': rules._bucket_quality(spec, section_ids, body),
                'document_card': {key: card.get(key) for key in ('title', 'summary')},
            }
            bucket = {'id': bucket_id, 'document_id': document_id, 'title': title,
                      'summary': str(spec.get('summary') or body[:300]), 'content': body,
                      'metadata': metadata, 'created_at': moment, 'updated_at': moment}
            local_chunks = []
            sources = sources or [{'section_id': metadata['bucket_key'] + '_content', 'path': title, 'title': title, 'content': metadata['content']}]
            for section in sources:
                content = str(section.get('content') or '')
                related_section = section.get('related_section_id') or section.get('section_id') or len(local_chunks)
                for group_index, parts in enumerate(rules._chunk_text_related_groups(content, rules.EVIDENCE_CHUNK_CHARS)):
                    group = []
                    for part in parts:
                        local_index = len(local_chunks)
                        row = {'id': s.identifier('kchunk'), 'document_id': document_id, 'bucket_id': bucket_id,
                               'chunk_index': local_index, 'content': part, 'summary': rules._summarize_text(part, 180),
                               'source_ref': f"{filename} / {section.get('path') or title} / evidence {local_index + 1}",
                               'metadata': {'ingest_schema_version': rules.KNOWLEDGE_INGEST_SCHEMA_VERSION,
                                   'node_type': 'evidence_chunk', 'related_group_id': f'{document_id}:{bucket_id}:{related_section}:{group_index}',
                                   'related_chunk_index': len(group), 'section_id': section.get('section_id'),
                                   'section_path': section.get('path'), 'section_title': section.get('title'),
                                   'bucket_title': title, 'source_span': section.get('source_span') or {},
                                   'context_window': rules._summarize_text(content, 260)},
                               'created_at': moment, 'updated_at': moment}
                        group.append(row); local_chunks.append(row)
                    for position, row in enumerate(group):
                        window = rules._related_chunk_window([SimpleNamespace(**item) for item in group], position) if len(group) > 1 else []
                        ids = [item.id for item in window]
                        offset = ids.index(row['id']) if ids else 0
                        row['metadata'].update(related_chunk_ids=ids, related_chunk_count=len(ids),
                            related_previous_chunk_id=ids[offset - 1] if offset else None,
                            related_next_chunk_id=ids[offset + 1] if offset + 1 < len(ids) else None)
                        if not ids: row['metadata']['related_group_id'] = None
            metadata.update(representative_chunk_ids=[row['id'] for row in local_chunks[:3]], chunk_count=len(local_chunks))
            buckets.append(bucket); chunks.extend(local_chunks)
        return buckets, chunks


class Retrieval:
    def __init__(self, model=None, config=None):
        self.model, self.config = model, config

    def query(self, query, documents, buckets, chunks, limit=8, use_model=True, options=None):
        options = options or {}
        docs = [SimpleNamespace(**{**row, "metadata_json": row["metadata"]}) for row in documents]
        indexed = [SimpleNamespace(**{**row, "metadata_json": row["metadata"]}) for row in buckets]
        evidence = [SimpleNamespace(**{**row, "metadata_json": row["metadata"]}) for row in chunks]
        selected = rules._score_documents(query, docs)[:5]
        trace = [{"phase": "document_route", "candidate_count": len(docs), 'message': '正在选择知识文档', 'mode': options.get('mode', 'auto')}]
        if use_model and self.model and self.config:
            try:
                prompt = (Path(__file__).parents[1] / "prompts/knowledge_document_route_prompt.md").read_text()
                call_id = uuid4().hex
                value = self.model.generate_json(self.config, prompt, {"query": query, "documents": [rules._document_card_for_route(row) for row in docs]})
                if not isinstance(value, dict): raise ValueError("Invalid model routing result")
                ids = value.get("selected_document_ids")
                if not isinstance(ids, list) or not all(isinstance(item, str) and item in {row.id for row in docs} for item in ids):
                    raise ValueError("Invalid model document routing")
                selected = [row for row in docs if row.id in ids]
                trace.append({"phase": "document_route_model", "model": self.config.model, "call_id": call_id, "selected_count": len(selected)})
            except Exception as error:
                trace.append({"phase": "document_route_lexical_fallback", "error_type": type(error).__name__})
        else:
            trace.append({"phase": "document_route_lexical", 'message': '按检索相关性选择知识文档', 'selected_count': len(selected)})
        ids = {row.id for row in selected}
        indexed = [row for row in indexed if row.document_id in ids]
        bucket_limit = options.get('max_buckets', 4)
        ranked_buckets = rules._score_buckets(query, indexed, options.get('query_type'))[:bucket_limit]
        trace.append({"phase": "bucket_route", "candidate_count": len(indexed), 'message': '正在选择内部索引', 'selected_document_ids': [row.id for row in selected]})
        if use_model and self.model and self.config and indexed:
            try:
                prompt = (Path(__file__).parents[1] / "prompts/knowledge_search_prompt.md").read_text()
                candidates = [row for row in indexed if row.document_id in ids]
                call_id = uuid4().hex
                value = self.model.generate_json(self.config, prompt, {"query": query, "buckets": [
                    {"id": row.id, "title": row.title, "summary": row.summary, "metadata": row.metadata_json} for row in candidates]})
                selected_ids = value.get("selected_bucket_ids") if isinstance(value, dict) else None
                if not isinstance(selected_ids, list) or not all(isinstance(item, str) and item in {row.id for row in candidates} for item in selected_ids):
                    raise ValueError("Invalid model bucket routing")
                ranked_buckets = [next(row for row in candidates if row.id == selected_id) for selected_id in selected_ids][:bucket_limit]
                trace.append({"phase": "bucket_route_model", "model": self.config.model, "call_id": call_id, "selected_count": len(ranked_buckets)})
            except Exception as error:
                trace.append({"phase": "bucket_route_lexical_fallback", "error_type": type(error).__name__})
        else: trace.append({"phase": "bucket_route_lexical", 'message': '按检索相关性选择内部索引', 'selected_count': len(ranked_buckets)})
        bucket_ids = {row.id for row in ranked_buckets}
        expanded = rules._expand_sections(selected, ranked_buckets, options.get('max_depth', 2))
        trace.append({"phase": "section_expand", "section_count": len(expanded), 'message': '正在展开章节'})
        candidates = [row for row in evidence if row.bucket_id in bucket_ids]
        ranked = rules._rank_chunks(query, candidates, ranked_buckets, expanded)
        hits = rules._select_diverse_chunk_hits(ranked, ranked_buckets, limit)
        direct_hit_ids = {row.id for row in hits}
        hits = rules._expand_related_chunks(hits, candidates)
        by_id = {row["id"]: row for row in chunks}
        result = [by_id[row.id] for row in hits]
        pack = rules._build_evidence_pack(query, hits, direct_hit_ids=direct_hit_ids) if options.get('need_evidence_pack', True) else []
        trace.append({'phase': 'read_chunks', 'message': '读取引用来源', 'chunk_count': len(hits)})
        trace.append({"phase": "evidence_pack", "evidence_count": len(pack), 'message': '整理引用来源包'})
        return {"chunks": result, "buckets": [row for row in buckets if row["id"] in bucket_ids],
                "evidence_pack": pack, "expanded_sections": expanded,
                "route_trace": trace, "selected_concepts": [],
                'selected_documents': [rules._document_card_for_search(row) for row in selected]}
