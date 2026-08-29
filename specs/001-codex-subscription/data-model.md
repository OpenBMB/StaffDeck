# Data Model: Codex Subscription Models

## Model configuration (existing entity, extended)

| Field | Type / values | Rules |
|---|---|---|
| `auth_mode` | `api_key`, `chatgpt_subscription` | Defaults to `api_key` so all existing rows remain compatible. Switching mode invalidates trust and disables the entry. |
| `api_protocol` | existing direct protocols, `codex_app_server` | Direct models retain existing values. Subscription models always use `codex_app_server`; users do not select it manually. |
| `api_key_encrypted` | encrypted direct key or empty | Required for `api_key`; must be empty for subscriptions. Raw OAuth data is never stored here. |
| `base_url` | optional URL | Validated for `api_key`; cleared for subscriptions. |
| `extra_body_json`, `protocol_options_json` | object | Existing direct semantics; empty for subscriptions. |
| `model`, `name`, `enabled`, `is_default`, verification fields | existing fields | Retained for both modes. A subscription config is trusted only after existing verification probes succeed. |

## Subscription account state (process-local, never persisted by StaffDeck)

| Field | Type / values | Rules |
|---|---|---|
| `status` | `connected`, `pending`, `requires_login`, `unavailable` | Safe, user-facing status derived from Codex account state and local process state. |
| `plan_type` | optional string | Non-secret account plan label supplied by Codex when available. No email or account identifier is exposed. |
| pending login identifier | process-private opaque value | Retained only long enough to cancel the active browser login; never returned to the browser or database. |

## Relationships and lifecycle

One local Codex subscription state can be referenced by zero or more model configurations. Each configuration has its own name, model, validation, enablement and default state. Removing or disabling a configuration does not sign out the local Codex account. Explicit sign-out changes the shared account state, so all subscription model validations fail until the user reconnects and retests them.
