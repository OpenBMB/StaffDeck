---
description: "Package map for the web access capability family: the search/fetch service, its provider backends, and the model-facing tools that consume them."
kind: "package-group"
---

# web/ — web access capability family

English | [中文](README.zh.md)

## Summary

The `web/` group gives the harness web access — searching the web and fetching URLs — through one provider-neutral service (`ctx.web`) and the backends and tools that use it. A deployment mounts one or more backends — Exa, Perplexity, or DeepSeek for search, anonymous HTTP(S) for fetch — and the service picks a usable provider per operation, so the model-facing tools stay stable while backends come and go. Six packages split the family: the `web/` service that owns provider selection and errors, three search backends, one fetch backend, and `tool-web/`, which exposes `web_search` and `web_fetch` to the model. The group owns web access only: no browsing or extraction, no per-URL policy, and each backend keeps its own resource caps. Search and fetch deliberately share one service so selection, cancellation, errors, and configuration have a single owner.

## Table of Contents

- [Packages](#packages)
- [Related documentation](#related-documentation)
- [Dev Note](#dev-note)

-----

<a id="packages"></a>
## Packages

Six packages play the web roles; the subsystem reference owns the exhaustive vocabulary and contracts.

| Package | Role | ctx key |
|---|---|---|
| [`web/`](web/README.md) | Search/fetch service: search and fetch URLs through interchangeable backends, one selection and error policy | `ctx.web` |
| [`web-search-exa/`](web-search-exa/README.md) | Searches the web through Exa | registers on `ctx.web` |
| [`web-search-perplexity/`](web-search-perplexity/README.md) | Searches the web through Perplexity | registers on `ctx.web` |
| [`web-search-deepseek/`](web-search-deepseek/README.md) | Searches the web through DeepSeek native search | registers on `ctx.web` |
| [`web-fetch-http/`](web-fetch-http/README.md) | Fetches public HTTP(S) pages anonymously | registers on `ctx.web` |
| [`tool-web/`](tool-web/README.md) | Exposes `web_search` and `web_fetch` to the model | registers on `ctx.tools` |

-----

<a id="related-documentation"></a>
## Related documentation

Start with the subsystem reference for the shared vocabulary, then the design decision behind the single provider-selection service.

- [Web subsystem](../../docs/subsystems/web.md) — the search/fetch requests and results, provider availability, `WebError`, and public-address enforcement.
- [Web capability seam decision](../../.agents/notes/implemented/architecture/2026-06-24-web-capability-seam.md) — why search and fetch share one provider-selection service.

<a id="dev-note"></a>
## Dev Note

<details>
<summary>Working context for maintainers — click to expand</summary>

None.

</details>
