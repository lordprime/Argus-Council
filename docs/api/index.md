# API Reference

The REST API lives under `/api/v1`, next to the health checks (`/health/live`, `/health/ready`),
Prometheus metrics (`/metrics`) and the agent protocols (`/mcp`, `/a2a`, `/acp`,
`/.well-known/agent-card.json`). Every endpoint, schema and error is in the reference generated
from the server's own OpenAPI document:

[:octicons-book-24: Open the interactive API reference](reference.html){ .md-button .md-button--primary }
[:octicons-download-24: Download openapi.json](openapi.json){ .md-button }

A running server outside production also serves the reference at `http://localhost:8000/docs`
and the document at `/openapi.json`. In production both are switched off.

## Endpoint groups

| Tag | Prefix | Covers |
| --- | --- | --- |
| health | `/health` | Liveness and readiness |
| auth | `/api/v1/auth` | Register, login, refresh, profile, API keys |
| admin | `/api/v1/admin/users` | User management (admins) |
| councils | `/api/v1/councils`, `/api/v1/roles` | Councils, seats, resizing, the role catalogue |
| providers | `/api/v1/providers`, `/api/v1/llm/complete` | Provider connections, models, connection tests, a direct completion |
| runs | `/api/v1/runs` | Runs, controls, interventions, approvals, events, snapshots, streaming, artifacts, exports |
| skills | `/api/v1/skills` | Skills, marketplace, import and export, versions |
| connections | `/api/v1/mcp-servers`, `/api/v1/remote-agents`, `/api/v1/ledger` | MCP servers, remote agents, the ledger and its verification |
| observability | `/api/v1/audit`, `/api/v1/insights` | Audit log and run insights |
| webhooks | `/api/v1/webhooks` | Webhooks, secrets, tests, deliveries |
| data | `/api/v1/me`, `/api/v1/admin/retention` | Account export and deletion, data retention (admins) |
| knowledge | `/api/v1/knowledge-bases`, `/api/v1/memory` | Knowledge bases, documents, search, memory |
| templates | `/api/v1/templates` | Run templates |
| notifications | `/api/v1/notifications` | In-app notifications |
| mcp, a2a, acp | `/mcp`, `/a2a`, `/acp`, `/.well-known` | Agent protocols ([Protocols and Ledger](../protocols.md)) |

## Regenerating the document

`docs/api/openapi.json` is written by `scripts/gen-openapi.sh`, which imports the app and dumps
`app.openapi()`; no database or keys are needed. The documentation workflow regenerates it before
every build, so the published reference always matches the code on `main`.

```bash
./scripts/gen-openapi.sh            # write docs/api/openapi.json
./scripts/gen-openapi.sh --check    # fail if the committed copy is out of date
```
