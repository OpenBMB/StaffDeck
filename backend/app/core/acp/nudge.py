"""Pressure evaluation producing advisory nudge recommendations.

Nudges are advisory only: the kernel never forces compression. The model
decides whether and what to compress.
"""

from dataclasses import dataclass

from .config import AcpConfig


@dataclass(frozen=True)
class NudgeRecommendation:
    """Advisory recommendation to consider compressing context."""

    level: str
    current_tokens: int
    limit_tokens: int
    usage_pct: float
    message: str


def evaluate_pressure(current_tokens: int, config: AcpConfig) -> NudgeRecommendation | None:
    """Evaluate context pressure against configured thresholds.

    Returns ``None`` when pressure is below the nudge threshold, a normal
    recommendation at or above ``nudge_max_context_limit_pct``, and an
    emergency recommendation at or above ``nudge_emergency_threshold_pct``.
    """
    tokens = max(0, current_tokens)
    usage_pct = tokens / config.model_context_limit
    if usage_pct < config.nudge_min_context_limit_pct:
        return None
    if usage_pct >= config.nudge_emergency_threshold_pct:
        return NudgeRecommendation(
            level="emergency",
            current_tokens=tokens,
            limit_tokens=config.model_context_limit,
            usage_pct=usage_pct,
            message=(
                f"紧急：上下文使用率已达 {usage_pct:.0%}，建议立即压缩历史消息"
                "以释放空间（压缩由模型自主决定，非强制）。"
            ),
        )
    if usage_pct >= config.nudge_max_context_limit_pct:
        return NudgeRecommendation(
            level="normal",
            current_tokens=tokens,
            limit_tokens=config.model_context_limit,
            usage_pct=usage_pct,
            message=(
                f"提示：上下文使用率已达 {usage_pct:.0%}，可考虑压缩历史消息"
                "以释放空间（压缩由模型自主决定，非强制）。"
            ),
        )
    return None