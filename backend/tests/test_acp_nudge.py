"""Tests for pressure evaluation and nudge recommendations."""

import pytest

from app.core.acp.config import AcpConfig
from app.core.acp.nudge import evaluate_pressure


def test_below_max_threshold_returns_none() -> None:
    config = AcpConfig()
    assert evaluate_pressure(int(128000 * 0.5), config) is None


def test_at_max_threshold_returns_normal_nudge() -> None:
    config = AcpConfig()
    recommendation = evaluate_pressure(int(128000 * 0.70), config)
    assert recommendation is not None
    assert recommendation.level == "normal"
    assert recommendation.usage_pct == pytest.approx(0.70)
    assert recommendation.limit_tokens == 128000


def test_at_emergency_threshold_returns_emergency_nudge() -> None:
    config = AcpConfig()
    recommendation = evaluate_pressure(int(128000 * 0.85), config)
    assert recommendation is not None
    assert recommendation.level == "emergency"


def test_below_min_threshold_returns_none() -> None:
    config = AcpConfig()
    assert evaluate_pressure(int(128000 * 0.1), config) is None


def test_negative_tokens_are_clamped_to_zero() -> None:
    config = AcpConfig()
    assert evaluate_pressure(-100, config) is None


def test_nudge_is_advisory_and_never_mandatory() -> None:
    config = AcpConfig()
    recommendation = evaluate_pressure(128000, config)
    assert recommendation is not None
    assert "非强制" in recommendation.message


def test_config_validation_fails_fast() -> None:
    with pytest.raises(ValueError):
        AcpConfig(model_context_limit=0)
    with pytest.raises(ValueError):
        AcpConfig(nudge_max_context_limit_pct=1.5)
    with pytest.raises(ValueError):
        AcpConfig(nudge_min_context_limit_pct=0.9, nudge_max_context_limit_pct=0.7)
    with pytest.raises(ValueError):
        AcpConfig(search_top_k=0)
    with pytest.raises(ValueError):
        AcpConfig(search_ngram_size=1)
    with pytest.raises(ValueError):
        AcpConfig(max_tier=1)