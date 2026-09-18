# StaffDeck SOP HTTP Adapter

This directory contains an HTTP protocol adapter for PilotDeck. It imports
`staffdeck_harness.sop` and the existing StaffDeck task contracts from
`../backend`; it does not contain an SOP transition engine.

Build from the StaffDeck repository root:

```bash
docker build -f portable_sop/Dockerfile -t staffdeck-sop-runtime .
```

For local execution, install `backend` into an isolated environment and set:

```bash
PYTHONPATH=backend:backend/src:portable_sop/src python -m staffdeck_sop_runtime
```

The adapter's tests require the same backend dependency environment because
they exercise the real StaffDeck lifecycle and submission validator.
