---
name: staffdeck-api-manage-sops
description: Manage a StaffDeck employee's SOP lifecycle through Open API v1, including listing published SOPs and drafts, generating from source text, rewriting selected fields, replacing or JSON-patching drafts, validating, publishing, archiving, comparing versions, and creating rollback drafts. Use for controlled SOP authoring and release automation.
---

# Manage StaffDeck SOPs

## Preserve the draft boundary

Generate, rewrite, structured create, patch, and rollback as drafts. Never publish a draft unless the user explicitly requests publication. Unpublished drafts must not be treated as runtime-active SOPs.

## Workflow

1. Verify the credential can write the target employee and has the required `sops:*` scopes.
2. List current published SOPs and drafts with `GET /agents/{agent_id}/sops`.
3. Choose one authoring path from [references/sop-api.md](references/sop-api.md): structured create, generate, rewrite, replace, or JSON Patch.
4. For asynchronous generate or rewrite, poll the returned `/jobs/{job_id}` and read `/jobs/{job_id}/result` only after success.
5. Preserve the returned `draft_id`, `sop_id`, and `ETag`.
6. Validate the exact draft before publication.
7. Publish only the validated draft ID requested by the user.
8. Re-read the published SOP and report its version.

## Concurrency and edits

- Send `If-Match` on draft replacement and JSON Patch. On `412`, fetch the latest draft and reconcile; never overwrite blindly.
- Use `application/json-patch+json` for PATCH operations.
- Keep `skill_id` immutable.
- Prefer targeted rewrite paths or JSON Patch for small changes; use full replacement only when the complete SkillCard is authoritative.
- Treat rollback as creation of a new draft, not immediate runtime rollback.

## Capability safety

Before publishing, confirm every referenced skill, knowledge base, and tool belongs to the employee's available capability set. A `sop_specific` capability must be explicitly referenced by an SOP node to become executable.
