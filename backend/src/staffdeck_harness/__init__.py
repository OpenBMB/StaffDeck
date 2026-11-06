"""StaffDeck × Harness v3 pluggable runtime.

This package owns modular runtime implementations while reusing ``backend/app``
ORM and persistence services. The v2 entry is a compatibility consumer of these
modules; SOP lifecycle no longer depends on AgentLoop's private state methods.

Layering (see ``design-harness-v3-pluggable-runtime.md``):

- ``contracts``     Module SDK: manifests, bindings, invocations, receipts, hooks, PEP.
- ``security``      One ``SecurityProfile`` per deployment: ``OSS_LOCAL`` or ``BUSINESS_BASE``.
- ``composition``   ``StaffComposition`` projection, logical SOP slots, immutable snapshots.
- ``capabilities``  ``CapabilityHost`` + Guarded Facades + Invocation Ledger.
- ``interactions``  ``InteractionPipelineHost`` and the fixed hook plan.
- ``sop``          SOP lifecycle, graph rules and resumable state behind ``SopRuntimePort``.
- ``bridge``        ``EngineHost`` and the StaffDeck–Harness v3 Bridge over the official SDK.
- ``handoff``/``channels``/``events``  trusted hosts wrapping existing services.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
