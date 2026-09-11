"""ACP kernel configuration.

Holds the tunable thresholds for context-pressure nudging and lexical
search. Values are validated eagerly so misconfiguration fails fast at
construction time instead of surfacing as surprising runtime behaviour.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class AcpConfig:
    """Tunable parameters for the ACP compression kernel.

    Percentages are fractions in ``(0, 1]`` and must satisfy
    ``min <= max <= emergency`` so the pressure zones stay well ordered.
    """

    model_context_limit: int = 128000
    nudge_max_context_limit_pct: float = 0.70
    nudge_emergency_threshold_pct: float = 0.85
    nudge_min_context_limit_pct: float = 0.45
    search_top_k: int = 5
    search_min_score: float = 0.0
    search_ngram_size: int = 3
    max_tier: int = 3

    def __post_init__(self) -> None:
        if self.model_context_limit <= 0:
            raise ValueError("model_context_limit must be positive")
        for name, value in (
            ("nudge_max_context_limit_pct", self.nudge_max_context_limit_pct),
            ("nudge_emergency_threshold_pct", self.nudge_emergency_threshold_pct),
            ("nudge_min_context_limit_pct", self.nudge_min_context_limit_pct),
        ):
            if not 0 < value <= 1:
                raise ValueError(f"{name} must be in (0, 1], got {value}")
        if not (
            self.nudge_min_context_limit_pct
            <= self.nudge_max_context_limit_pct
            <= self.nudge_emergency_threshold_pct
        ):
            raise ValueError("nudge thresholds must satisfy min <= max <= emergency")
        if self.search_top_k <= 0:
            raise ValueError("search_top_k must be positive")
        if self.search_ngram_size < 2:
            raise ValueError("search_ngram_size must be at least 2")
        if self.max_tier < 2:
            raise ValueError("max_tier must be at least 2")