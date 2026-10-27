---
name: threat-model
description: Build a STRIDE threat model for a system, feature or architecture change, with data-flow diagram, ranked threats and concrete mitigations. Use when designing or reviewing anything that handles untrusted input, credentials, personal data or crosses a trust boundary.
version: 1.0.0
allowed-tools: [fs_read, fs_list, fs_search, notes_write, notes_read, artifact_write]
metadata:
  tags: [security, architecture, review]
---

# Threat model

Work through these steps; record each threat with `notes_write` so other members can challenge it.

## 1. Scope
- One paragraph: what the system does, who uses it, what it must never do.
- List the **assets** (data, credentials, money, availability) and who would want them.

## 2. Model the system
- Draw a data-flow diagram as a Mermaid `flowchart`: external entities, processes, data stores, flows.
- Mark every **trust boundary** (internet to service, service to DB, tenant to tenant, user to admin).
- If code is available, ground the diagram in it (`fs_search` for routes, queues, DB clients, auth checks).

## 3. Enumerate threats (STRIDE per element)
For each element and each flow that crosses a boundary, ask the six questions in
`references/stride.md`. Skip categories that genuinely do not apply, and say why.

## 4. Rate and mitigate
For every threat give: `id`, STRIDE category, affected element, a concrete attack in one sentence,
likelihood (low/med/high), impact (low/med/high), existing control (or "none"), and the smallest
mitigation that closes it. Prefer standard controls (parameterised queries, authz at the resource,
short-lived tokens, rate limits, encryption at rest) over bespoke ones.

## 5. Deliver
Write `threat-model.md` with `artifact_write`: scope, diagram, threat table sorted by risk,
top three actions with owners, and accepted risks with the reason they are accepted.
Never mark a threat mitigated without naming the control that does it.
