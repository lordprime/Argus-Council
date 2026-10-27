# STRIDE prompts

| Category | Property violated | Ask |
|---|---|---|
| **S**poofing | Authentication | Can someone act as another user, service or device? Are tokens bound, short-lived, verified on every hop? |
| **T**ampering | Integrity | Can data be modified in transit, at rest, or in a client-controlled field (IDs, prices, roles)? |
| **R**epudiation | Non-repudiation | Could a user deny an action? Are security-relevant actions audit-logged with actor and time? |
| **I**nformation disclosure | Confidentiality | Can data leak via errors, logs, caches, IDOR, side channels, backups, or over-broad API responses? |
| **D**enial of service | Availability | Unbounded input, fan-out, regex, queue growth, expensive queries, missing timeouts or quotas? |
| **E**levation of privilege | Authorization | Can a lower-privileged actor reach admin functions, other tenants' data, or execute code? |

## Common high-value checks
- Authorization is enforced on the **resource**, not only the route.
- Server-side request forgery on any URL the user can supply.
- Secrets never in source, logs, URLs or client bundles.
- Deserialization and template rendering of untrusted input.
- Supply chain: pinned dependencies, signed images, least-privilege CI tokens.
