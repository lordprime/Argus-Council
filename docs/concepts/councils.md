# Councils and Roles

A council is a saved, resizable group of agent seats that you can run again and again. Each seat
has a role, a provider and model, a persona (system prompt), a temperature and the tools it may
use. You can also start a run without a saved council: Argus then seats an ad-hoc council from
the mode's default roster.

## Creating a council

```bash
# a research council with 5 seats from the default roster
curl -X POST localhost:8000/api/v1/councils -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"name":"lit-review","mode":"research","size":5}'

# grow it to 8 seats: new seats come from the mode's priority roster
curl -X POST localhost:8000/api/v1/councils/$ID/resize -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"size":8}'
```

| Field | Meaning |
| --- | --- |
| `mode` | `general`, `research`, `coding` or `chat`; picks the default roster and tools |
| `size` | Seats to fill from the default roster; or pass explicit `members` instead |
| `min_size`, `max_size` | Bounds every resize and membership change (1 to 64, default 1 and 12) |
| `default_provider`, `default_model` | Provider and model for seats you do not set explicitly (default `mock`, `mock-1`) |
| `guardrails` | The council's guardrail layer, validated when you save it ([Guardrails](guardrails.md)) |

Seats are managed with `POST /councils/{id}/members`, `PATCH /councils/{id}/members/{member_id}`
and `DELETE /councils/{id}/members/{member_id}`. A seat added by hand gets its role's default
tools and overseer flag unless you set them. In the UI, the council builder at `/councils/new`
and the detail page at `/councils/:id` cover the same operations and list the runs made by each
council.

## Default rosters

Rosters are ordered by priority: resizing down drops seats from the end, resizing up adds the next
role in the list. Past the end of the list, Argus cycles through the mode's worker roles (not the
chair and not the overseers).

| Mode | Roster in priority order | Ad-hoc size |
| --- | --- | --- |
| `general` | chair, analyst, critic, monitor, writer, researcher, security, designer | 5 |
| `research` | chair, researcher, analyst, critic, writer, monitor, researcher, security | 6 |
| `coding` | chair, architect, coder, tester, security, monitor, critic, coder, designer | 6 |
| `chat` | assistant, monitor, critic, researcher | 2 |

The ad-hoc size applies when a run has no `council_id` and no `size`.

## Roles

| Role | Overseer | Purpose |
| --- | --- | --- |
| `chair` | | Decomposes the goal, assigns work, merges results and decides when the task is done |
| `researcher` | | Gathers information from the web and documents, takes notes with citations |
| `analyst` | | Infers, compares and reasons over gathered evidence; builds the argument |
| `writer` | | Produces the final deliverable in the requested format |
| `architect` | | Designs the software structure, interfaces and file layout before coding starts |
| `coder` | | Writes and edits code in the sandboxed workspace and runs it |
| `tester` | | Writes and runs tests; reports failures with reproduction steps |
| `designer` | | Shapes UX, UI and presentation quality of deliverables |
| `assistant` | | General conversational helper |
| `critic` | yes | Challenges reasoning and outputs; finds errors, gaps and weak evidence |
| `monitor` | yes | Watches every thought for drift, loops and guardrail breaches |
| `security` | yes | Reviews actions and code for safety, secrets leakage and dangerous operations |

`GET /api/v1/roles` returns the roles with their descriptions and default tools. Only the chair
has the `subcouncil` tool by default ([Sub-councils](sub-councils.md)).

## What a run takes from a council

When a run starts, Argus copies the council's enabled seats into the run as a snapshot, so later
edits to the council do not change runs already in flight. The run's mode is the council's mode
unless the request names one other than `general`. Guardrails are layered: the mode's defaults,
then the council's, then the run's own ([Guardrails](guardrails.md#layering)).
