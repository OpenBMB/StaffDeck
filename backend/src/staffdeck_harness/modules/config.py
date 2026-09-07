"""Admin-editable runtime assembly, persisted as a small JSON file.

Environment/settings give the deployment its *defaults*; the admin page lets an
operator change the assembly at runtime (which engine, which security profile,
which modules are enabled, which extra module packages to load, how to reach
the enterprise permission centre). Those choices are saved here and applied on
top of ``Settings`` when the runtime is (re)started, so they survive a process
restart and never require editing ``.env``.

    saved   – what the admin last wrote (``load_overrides``)
    applied – what the running registry was built from (tracked by assembly)
    pending – saved != applied  → "restart to apply"

``placements`` (module → taxonomy sub-module) is display-only and deliberately
excluded from the pending comparison. Secrets in the Base block are stored
encrypted with the application secret (``app.security.encryption``).
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

ENGINES = ("harness_v3",)
SECURITY_PROFILES = ("OSS_LOCAL", "BUSINESS_BASE")
CONFIG_FILENAME = "staffdeck-runtime.json"
SECRET_MASK = "••••••••"

_SECRET_FIELDS = ("decision_token", "control_token", "runtime_client_secret")


class InvalidConnection(ValueError):
    """Operator-facing validation error for the Base connection block."""


def _timeout(value: Any, lo: float, hi: float) -> float | None:
    if value in (None, ""):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError) as exc:
        raise InvalidConnection(f"超时时间必须是数字：{value!r}") from exc
    if f != f or f < lo or f > hi:  # NaN or out of range
        raise InvalidConnection(f"超时时间需在 {lo:g}–{hi:g} 秒之间：{value!r}")
    return f


def _host_allowed(host: str, scheme: str, allowlist: str) -> bool:
    import fnmatch
    import ipaddress

    patterns = [x.strip() for x in str(allowlist or "").split(",") if x.strip()]
    if "*" in patterns:
        return True
    host_l = host.lower().strip("[]")
    for pat in patterns:
        if fnmatch.fnmatch(host_l, pat.lower()):
            return True
    try:
        ip = ipaddress.ip_address(host_l)
        return ip.is_loopback or ip.is_private or ip.is_link_local
    except ValueError:
        return host_l in ("localhost",) or host_l.endswith(".localhost") or host_l.endswith(".internal") or host_l.endswith(".local")


def check_url_policy(value: str, settings: Any, *, field: str = "地址") -> None:
    """Deployment policy for admin-supplied service URLs (SSRF / secret-exfiltration guard)."""

    if not value:
        return
    import httpx

    url = httpx.URL(value)
    allowlist = str(getattr(settings, "base_url_allowlist", "") or "") if settings is not None else ""
    if not _host_allowed(url.host, url.scheme, allowlist):
        raise InvalidConnection(f"{field} {url.host} 不在允许范围内（默认只允许内网/本机地址；可通过 BASE_URL_ALLOWLIST 放开）")
    if url.scheme != "https":
        try:
            import ipaddress

            ip = ipaddress.ip_address(url.host.strip("[]"))
            private = ip.is_loopback or ip.is_private or ip.is_link_local
        except ValueError:
            private = url.host in ("localhost",) or url.host.endswith((".localhost", ".internal", ".local"))
        if not private and "*" not in allowlist:
            raise InvalidConnection(f"{field} 指向内网之外时必须使用 https://：{value}")


def validate_url(value: str, *, field: str = "地址") -> str:
    value = str(value or "").strip().rstrip("/")
    if not value:
        return ""
    import httpx

    try:
        url = httpx.URL(value)
    except Exception as exc:  # noqa: BLE001 — httpx.InvalidURL is not an HTTPError
        raise InvalidConnection(f"{field}不是合法的 URL：{value}") from exc
    if url.scheme not in ("http", "https") or not url.host:
        raise InvalidConnection(f"{field}必须是 http(s):// 开头的完整地址：{value}")
    return value


def _encrypt(value: str) -> str:
    if not value:
        return ""
    from app.security.encryption import encrypt_secret

    return encrypt_secret(value)


def _decrypt(value: str) -> str:
    if not value:
        return ""
    from app.security.encryption import decrypt_secret

    try:
        return decrypt_secret(value)
    except Exception:  # noqa: BLE001 — a rotated APP_SECRET must not brick the admin page
        return ""


@dataclass
class BaseConnection:
    """How to reach the enterprise permission centre (Base). Empty fields fall back to env settings."""

    authz_url: str = ""
    decision_token: str = ""
    control_token: str = ""
    timeout_seconds: float | None = None
    pending_timeout_seconds: float | None = None
    identity_internal_url: str = ""
    runtime_client_id: str = ""
    runtime_client_secret: str = ""
    workload_audience: str = ""
    last_test_ok: bool | None = None
    last_test_at: str | None = None

    _SETTINGS_FIELDS = {
        "authz_url": "base_authz_url",
        "decision_token": "base_authz_decision_token",
        "control_token": "base_authz_control_token",
        "timeout_seconds": "base_authz_timeout_seconds",
        "pending_timeout_seconds": "base_authz_pending_timeout_seconds",
        "identity_internal_url": "base_identity_internal_url",
        "runtime_client_id": "base_identity_runtime_client_id",
        "runtime_client_secret": "base_identity_runtime_client_secret",
        "workload_audience": "base_workload_identity_audience",
    }

    def normalized(self) -> "BaseConnection":
        return BaseConnection(
            authz_url=validate_url(self.authz_url, field="权限中心地址"),
            decision_token=str(self.decision_token or "").strip(),
            control_token=str(self.control_token or "").strip(),
            timeout_seconds=_timeout(self.timeout_seconds, 0.5, 30.0),
            pending_timeout_seconds=_timeout(self.pending_timeout_seconds, 0.0, 30.0),
            identity_internal_url=validate_url(self.identity_internal_url, field="身份中心地址"),
            runtime_client_id=str(self.runtime_client_id or "").strip(),
            runtime_client_secret=str(self.runtime_client_secret or "").strip(),
            workload_audience=str(self.workload_audience or "").strip(),
            last_test_ok=self.last_test_ok, last_test_at=self.last_test_at,
        )

    def is_empty(self) -> bool:
        return not any(getattr(self, f) for f in self._SETTINGS_FIELDS)

    def to_stored(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in self._SETTINGS_FIELDS:
            v = getattr(self, f)
            if f in _SECRET_FIELDS:
                out[f"{f}_enc"] = _encrypt(v)
            else:
                out[f] = v
        out["last_test_ok"] = self.last_test_ok
        out["last_test_at"] = self.last_test_at
        return out

    @classmethod
    def from_stored(cls, raw: Mapping[str, Any] | None) -> "BaseConnection":
        raw = dict(raw or {})
        kw: dict[str, Any] = {}
        for f in cls._SETTINGS_FIELDS:
            if f in _SECRET_FIELDS:
                kw[f] = _decrypt(str(raw.get(f"{f}_enc") or ""))
            else:
                kw[f] = raw.get(f, "" if f not in ("timeout_seconds", "pending_timeout_seconds") else None)
        kw["last_test_ok"] = raw.get("last_test_ok")
        kw["last_test_at"] = raw.get("last_test_at")
        return cls(**kw).normalized()

    def effective(self, settings: Any) -> "BaseConnection":
        """Admin value wins; empty falls back to the environment setting."""

        merged = {}
        for f, sname in self._SETTINGS_FIELDS.items():
            v = getattr(self, f)
            if v in ("", None):
                v = env_value(settings, sname) if settings is not None else None
                if v == "":
                    v = None if f in ("timeout_seconds", "pending_timeout_seconds") else ""
            merged[f] = v
        return BaseConnection(**merged, last_test_ok=self.last_test_ok, last_test_at=self.last_test_at).normalized()

    def masked(self, settings: Any = None) -> dict[str, Any]:
        """For the admin API: never return secrets, but say whether they are set and where they come from."""

        eff = self.effective(settings) if settings is not None else self
        out: dict[str, Any] = {}
        for f, sname in self._SETTINGS_FIELDS.items():
            admin_value = getattr(self, f)
            eff_value = getattr(eff, f)
            source = "admin" if admin_value not in ("", None) else ("env" if eff_value not in ("", None) else "none")
            if f in _SECRET_FIELDS:
                out[f] = SECRET_MASK if eff_value else ""
                out[f"has_{f}"] = bool(eff_value)
            else:
                out[f] = eff_value if eff_value is not None else ""
            out[f"{f}_source"] = source
        out["last_test_ok"] = self.last_test_ok
        out["last_test_at"] = self.last_test_at
        out["configured"] = bool(eff.authz_url and eff.decision_token)
        return out

    def merge_update(self, patch: Mapping[str, Any]) -> "BaseConnection":
        """Apply an admin edit: missing key = keep, ``null``/"" = clear, masked secret = keep."""

        data = asdict(self)
        for f in self._SETTINGS_FIELDS:
            if f not in patch:
                continue
            v = patch[f]
            if v is None:
                data[f] = None if f in ("timeout_seconds", "pending_timeout_seconds") else ""
                continue
            if f in _SECRET_FIELDS and str(v) == SECRET_MASK:
                continue
            data[f] = v
        return BaseConnection(**data).normalized()

    def signature(self) -> tuple:
        return tuple(getattr(self, f) for f in self._SETTINGS_FIELDS)


@dataclass
class RuntimeOverrides:
    engine: str = "harness_v3"
    security_profile: str = "OSS_LOCAL"
    disabled_modules: list[str] = field(default_factory=list)
    extra_modules: list[str] = field(default_factory=list)   # "pkg.mod:register" specs
    placements: dict[str, str] = field(default_factory=dict)  # module_id -> taxonomy sub id (display only)
    base: BaseConnection = field(default_factory=BaseConnection)
    updated_at: str | None = None
    updated_by: str | None = None

    def to_dict(self, *, settings: Any = None) -> dict[str, Any]:
        """API shape: secrets are masked (see ``BaseConnection.masked``); use ``to_stored`` for disk."""

        d = asdict(self)
        d["base"] = self.base.masked(settings)
        return d

    def to_stored(self) -> dict[str, Any]:
        d = self.to_dict()
        d["base"] = self.base.to_stored()
        return d

    def normalized(self) -> "RuntimeOverrides":
        placements = {}
        for k, v in list(self.placements.items())[:500]:
            k2, v2 = str(k).strip(), str(v).strip()
            if k2 and v2:
                placements[k2] = v2
        return RuntimeOverrides(
            engine="harness_v3",  # migrate saved legacy selections without changing business data
            security_profile=self.security_profile if self.security_profile in SECURITY_PROFILES else "OSS_LOCAL",
            disabled_modules=sorted({str(x).strip() for x in self.disabled_modules if str(x).strip()}),
            extra_modules=[str(x).strip() for x in self.extra_modules if str(x).strip()],
            placements=dict(sorted(placements.items())),
            base=self.base.normalized(),
            updated_at=self.updated_at, updated_by=self.updated_by,
        )

    def same_assembly(self, other: "RuntimeOverrides" | None) -> bool:
        """Placements never count; the Base connection counts only when the enterprise profile is involved."""

        if other is None:
            return False
        a, b = self.normalized(), other.normalized()
        core_equal = (a.engine, a.security_profile, a.disabled_modules, a.extra_modules) == (b.engine, b.security_profile, b.disabled_modules, b.extra_modules)
        if not core_equal:
            return False
        if "BUSINESS_BASE" in (a.security_profile, b.security_profile):
            return a.base.signature() == b.base.signature()
        return True


def _split(value: Any) -> list[str]:
    return [x.strip() for x in str(value or "").split(",") if x.strip()]


_ENV_FIELDS = ("harness_v3_enabled", "security_profile", "harness_disabled_modules", "harness_modules", *BaseConnection._SETTINGS_FIELDS.values())
_env_snapshot: dict[str, Any] | None = None


def snapshot_env(settings: Any, *, force: bool = False) -> dict[str, Any]:
    """Remember the deployment (env/.env) values once, before any assembly mutates ``settings``.

    Every "fall back to the environment" decision reads this snapshot, never the
    live ``Settings`` object — otherwise a value applied by an admin would pose
    as the environment default forever.
    """

    global _env_snapshot
    if _env_snapshot is None or force:
        _env_snapshot = {name: getattr(settings, name, None) for name in _ENV_FIELDS}
    return _env_snapshot


def env_value(settings: Any, name: str) -> Any:
    snap = snapshot_env(settings)
    return snap.get(name) if name in snap else getattr(settings, name, None)


def reset_env_snapshot() -> None:
    global _env_snapshot
    _env_snapshot = None


def config_path(settings: Any) -> Path:
    explicit = str(getattr(settings, "harness_runtime_config_path", "") or os.environ.get("STAFFDECK_HARNESS_RUNTIME_CONFIG", "") or "")
    if explicit:
        return Path(explicit).expanduser()
    home = str(getattr(settings, "harness_v3_home", "") or "")
    base = Path(home).expanduser() if home else Path.cwd()
    return base / CONFIG_FILENAME


def defaults_from_settings(settings: Any) -> RuntimeOverrides:
    """What the deployment would run with if the admin never touched anything."""

    return RuntimeOverrides(
        engine="harness_v3",
        security_profile=str(env_value(settings, "security_profile") or "OSS_LOCAL"),
        disabled_modules=_split(env_value(settings, "harness_disabled_modules")),
        extra_modules=_split(env_value(settings, "harness_modules")),
    ).normalized()


def _safe_base(raw: Any) -> BaseConnection:
    try:
        return BaseConnection.from_stored(raw if isinstance(raw, dict) else None)
    except InvalidConnection:
        return BaseConnection()


def load_overrides(settings: Any) -> RuntimeOverrides:
    path = config_path(settings)
    if not path.exists():
        return defaults_from_settings(settings)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return defaults_from_settings(settings)
    base = defaults_from_settings(settings)
    placements = raw.get("placements")
    return RuntimeOverrides(
        engine=str(raw.get("engine") or base.engine),
        security_profile=str(raw.get("security_profile") or base.security_profile),
        disabled_modules=list(raw.get("disabled_modules") or []),
        extra_modules=list(raw.get("extra_modules") or []),
        placements=dict(placements) if isinstance(placements, dict) else {},
        base=_safe_base(raw.get("base")),
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
    tmp.write_text(json.dumps(ov.to_stored(), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return ov


def settings_updates(settings: Any, overrides: RuntimeOverrides) -> dict[str, Any]:
    """The ``Settings`` fields an assembly projects, as a dict (used by apply and by dry-runs)."""

    ov = overrides.normalized()
    updates: dict[str, Any] = {
        "harness_v3_enabled": ov.engine == "harness_v3",
        "security_profile": ov.security_profile,
        "harness_disabled_modules": ",".join(ov.disabled_modules),
        "harness_modules": ",".join(ov.extra_modules),
    }
    eff = ov.base.effective(settings)
    for f, sname in BaseConnection._SETTINGS_FIELDS.items():
        v = getattr(eff, f)
        if v is None:
            env = env_value(settings, sname)
            v = env if env not in (None, "") else (3.0 if f in ("timeout_seconds", "pending_timeout_seconds") else "")
        updates[sname] = v
    return updates


def apply_overrides(settings: Any, overrides: RuntimeOverrides) -> None:
    """Project the overrides onto the live ``Settings`` object.

    Every consumer (``AgentLoop._open_engine``, the registry, the security
    profile, the Harness v3 runtime) already reads these fields, so mutating the
    cached settings is the one place that makes a restart pick everything up.
    """

    for name, value in settings_updates(settings, overrides).items():
        try:
            setattr(settings, name, value)
        except (AttributeError, TypeError, ValueError):
            object.__setattr__(settings, name, value)


def settings_view(settings: Any, overrides: RuntimeOverrides) -> Any:
    """A non-mutating copy of ``settings`` with the assembly applied (for dry runs)."""

    updates = settings_updates(settings, overrides)
    if hasattr(settings, "model_copy"):
        return settings.model_copy(update=updates)
    import copy

    view = copy.copy(settings)
    for k, v in updates.items():
        setattr(view, k, v)
    return view
