"""StaffDeck–Harness v3 Bridge: EngineHost, Harness v3 worker, capability MCP callback port, and the task-agent seam."""

from staffdeck_harness.bridge.capability_mcp import ACTIVATION_HEADER, ActivationRegistry, CapabilityMcpServer
from staffdeck_harness.bridge.engine_host import HarnessV3Engine, EngineHost, get_runtime, reset_runtime
from staffdeck_harness.bridge.task_agent import HarnessV3Runtime, HarnessV3TaskAgent, HarnessV3TurnContext
from staffdeck_harness.bridge.worker import DISABLED_ROWS, HarnessV3Process, HarnessV3WorkerConfig, render_patch

__all__ = [
    "ACTIVATION_HEADER", "ActivationRegistry", "CapabilityMcpServer", "HarnessV3Engine", "EngineHost", "get_runtime",
    "reset_runtime", "HarnessV3Runtime", "HarnessV3TaskAgent", "HarnessV3TurnContext", "DISABLED_ROWS", "HarnessV3Process", "HarnessV3WorkerConfig",
    "render_patch",
]
