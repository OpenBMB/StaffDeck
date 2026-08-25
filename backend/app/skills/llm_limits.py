from __future__ import annotations

from app.db.models import ModelConfig
from app.llm.model_config_resolver import snapshot_model_config


def skill_model_config(model_config: ModelConfig) -> ModelConfig:
    return snapshot_model_config(model_config)
