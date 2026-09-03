"""Admin-editable runtime assembly, persisted as a small JSON file.

Environment/settings give the deployment its *defaults*; the admin page lets an
operator change the assembly at runtime (which engine, which security profile,
which modules are enabled, which extra module packages to load). Those choices
are saved here and applied on top of ``Settings`` when the runtime is
(re)started, so they survive a process restart and never require editing
``.env``.

    saved   – what the admin last wrote (``load_overrides``)
    applied – what the running registry was built from (tracked by assembly)
    pending – saved != applied  → "restart to apply"
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ENGINES = ("dsh", "legacy")
SECURITY_PROFILES = ("OSS_LOCAL", "BUSINESS_BASE")
CONFIG_FILENAME = "staffdeck-runtime.json"


@dataclass
class RuntimeOverrides:
    engine: str = "legacy"                      # "dsh" (Harness v3) | "legacy" (Harness v2)
    security_profile: str = "OSS_LOCAL"
    disabled_modules: list[str] = field(default_factory=list)
    extra_modules: list[str] = field(default_factory=list)   # "pkg.mod:register" specs
    updated_at: str | None = None
    updated_by: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def normalized(self) -> "RuntimeOverrides":
        return RuntimeOverrides(
            engine=self.engine if self.engine in ENGINES else "legacy",
            security_profile=self.security_profile if self.security_profile in SECURITY_PROFILES else "OSS_LOCAL",
            disabled_modules=sorted({str(x).strip() for x in self.disabled_modules if str(x).strip()}),
            extra_modules=[str(x).strip() for x in self.extra_modules if str(x).strip()],
            updated_at=self.updated_at, updated_by=self.updated_by,
        )

    def same_assembly(self, other: "RuntimeOverrides" | None) -> bool:
        if other is None:
            return False
        a, b = self.normalized(), other.normalized()
        return (a.engine, a.security_profile, a.disabled_modules, a.extra_modules) == (b.engine, b.security_profile, b.disabled_modules, b.extra_modules)


def _split(value: Any) -> list[str]:
    return [x.strip() for x in str(value or "").split(",") if x.strip()]


def config_path(settings: Any) -> Path:
    explicit = str(getattr(settings, "dsh_runtime_config_path", "") or os.environ.get("STAFFDECK_DSH_RUNTIME_CONFIG", "") or "")
    if explicit:
        return Path(explicit).expanduser()
    home = str(getattr(settings, "dsh_home", "") or "")
    base = Path(home).expanduser() if home else Path.cwd()
    return base / CONFIG_FILENAME


def defaults_from_settings(settings: Any) -> RuntimeOverrides:
    """What the deployment would run with if the admin never touched anything."""

    return RuntimeOverrides(
        engine="dsh" if bool(getattr(settings, "dsh_enabled", False)) else "legacy",
        security_profile=str(getattr(settings, "security_profile", "OSS_LOCAL") or "OSS_LOCAL"),
        disabled_modules=_split(getattr(settings, "dsh_disabled_modules", "")),
        extra_modules=_split(getattr(settings, "dsh_modules", "")),
    ).normalized()


def load_overrides(settings: Any) -> RuntimeOverrides:
    path = config_path(settings)
    if not path.exists():
        return defaults_from_settings(settings)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return defaults_from_settings(settings)
    base = defaults_from_settings(settings)
    return RuntimeOverrides(
        engine=str(raw.get("engine") or base.engine),
        security_profile=str(raw.get("security_profile") or base.security_profile),
        disabled_modules=list(raw.get("disabled_modules") or []),
        extra_modules=list(raw.get("extra_modules") or []),
        updated_at=raw.get("updated_at"),
        updated_by=raw.get("updated_by"),
    ).normalized()


def save_overrides(settings: Any, overrides: RuntimeOverrides, *, by: str | None = None) -> RuntimeOverrides:
    ov = overrides.normalized()
    ov.updated_at = datetime.now(timezone.utc).isoformat()
    ov.updated_by = by
    path = config_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(ov.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return ov


def apply_overrides(settings: Any, overrides: RuntimeOverrides) -> None:
    """Project the overrides onto the live ``Settings`` object.

    Every consumer (``AgentLoop._open_engine``, the registry, the security
    profile, the DSH runtime) already reads these fields, so mutating the
    cached settings is the one place that makes a restart pick everything up.
    """

    ov = overrides.normalized()
    for name, value in (
        ("dsh_enabled", ov.engine == "dsh"),
        ("security_profile", ov.security_profile),
        ("dsh_disabled_modules", ",".join(ov.disabled_modules)),
        ("dsh_modules", ",".join(ov.extra_modules)),
    ):
        try:
            setattr(settings, name, value)
        except (AttributeError, TypeError, ValueError):
            object.__setattr__(settings, name, value)
