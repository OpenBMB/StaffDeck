"""Warm process pool for the Harness v3 engine.

Why a pool
----------
Booting one engine subprocess costs about a second, and a turn used to pay it *per TaskFrame*.
The MCP activation token is read from the environment when the engine loads its plugins, so a
process is welded to one token for its life. Instead of one process per frame, the pool keeps
warm processes and hands one out per **turn**: the token stays with the process, and the
``ActivationRegistry`` entry behind that token is *rebound* to whichever phase host must answer
capability/model calls right now (planning → each frame → reply). Between phases the entry
points at an idle placeholder that refuses tool calls, so a stale model request cannot reach a
capability it should not see.

Lifecycle
---------
- ``acquire(cfg, tenant_id)`` returns a ``PooledProcess`` (warm if one matches the tenant +
  model config, otherwise freshly started). The caller rebinds its activation and runs phases.
- ``release(proc)`` returns it to the tenant's warm set, or closes it when the set is full, the
  process died, or it exceeded its reuse budget. Releasing never blocks the turn.
- ``close_all()`` on runtime stop / restart.

Isolation
---------
A process is reused only within the same tenant *and* the same model route (model name +
thinking policy), because those are baked into the engine's profile patch at boot. Each turn
still gets a fresh engine *session* (``session_id`` is per turn); the process is shared, the
conversation is not.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from staffdeck_harness.bridge.worker import HarnessV3Process, HarnessV3WorkerConfig

logger = logging.getLogger(__name__)

DEFAULT_WARM_PER_KEY = 2
DEFAULT_MAX_REUSES = 200
DEFAULT_IDLE_TTL_SECONDS = 600.0


def pool_key(cfg: HarnessV3WorkerConfig, tenant_id: str) -> str:
    """Processes are interchangeable only when everything baked into the boot patch matches."""

    return "|".join([tenant_id, cfg.model, cfg.thinking or "", cfg.reasoning_effort or "", str(cfg.harness_v3_home)])


@dataclass
class PooledProcess:
    key: str
    token: str
    process: HarnessV3Process
    cwd: Path
    created_at: float = field(default_factory=time.monotonic)
    last_used_at: float = field(default_factory=time.monotonic)
    uses: int = 0

    @property
    def alive(self) -> bool:
        return self.process.alive


class ProcessPool:
    def __init__(self, *, warm_per_key: int = DEFAULT_WARM_PER_KEY, max_reuses: int = DEFAULT_MAX_REUSES, idle_ttl_seconds: float = DEFAULT_IDLE_TTL_SECONDS, factory: Callable[..., HarnessV3Process] | None = None):
        self._lock = threading.Lock()
        self._warm: dict[str, list[PooledProcess]] = {}
        self._checked_out: set[str] = set()
        self.warm_per_key = max(0, int(warm_per_key))
        self.max_reuses = max(1, int(max_reuses))
        self.idle_ttl_seconds = float(idle_ttl_seconds)
        self._factory = factory or (lambda cfg, **kw: HarnessV3Process(cfg, **kw))
        self.stats = {"hits": 0, "misses": 0, "closed": 0}

    # -- checkout ------------------------------------------------------------------------

    def acquire(self, cfg: HarnessV3WorkerConfig, tenant_id: str, *, mcp_url: str, cwd: Path, register: Callable[[str], None], factory: Callable[..., Any] | None = None) -> PooledProcess:
        """Return a process for this turn. ``register(token)`` is called for a *new* process before
        it boots so the MCP server knows the token the moment the engine connects."""

        key = pool_key(cfg, tenant_id)
        with self._lock:
            bucket = self._warm.get(key) or []
            now = time.monotonic()
            while bucket:
                cand = bucket.pop()
                if cand.alive and (now - cand.last_used_at) <= self.idle_ttl_seconds and cand.uses < self.max_reuses:
                    cand.uses += 1
                    cand.last_used_at = now
                    self._checked_out.add(cand.token)
                    self.stats["hits"] += 1
                    return cand
                self._close_quietly(cand)
            self.stats["misses"] += 1
        token = secrets.token_urlsafe(24)
        register(token)
        proc = (factory or self._factory)(cfg, activation_token=token, mcp_url=mcp_url, cwd=cwd)
        proc.start()
        pooled = PooledProcess(key=key, token=token, process=proc, cwd=cwd, uses=1)
        with self._lock:
            self._checked_out.add(token)
        return pooled

    def release(self, pooled: PooledProcess, *, on_close: Callable[[str], None]) -> None:
        """Return to the warm set, or close it (``on_close(token)`` lets the caller drop the activation)."""

        with self._lock:
            self._checked_out.discard(pooled.token)
            bucket = self._warm.setdefault(pooled.key, [])
            keep = pooled.alive and pooled.uses < self.max_reuses and len(bucket) < self.warm_per_key
            if keep:
                pooled.last_used_at = time.monotonic()
                bucket.append(pooled)
                return
        self._close_quietly(pooled)
        on_close(pooled.token)

    # -- housekeeping --------------------------------------------------------------------

    def close_all(self, *, on_close: Callable[[str], None] | None = None) -> None:
        with self._lock:
            items = [p for bucket in self._warm.values() for p in bucket]
            self._warm.clear()
        for p in items:
            self._close_quietly(p)
            if on_close:
                on_close(p.token)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "warm": sum(len(b) for b in self._warm.values()),
                "checked_out": len(self._checked_out),
                "keys": len(self._warm),
                **self.stats,
            }

    def _close_quietly(self, pooled: PooledProcess) -> None:
        try:
            pooled.process.close()
        except Exception:  # pragma: no cover
            logger.exception("closing pooled engine process failed")
        self.stats["closed"] += 1
