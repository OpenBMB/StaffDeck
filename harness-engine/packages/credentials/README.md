---
description: "Package map for the credential capability family: the credential-reference seam, the environment-and-file provider, the authorization flow registry, and how references keep secret values out of configuration."
kind: "package-group"
---

# credentials/ — credentials and authorization

English | [中文](README.zh.md)

## Summary

The `credentials/` group manages the secret values your configuration refers to by name: store an API key once, reference it from settings or `cordis.yml`, and rotate it without editing any configuration file. It provides the runtime part of the product that stores and looks up secrets (`credentials/`), the default on-machine credential file (`credentials-local/`), and the authorization flow registry (`authorization/`) for credentials that cannot be configured, because getting one means asking a human. A rotated key reaches the very next model request, and a per-run environment override (`DEEPSEEK_API_KEY=… dsh`) always wins over stored values. Secret values never enter configuration files you sync or render — only their names do, and the local file is readable by the same OS user, not by others.

## Table of Contents

- [Packages](#packages)
- [Related documentation](#related-documentation)
- [Dev Note](#dev-note)

-----

<a id="packages"></a>
## Packages

Three packages provide the credential feature: one stores, looks up, and removes secrets at runtime while configuration only names them; the second is the default on-machine store; the third lets plugins obtain credentials that have to be asked for. Their READMEs cover day-to-day use; the subsystem reference owns the exhaustive contracts.

| Package | Role | ctx key |
|---|---|---|
| [`credentials/`](credentials/README.md) | Store, look up, and remove secrets at runtime while configuration only names them | `ctx.credentials` |
| [`credentials-local/`](credentials-local/README.md) | The default on-machine store: a private YAML file, environment overrides win | registers `ctx.credentials` |
| [`authorization/`](authorization/README.md) | Plugin-owned flows that obtain a credential by asking a human | `ctx.authorization` |

-----

<a id="related-documentation"></a>
## Related documentation

Start with the subsystem reference for the shared vocabulary, then the capability-seam table and the configuration surface of the local store.

- [Credentials subsystem reference](../../docs/subsystems/credentials.md) — `CredentialRef` and `CredentialKey`, per-operation resolution, UI-safe `CredentialInfo`, authorization flows, and the generated cordis surface.
- [Capability seams](../../docs/capability-seams.md) — the Service Definition / Service Provider / Consumer split this family follows.
- [Generated configuration catalog](../../docs/config-catalog.md#deepseek-aidsh-credentials-local) — every accepted field of the local store.

<a id="dev-note"></a>
## Dev Note

<details>
<summary>Working context for maintainers — click to expand</summary>

None.

</details>
