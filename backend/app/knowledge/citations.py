"""Compatibility import surface for the independent citation implementation."""

from knowledge_core.citations import (
    CITATION_EXCERPT_CHAR_LIMIT, CITATION_SUMMARY_CHAR_LIMIT, CONCEPT_EXCERPT_CHAR_LIMIT,
    EMAIL_PATTERN,
    knowledge_citation_identity, normalize_knowledge_citations,
    compact_knowledge_citation_labels, restore_truncated_atomic_references,
    knowledge_source_candidates, knowledge_citations_from_results,
)
