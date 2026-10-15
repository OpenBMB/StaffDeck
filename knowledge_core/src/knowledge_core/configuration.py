"""Immutable startup configuration; optional modules are never auto-enabled."""
from dataclasses import dataclass, field
import json
from pathlib import Path
from types import MappingProxyType
from typing import Mapping


MODULE_NAMES = ("authentication", "spaces", "catalog", "versions", "concepts", "discovery", "publication")
DEPENDENCIES = {"versions": ("catalog",), "publication": ("discovery",)}


@dataclass(frozen=True)
class Configuration:
    preset: str = "minimal"
    modules: Mapping[str, bool] = field(default_factory=dict)

    def __post_init__(self):
        if self.preset not in {"minimal", "full"}:
            raise ValueError("preset must be minimal or full")
        unknown = set(self.modules) - set(MODULE_NAMES)
        if unknown:
            raise ValueError("Unknown modules: " + ", ".join(sorted(unknown)))
        if any(type(value) is not bool for value in self.modules.values()):
            raise ValueError("Module values must be boolean")
        values = {name: self.preset == "full" and name != "authentication" for name in MODULE_NAMES}
        values.update(self.modules)
        for name, dependencies in DEPENDENCIES.items():
            if values[name]:
                for dependency in dependencies:
                    if not values[dependency]:
                        raise ValueError(f"Module {name} requires {dependency}; enable it explicitly")
        object.__setattr__(self, "modules", MappingProxyType(values))

    @classmethod
    def load(cls, path: Path):
        value = json.loads(path.read_text())
        if set(value) - {"preset", "modules", "protocol"}:
            raise ValueError("Unknown configuration fields")
        if value.get("protocol", "knowledge-config/v1") != "knowledge-config/v1":
            raise ValueError("Unsupported configuration protocol")
        return cls(value.get("preset", "minimal"), value.get("modules", {}))

    def enabled(self, name: str) -> bool:
        return self.modules[name]

    def as_dict(self):
        return {"protocol": "knowledge-config/v1", "preset": self.preset, "modules": dict(self.modules)}

    @property
    def full_contract(self):
        return all(self.enabled(name) for name in MODULE_NAMES if name != "authentication")
