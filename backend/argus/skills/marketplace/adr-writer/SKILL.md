---
name: adr-writer
description: Write an Architecture Decision Record (ADR) - context, options considered with trade-offs, decision and consequences. Use when the council reaches or must make a significant technical decision that future maintainers need to understand.
version: 1.0.0
allowed-tools: [fs_read, fs_list, fs_search, notes_read, artifact_write]
metadata:
  tags: [engineering, architecture, writing]
---

# ADR writer

An ADR records *why*, so a reader in two years can tell whether the decision still holds.

1. **Title** — a short imperative phrase: "Use PostgreSQL for run storage".
2. **Context** — the forces at play: requirements, constraints, current pain, deadlines. Facts, not
   advocacy. Link to evidence (benchmarks, incidents, docs).
3. **Options** — at least two real alternatives plus "do nothing". For each: how it works in one
   line, pros, cons, cost, and risk. Score against the same criteria for every option.
4. **Decision** — the chosen option in one sentence, and the deciding criteria.
5. **Consequences** — what becomes easier, what becomes harder, follow-up work, and the signal that
   would make us revisit the decision.

Use `templates/adr.md`. Number ADRs sequentially if a `docs/adr/` folder exists (check with
`fs_list`). Keep it under one page; move detail into links. Status is Proposed until accepted.
