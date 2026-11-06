# AGENTS.md — Experimental packages

These rules supplement the [package rules](../AGENTS.md). The [experimental Agent Teams package decision](../../.agents/notes/implemented/architecture/2026-08-18-experimental-agent-teams-packages.md) owns the rationale.

- A package belongs here only when its complete public contract is experimental or internal-only. An experimental option inside a release package stays with its owning product role.
- Every package here uses the `@deepseek-ai/dsh-experimental-*` npm prefix, sets `private: true`, and omits `publishConfig`; the workspace constraints gate enforces these declarations and the dsh release family excludes this directory.
- Release packages and apps must not name packages here in `dependencies`, `optionalDependencies`, or `peerDependencies`. Experimental packages may depend on release packages and each other. Tests may use experimental packages through `devDependencies`; examples may load them explicitly.
- Experimental status does not relax engineering, security, documentation, lifecycle, testing, invariant, or snapshot requirements.
- Promotion moves a package to its product-role group and removes `experimental-` from its npm name. Update every import and configuration row atomically, then review its public contract, limitations, test evidence, release payload, runtime dependents, and named stable owner.
