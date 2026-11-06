# Create review Sessions from GitHub webhooks

English | [中文](github-review.zh.md)

This opt-in overlay adds a signed GitHub endpoint to `dsh web`. When a pull request in the configured repository changes from draft to ready for review, the rule creates a titled root Session under the repository's Web Workspace and starts a read-only review prompt.

## Prerequisites

- A local checkout that DSH may register as a Web Workspace.
- A high-entropy GitHub webhook secret available through the `DSH_GITHUB_WEBHOOK_SECRET` credential reference.
- A TLS reverse proxy or tunnel that can forward one public URL to the loopback listener.
- GitHub webhook subscription to the Pull requests event with content type `application/json`.

The overlay defaults the Workspace to the launch directory and the listener to `127.0.0.1:3081`. Override them with `DSH_GITHUB_REVIEW_WORKSPACE` and `DSH_GITHUB_WEBHOOK_PORT`.

## Start DSH

Generate a secret and retain the same value across restarts:

```sh
export DSH_GITHUB_WEBHOOK_SECRET="$(openssl rand -hex 32)"
printf '%s\n' "$DSH_GITHUB_WEBHOOK_SECRET"
```

From a development checkout:

```sh
export DSH_GITHUB_REVIEW_WORKSPACE=/path/to/deepseek-harness
pnpm dsh web --patch apps/cli/config/examples/github-review/cordis.yml
```

An installed DSH uses the same overlay through an absolute path:

```sh
dsh web --patch /absolute/path/to/github-review/cordis.yml
```

For a permanent profile, place `github-ready-review-rule.mjs` beside `$DSH_HOME/profiles/web/cordis.patch.yml`, append the rows from `cordis.yml` to that patch, and start with `dsh web`. The shipped CLI already contains both webhook packages; the overlay alone activates them.

## Expose the dedicated endpoint

The main Web UI and `/api` remain on port 3080. The overlay mounts a second WebServer in an isolated realm; only `POST /github` is registered there, and every other path returns `404`.

A Caddy configuration can expose only that listener:

```caddyfile
hooks.example.com {
  route {
    @github path /github
    reverse_proxy @github 127.0.0.1:3081
    respond 404
  }
}
```

Configure GitHub with:

```text
Payload URL:  https://hooks.example.com/github
Content type: application/json
Secret:       DSH_GITHUB_WEBHOOK_SECRET value
Events:       Pull requests
Active:       yes
```

## Rule behavior

The rule accepts only source `primary-github`, repository `deepseek-harness/deepseek-harness`, event `pull_request`, and action `ready_for_review`. It passes the exact head SHA plus selected PR fields to the review prompt, labeling the JSON as untrusted metadata and forbidding file, branch, PR, or GitHub mutation.

The Session request selects the `standard` agent preset and `read-only` permission preset. `workspacePath` is canonicalized through `WorkspaceRegistry.create()`, so the first matching delivery creates the Web Workspace when absent and later deliveries reuse it.

The HTTP response is intentionally weaker than the Agent outcome: `202` means the signature and JSON were accepted and rule calls were scheduled in memory. It does not mean this rule matched or that a Session was created.

## Programmatic extensions

`run()` is ordinary trusted JavaScript. A deployment can query an internal policy service before returning a Session request:

```js
const response = await fetch('https://policy.internal/pr-review', {
  method: 'POST',
  headers: { 'content-type': 'application/json' },
  body: JSON.stringify({ repository: payload.repository.full_name }),
  signal,
})
if (!response.ok || (await response.json()).automaticReview !== true) return null
```

It can also map repositories to different local paths:

```js
const workspacePath = {
  'deepseek-harness/deepseek-harness': '/path/to/deepseek-harness',
  'deepseek-harness/dsh-sdk': '/path/to/dsh-sdk',
}[payload.repository.full_name]
if (workspacePath === undefined) return null
```

## Delivery semantics

The webhook runtime stores no delivery or execution state. Repeated delivery runs the rule and may create another Session. A crash loses rule calls that have not admitted their prompt. After prompt admission, the ordinary Session log, persistence, Workspace, and Agent lifecycle own the work.

The webhook secret authenticates inbound GitHub data only. It grants neither rule code nor the created Agent outbound GitHub access; configure that authority separately when a rule or Agent needs it.
