from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast

from app.harness.errors import HarnessExecutionError

SANDBOX_WORKSPACE = "/workspace"
SandboxBackend = Literal["srt", "bubblewrap"]


@dataclass(frozen=True)
class SandboxExecutionContext:
    """Own the host-to-sandbox path contract for one process execution."""

    host_workspace: Path
    backend: SandboxBackend

    def __post_init__(self) -> None:
        if self.backend not in {"srt", "bubblewrap"}:
            raise HarnessExecutionError("SANDBOX_BACKEND_INVALID", "Unknown sandbox backend.")
        root = Path(self.host_workspace).resolve(strict=True)
        if not root.is_dir():
            raise HarnessExecutionError(
                "INVALID_WORKSPACE", "Sandbox workspace must be a directory."
            )
        object.__setattr__(self, "host_workspace", root)

    def process_path(self, raw_path: str | Path) -> str:
        path = Path(raw_path)
        if not path.is_absolute():
            return str(raw_path)
        try:
            relative = path.resolve(strict=False).relative_to(self.host_workspace)
        except (OSError, ValueError):
            return str(raw_path)
        if self.backend != "bubblewrap":
            return str(self.host_workspace / relative)
        suffix = PurePosixPath(*relative.parts).as_posix()
        return SANDBOX_WORKSPACE if suffix == "." else f"{SANDBOX_WORKSPACE}/{suffix}"

    def map_argv(self, argv: Sequence[str]) -> list[str]:
        return [self.process_path(item) for item in argv]

    def map_env(
        self,
        env: Mapping[str, str] | None,
        *,
        path_keys: Sequence[str] = (),
    ) -> dict[str, str]:
        mapped = dict(env or {})
        for key in path_keys:
            value = mapped.get(key)
            if value:
                mapped[key] = self.process_path(value)
        return mapped

    def map_payload(
        self,
        payload: Mapping[str, Any],
        *,
        path_keys: Sequence[str] = (),
    ) -> dict[str, Any]:
        mapped = dict(payload)
        for key in path_keys:
            value = mapped.get(key)
            if isinstance(value, str) and value:
                mapped[key] = self.process_path(value)
        return mapped

    def host_cwd(self, requested: Path | None = None) -> Path:
        candidate = (requested or self.host_workspace).resolve(strict=True)
        try:
            candidate.relative_to(self.host_workspace)
        except ValueError as exc:
            raise HarnessExecutionError(
                "INVALID_WORKSPACE_CWD",
                "Sandbox working directory must stay inside the task workspace.",
            ) from exc
        if not candidate.is_dir():
            raise HarnessExecutionError(
                "INVALID_WORKSPACE_CWD",
                "Sandbox working directory is unavailable.",
            )
        return candidate

    def sandbox_cwd(self, requested: Path | None = None) -> str:
        return self.process_path(self.host_cwd(requested))


    @classmethod
    def create(cls, host_workspace: Path, backend: str) -> SandboxExecutionContext:
        return cls(host_workspace, cast(SandboxBackend, backend))


__all__ = ["SANDBOX_WORKSPACE", "SandboxBackend", "SandboxExecutionContext"]
