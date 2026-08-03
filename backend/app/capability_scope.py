from __future__ import annotations

from typing import Literal, TypeAlias


CapabilityScope: TypeAlias = Literal["general", "sop_specific"]

GENERAL_CAPABILITY_SCOPE: CapabilityScope = "general"
SOP_SPECIFIC_CAPABILITY_SCOPE: CapabilityScope = "sop_specific"


def normalize_capability_scope(value: object) -> CapabilityScope:
    """Normalize persisted or imported values without making old rows unusable."""

    if value == SOP_SPECIFIC_CAPABILITY_SCOPE:
        return SOP_SPECIFIC_CAPABILITY_SCOPE
    return GENERAL_CAPABILITY_SCOPE
