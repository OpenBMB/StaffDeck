from __future__ import annotations

from app.db.models import ModelConfig
from app.llm.model_config_resolver import snapshot_model_config


SKILL_MAX_OUTPUT_TOKENS = 8192


def skill_model_config(model_config: ModelConfig) -> ModelConfig:
    return snapshot_model_config(model_config, min_output_tokens=SKILL_MAX_OUTPUT_TOKENS)
