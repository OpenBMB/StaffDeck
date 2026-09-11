"""Tiered distillation of summary nodes.

Compressing a summary block again produces a higher tier (tier2, tier3).
Each tier keeps its own checkpoint, so the chain stays traceable back to
the original messages.
"""

MAX_TIER = 3


def next_tier(tier: int) -> int:
    """Return the tier produced by distilling ``tier`` once more."""
    if tier < 1:
        raise ValueError(f"tier must be >= 1, got {tier}")
    if tier >= MAX_TIER:
        raise ValueError(f"cannot distill beyond max tier {MAX_TIER}")
    return tier + 1


def can_distill(tier: int) -> bool:
    """Whether a block at ``tier`` can be distilled once more."""
    return 1 <= tier < MAX_TIER


def tier_label(tier: int) -> str:
    """Human-readable label for a tier, e.g. ``tier2``."""
    return f"tier{tier}"