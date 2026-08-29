# Research: Codex Subscription Models

## Decision: Use Codex app-server's managed ChatGPT login

- **Decision**: Use the local `codex app-server` JSON-RPC protocol for account state, browser login, cancellation, logout and model turns.
- **Rationale**: The official app-server protocol exposes `account/read`, `account/login/start` with `type: "chatgpt"`, `account/login/cancel`, `account/logout`, and managed token refresh. The credential lifecycle remains owned by Codex rather than StaffDeck.
- **Alternatives considered**:
  - Calling OpenAI API endpoints with a ChatGPT subscription: rejected because this would be API-billed traffic and fails the requested subscription reuse behavior.
  - Importing access tokens into StaffDeck: rejected because the official schema labels `chatgptAuthTokens` unstable and internal-only, and it would create an unacceptable secret storage path.
  - Reusing the existing A2A `codex exec` connector: rejected because it is a durable coding-agent A2A endpoint, not a model-provider/account runtime.

## Decision: Keep a separate authentication mode and runtime protocol

- **Decision**: Add `auth_mode` (`api_key` or `chatgpt_subscription`) to model configurations, and bind subscription entries to an internal `codex_app_server` protocol.
- **Rationale**: Authentication choice is a user-visible security boundary; protocol identifies the runtime adapter. Separating them keeps old direct providers untouched while making the subscription path explicit during validation, fingerprinting and request dispatch.
- **Alternatives considered**:
  - Infer subscription from a special Base URL: rejected because it is error-prone and leaks an implementation detail into the user-facing configuration.
  - Treat subscription as an API Key: rejected because no API key exists in this mode and it would cause accidental decryption, validation and logging paths.

## Decision: Launch browser authorization inside the local backend

- **Decision**: The login endpoint asks the local backend to open Codex's authorization URL in the default browser, then returns only sanitized status for the UI to poll.
- **Rationale**: Users do not have to copy a command, URL, callback or authorization code. The API never returns the raw authorization URL, which can embed callback information.
- **Alternatives considered**:
  - Return `authUrl` to the frontend: rejected because it exposes callback-bearing protocol data and burdens the user with an implementation detail.
  - Device-code flow: deferred because it requires showing and copying a user code, contrary to the requested browser-confirmation experience.

## Decision: Make each subscription request an ephemeral, constrained Codex turn

- **Decision**: For each StaffDeck model request, create an ephemeral app-server thread with StaffDeck's system instructions, `read-only` sandbox, `never` approval policy, no writable roots, and a temporary safe working directory. Stream only agent-message deltas back to the existing LLM interface.
- **Rationale**: StaffDeck already supplies conversation context to its LLM requests, so durable Codex thread history is unnecessary. Ephemeral threads avoid persisting StaffDeck prompts into Codex session history and the restrictive turn settings avoid granting model-driven file or approval access.
- **Alternatives considered**:
  - Use durable Codex threads: rejected because it duplicates StaffDeck conversation storage and increases retention surface.
  - Use unrestricted or workspace-write Codex turns: rejected because model use must not gain coding-agent filesystem permissions.

## Decision: Preserve existing verification semantics

- **Decision**: Subscription configs run the existing text, stream and JSON verification probes through the new driver before becoming trusted, enabled or default.
- **Rationale**: The rest of StaffDeck already assumes verified model configurations; this preserves default-model and tenant isolation guarantees in both modes.
- **Alternatives considered**:
  - Mark a configuration verified immediately after login: rejected because a login does not prove the selected model is available.

## Runtime compatibility note

The local development machine reports `codex-cli 0.150.1` and supports `codex app-server`. Its generated schema confirms the browser `chatgpt` login variant, thread-level `developerInstructions`, `ephemeral`, `sandbox: "read-only"`, and turn-level `sandboxPolicy: {"type":"readOnly"}`/`approvalPolicy: "never"` fields. Production code should handle a missing or incompatible `codex` executable as an unavailable subscription runtime, not fall back to direct API calls.
