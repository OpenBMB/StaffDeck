"""StaffDeck × Harness v3 pluggable runtime.

This package is a parallel implementation that sits beside ``backend/app``. It
never mutates the legacy tree; ``backend/app`` is imported read-only for the
ORM, existing services, and the Harness v2 engine. The only legacy touch points are
two switches (``Settings.harness_v3_enabled`` and the ``AgentLoop.handle_turn`` entry
branch), both of which default to the legacy path.

Layering (see ``design-harness-v3-pluggable-runtime.md``):

- ``contracts``     Module SDK: manifests, bindings, invocations, receipts, hooks, PEP.
- ``security``      One ``SecurityProfile`` per deployment: ``OSS_LOCAL`` or ``BUSINESS_BASE``.
- ``composition``   ``StaffComposition`` projection, logical SOP slots, immutable snapshots.
- ``capabilities``  ``CapabilityHost`` + Guarded Facades + Invocation Ledger.
- ``interactions``  ``InteractionPipelineHost`` and the fixed hook plan.
- ``bridge``        ``EngineHost`` and the StaffDeck–Harness v3 Bridge over the official SDK.
- ``handoff``/``channels``/``events``  trusted hosts wrapping existing services.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
