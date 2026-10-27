# API review checklist

- [ ] Every list endpoint is paginated and has a documented maximum page size.
- [ ] Every write that clients may retry accepts an idempotency key or is naturally idempotent.
- [ ] Error responses share one schema and carry a machine-readable `code`.
- [ ] 404 vs 403 policy is deliberate (do not reveal existence of other tenants' resources).
- [ ] Field names are consistent (`created_at` everywhere, not `createdAt` in one place).
- [ ] Nullable vs optional is explicit in the schema.
- [ ] Deprecations are announced with a `Deprecation`/`Sunset` header or changelog entry.
- [ ] Large payloads are limited; uploads stream and have a size cap.
- [ ] Filtering and sorting parameters are validated against an allow-list.
- [ ] Breaking changes ship under a new version or behind an opt-in.
