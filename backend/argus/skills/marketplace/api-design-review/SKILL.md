---
name: api-design-review
description: Review an HTTP/REST or RPC API design (OpenAPI spec, route code or proposal) for consistency, evolvability, error handling, security and client ergonomics. Use before publishing a new endpoint or changing an existing contract.
version: 1.0.0
allowed-tools: [fs_read, fs_list, fs_search, notes_write, artifact_write]
metadata:
  tags: [engineering, api, review]
---

# API design review

Read the contract first (OpenAPI file, route handlers, schemas). Then check, in order:

1. **Resources and naming** — plural nouns, stable identifiers, no verbs in paths except explicit
   actions (`POST /orders/{id}/cancel`). Consistent casing across fields.
2. **Methods and semantics** — GET is safe, PUT/DELETE are idempotent, POST that creates returns 201
   with the resource. Long operations return 202 plus a status resource.
3. **Errors** — one error envelope everywhere (`code`, `message`, optional `details`), correct status
   classes (400 validation, 401/403 auth, 404, 409 conflict, 422 if used consistently, 429 with
   `Retry-After`). No stack traces or internal IDs leaked.
4. **Evolvability** — additive changes only within a version; unknown fields tolerated; enums
   documented as open; pagination (cursor preferred) on every list; no unbounded responses.
5. **Security** — authn on every non-public route, authz on the resource, scopes documented, rate
   limits, idempotency keys for retried writes, input size limits.
6. **Ergonomics** — a client can complete the main use case in the fewest calls; examples exist;
   timestamps are RFC 3339 UTC; money is integer minor units plus currency.

Use `references/checklist.md` for the full list. For each finding report severity
(blocker / should-fix / nit), the endpoint, the problem, and a corrected example request or response.
Finish with a short verdict: ship, ship with fixes, or redesign.
