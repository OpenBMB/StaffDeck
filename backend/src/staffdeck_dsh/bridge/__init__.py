"""StaffDeck–DSH Bridge: EngineHost, DSH worker, capability MCP callback port, and the task-agent seam."""

from staffdeck_dsh.bridge.capability_mcp import ACTIVATION_HEADER, ActivationRegistry, CapabilityMcpServer
from staffdeck_dsh.bridge.engine_host import DshEngine, EngineHost, get_runtime, reset_runtime
from staffdeck_dsh.bridge.task_agent import DshRuntime, DshTaskAgent, DshTurnContext
from staffdeck_dsh.bridge.worker import DISABLED_ROWS, DshProcess, DshWorkerConfig, render_patch

__all__ = [
    "ACTIVATION_HEADER", "ActivationRegistry", "CapabilityMcpServer", "DshEngine", "EngineHost", "get_runtime",
    "reset_runtime", "DshRuntime", "DshTaskAgent", "DshTurnContext", "DISABLED_ROWS", "DshProcess", "DshWorkerConfig",
    "render_patch",
]
