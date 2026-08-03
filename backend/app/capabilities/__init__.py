"""Typed ports for externally provided Agent capabilities.

This package intentionally keeps Knowledge, Scene Skill and General Skill
contracts separate. Local adapters preserve the existing behavior while
allowing startup-time Provider implementations to replace the content source.
"""

from app.capabilities.contracts import (
    CapabilityContext,
    CitationDetail,
    GeneralSkillCatalog,
    GeneralSkillFile,
    GeneralSkillPackage,
    GeneralSkillResourceRef,
    KnowledgeRuntime,
    KnowledgeScope,
    KnowledgeSearchQuery,
    KnowledgeSearchResult,
    SceneSkillCatalog,
    SceneSkillDefinition,
)
from app.capabilities.errors import CapabilityErrorInfo, CapabilityProviderError
from app.capabilities.local_general_skill import (
    GeneralSkillRuntimeSnapshot,
    LocalGeneralSkillCatalog,
    local_runtime_snapshot,
    resource_ref_from_row,
    runtime_snapshot_from_package,
)
from app.capabilities.local_knowledge import LocalKnowledgeRuntime
from app.capabilities.local_registry import build_local_capability_registry
from app.capabilities.registry import (
    CapabilityBinding,
    CapabilityRegistry,
    CapabilitySnapshot,
    DurableCapabilityBinding,
)
from app.capability_scope import (
    GENERAL_CAPABILITY_SCOPE,
    SOP_SPECIFIC_CAPABILITY_SCOPE,
    CapabilityScope,
    normalize_capability_scope,
)
from app.capabilities.testkit import ContractViolation

__all__ = [
    "CapabilityBinding",
    "CapabilityContext",
    "CapabilityErrorInfo",
    "CapabilityProviderError",
    "CapabilityRegistry",
    "CapabilityScope",
    "CapabilitySnapshot",
    "CitationDetail",
    "ContractViolation",
    "DurableCapabilityBinding",
    "GeneralSkillCatalog",
    "GeneralSkillFile",
    "GeneralSkillPackage",
    "GeneralSkillResourceRef",
    "GeneralSkillRuntimeSnapshot",
    "GENERAL_CAPABILITY_SCOPE",
    "KnowledgeRuntime",
    "KnowledgeScope",
    "KnowledgeSearchQuery",
    "KnowledgeSearchResult",
    "LocalGeneralSkillCatalog",
    "LocalKnowledgeRuntime",
    "SceneSkillCatalog",
    "SceneSkillDefinition",
    "SOP_SPECIFIC_CAPABILITY_SCOPE",
    "build_local_capability_registry",
    "local_runtime_snapshot",
    "normalize_capability_scope",
    "resource_ref_from_row",
    "runtime_snapshot_from_package",
]
