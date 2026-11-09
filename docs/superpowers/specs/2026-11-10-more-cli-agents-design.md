# Six more CLI agents: design

Date: 2026-11-10. Release: 0.1.4. Companion to
[2026-11-09-cli-agent-sign-in-design.md](2026-11-09-cli-agent-sign-in-design.md).

## Why

The user asked for about six more CLI agents next to Claude Code, Codex and the Gemini CLI. Nine
candidates were researched (notes with sources in the session notes folder, summarised into
`docs/cli-agents.md`). The user picked: **GitHub Copilot CLI, Kiro CLI, Qwen Code, opencode,
Goose, Mistral Vibe**. Rejected: Cursor CLI (no supported way to turn its shell and write tools
off), Amazon Q Developer CLI (replaced by Kiro CLI, maintenance only), Charm Crush (no
machine-readable output, auto-approves every tool, user config overrides the system config), Amp
(server-side tools not confinable locally, every thread stored on ampcode.com, no model choice).

## Goals

- Each new CLI is a council seat provider (`copilot-cli`, `kiro-cli`, `qwen-cli`, `opencode-cli`,
  `goose-cli`, `vibe-cli`) with the same model as today: a fixed argv built in
  `argus/runner/specs.py`, the prompt on stdin, the system prompt in a file, **every tool of the
  CLI's own turned off and enforced by the operator** (flag, environment or a root-owned policy
  file in the image, never only user config), machine-readable output parsed in
  `argus/providers/cli/parsers.py`, session resume, usage/rate-limit/sign-in errors mapped to the
  fallback categories, per-user HOME, a per-user API key, and, where the CLI has a login that can
  be driven headlessly, the in-app sign-in.
- Defence in depth: where a CLI reports its tool list at start (Qwen `system/init.tools`, Copilot
  `session.start`/`session.tools_updated`, Kiro ACP session capabilities), the runner checks it is
  empty and kills the turn with `tools_not_disabled` otherwise.
- Pinned versions with checksums; facts and sources per CLI in `docs/cli-agents.md`.

## Non-goals

- No CLI whose tools cannot be confined. No CLI-wrapping providers inside a harness (Goose's
  `claude-code`, `codex`, `gemini-cli`, `cursor-agent`, `*-acp` providers are refused).
- No Claude subscription through opencode (removed upstream at Anthropic's request).
- No Alibaba Coding Plan or Token Plan keys for Qwen (their terms forbid backend/automated use).

## Per-CLI design

| | Copilot CLI | Kiro CLI | Qwen Code | opencode | Goose | Mistral Vibe |
| --- | --- | --- | --- | --- | --- | --- |
| Pin | npm `@github/copilot` 1.0.108 | `desktop-release.q.us-east-1.amazonaws.com/3.1.0/kirocli-x86_64-linux.zip`, sha256 `6f43c9ca…c8ba` (from the release manifest) | npm `@qwen-code/qwen-code` 0.27.1 | npm `opencode-ai` 1.19.5 (1.x line; OpenCode 2 is a separate beta) | GitHub release v1.57.0 `goose-x86_64-unknown-linux-gnu.tar.gz`, sha256 `10eb96cb…98e3` | PyPI `mistral-vibe==3.1.0`, hash-pinned, own venv `/opt/vibe` |
| Run | `copilot --output-format json --stream on --no-color --no-auto-update --no-custom-instructions --no-ask-user --disable-builtin-mcps --disallow-temp-dir --available-tools= --deny-tool=shell --deny-tool=write --deny-tool=read --deny-tool=url --deny-tool=memory --sync-session=off --disable-client-telemetry --usage-output-file F [--model M] [--reasoning-effort E] --session-id ID` | `kiro-cli acp` driven by the runner over JSON-RPC (see below), `KIRO_HOME` per user, agent `argus-seat` | `qwen --output-format stream-json --include-partial-messages --approval-mode default --safe-mode --max-tool-calls 0 [-m M] (--session-id ID \| --resume ID)`, `QWEN_SYSTEM_MD` | `opencode --pure run --format json --agent argus [-m provider/model] [--variant E] [-s ses_…]` | `goose run --no-profile --output-format stream-json -i - (-n ID \| --resume -n ID)` with `GOOSE_MODE=chat`, `GOOSE_PATH_ROOT`, `GOOSE_DISABLE_KEYRING=1`, `GOOSE_PROVIDER`, `GOOSE_SYSTEM_PROMPT_FILE_PATH`, `GOOSE_DISABLE_SESSION_NAMING=true` | `vibe -p --output streaming --agent ask --disabled-tools '*' --system-prompt-file F --output-dir D [--model M] [--resume ID]` with `VIBE_HOME`, `VIBE_ENABLE_SUBAGENTS=false`, `VIBE_ENABLE_BACKGROUND_PROCESSES=false`, `VIBE_ENABLE_TELEMETRY=false` |
| Tools off, enforced by | argv (deny always wins; `-p` denies anything that would prompt) | root-owned `/etc/kiro/managed-settings.json` denying every capability (fails closed), agent with `tools: []`, `includeMcpJson: false`, knowledge and code intelligence off, never `--trust-*`/`--cloud`; ACP permission requests answered "reject" | root-owned `/etc/qwen-code/settings.json` (highest precedence, deny union-merged) listing all 71 tool names + `mcp__*`, `mcp.excluded: ["*"]`, plus `--safe-mode`, `--max-tool-calls 0`, `init.tools == []` check; never `--bare` | root-owned `/etc/opencode/opencode.json` (loaded last) defining agent `argus` with `permission: "deny"` and every tool key denied, plus `OPENCODE_PERMISSION={"*":"deny"}` and the `OPENCODE_DISABLE_*` switches; `run` auto-rejects anything that asks | `--no-profile` (no extensions, so no tools) + `GOOSE_MODE=chat` (any tool call skipped); provider pinned by env to an API provider | `--disabled-tools '*'` (concatenated across layers, cannot be removed) + the env switches; never `--auto-approve`/`--yolo` |
| System prompt | folded into the prompt (no flag), as Codex | agent `prompt: "file://…"` | `QWEN_SYSTEM_MD` file | agent prompt `{file:…}` via `OPENCODE_CONFIG_CONTENT` | `GOOSE_SYSTEM_PROMPT_FILE_PATH` | `--system-prompt-file` |
| Output parsed | JSONL `session.start`, `assistant.message_delta`, `assistant.message`, `session.error`, `result`; tokens from the usage file | ACP `session/update` notifications (agent message chunks), `session/prompt` result | Claude-style stream-json (`system/init`, `stream_event`, `assistant`, `result`) | JSONL `text`, `step_finish` (cost, tokens), `error` (no token deltas) | JSONL `message` chunks by id, `complete` (tokens, cost), `error` | completed entries on stdout; session id, usage, error code from `export.json`, which the runner reads after exit and emits as one `argus.export` event |
| Session id | chosen UUID | read back (ACP `session/new`); resume with `session/load` | chosen UUID | read back `ses_…` from the first event | chosen: `-n <uuid>` | read back from `export.json` |
| API key | `COPILOT_GITHUB_TOKEN` (fine-grained PAT, "Copilot Requests") | `KIRO_API_KEY` (paid plans) | provider key: DashScope (Model Studio pay-as-you-go) or OpenAI-compatible | provider key: Anthropic, OpenAI, Google, OpenRouter, Mistral | provider key: Anthropic, OpenAI, Google, OpenRouter, Mistral | `MISTRAL_API_KEY` |
| In-app sign-in | `copilot login --device-code` (GitHub device code) | `kiro-cli login --use-device-flow` (Builder ID; Google and GitHub via `--social`) | none (key only) | `opencode auth login -p github-copilot` (device code) and `-p openai` "ChatGPT Pro/Plus (headless)" (device code), in a PTY | none: no account of its own | none (key only) |
| Status / sign-out | stored credential under `~/.copilot` (presence, "not verified"); sign-out removes it | `kiro-cli whoami --format json` / `kiro-cli logout` | n/a | `opencode auth list` / `opencode auth logout <provider>` | n/a | n/a |
| Fallback signals | `session.error` (`statusCode` 429, rate-limit messages), `model.call_failure`, auth errors on stderr | JSON-RPC errors, exit 1, 429 text | exit 41 auth, `result.is_error`, 429/529 text | `APIError.statusCode`, `ProviderAuthError`, "Usage limit reached" | "Rate limit exceeded:", "Credits exhausted:", "Authentication error:" | `export.json` `error.code` `rate_limit` / `unauthorized`, exit 2/3 |

### Kiro over its Agent Client Protocol

Kiro's `stream-json` schema is undocumented, so a parser could only be written from captured
output. `kiro-cli acp` speaks the documented Agent Client Protocol (Zed's JSON-RPC protocol for
coding agents; unrelated to the Agent Communication Protocol Argus removed in 0.1.4). The runner
gets a small driver for it: `initialize`, then `session/new` (or `session/load` to resume),
`session/prompt` with the prompt, streaming `session/update` notifications back as events,
answering every `session/request_permission` with reject, and closing after the prompt result.
The driver is generic, so other CLIs that speak ACP could use it later.

### Keys for harness CLIs (Qwen, opencode, Goose)

These run models from several providers, so a key alone is not enough. The CLI key form gains a
**provider** choice from a short fixed list per CLI (above). Stored with the key
(`cli_credentials.provider`, migration 0023), sent to the runner only with that user's turn, mapped
by the runner to the provider's documented env var (and, for Goose, `GOOSE_PROVIDER`). Models are
given as that CLI expects (`provider/model` for opencode).

### Shared changes

- `CliName`, `CLI_NAMES`, `CLI_PROVIDERS`, `SUGGESTED_MODELS`, `cli_agents_allowed` validation,
  provider catalog and profiles, frontend types and the council builder's provider list grow to the
  nine CLIs. `ARGUS_CLI_AGENTS_ALLOWED` keeps its default of `claude,codex,gemini`; operators add
  the new ones explicitly.
- Session-id validation moves from "UUID" to a per-CLI pattern (UUID for most, `ses_[A-Za-z0-9]{8,60}`
  for opencode); ids read back from output are validated before they are recorded.
- The runner gains: optional PTY mode (shared with sign-in), reading one result file after exit
  (Vibe `export.json`, Copilot usage file) into one event, the tools-empty check, and per-CLI
  per-turn config files written into the user's HOME (Kiro agent file, opencode config content).
- Runner image: three more npm packages in `backend/docker/runner/package.json` (lockfile
  regenerated), Kiro and Goose fetched with `ADD --checksum=sha256:…`, Vibe in its own venv from a
  hash-pinned requirements file, the three policy files root-owned. Expect the image to grow by
  roughly 600 MB (opencode's binary alone is about 214 MB).
- `scripts/build.sh` and `scripts/lib.sh` are not changed (owned by the browser work); the runner
  image name does not change.

## Testing and what cannot be verified here

Tests use fake CLI executables (Python scripts installed as the binaries) that emit each CLI's
documented events, including errors and limit messages, plus a fake ACP agent for Kiro. Covered:
argv per CLI (tools-off flags always present, never the forbidden flags), env per CLI, parsers,
session id extraction and validation, the tools-empty check, fallback categories, policy files'
content, sign-in parsing per CLI, key provider mapping.

Not verifiable on this machine: running the real CLIs (no Docker, no vendor accounts, and running
downloaded CLI code is blocked by the session's permission rules) and building the image. The
first real run on a deployed instance is the integration check; the tools-empty check and the
operator policy files are the safety net if a CLI's behaviour differs from its docs.

## Build order

1. In-app sign-in for the existing three CLIs, with a per-CLI login description the new CLIs plug
   into. Commit.
2. Shared changes (names, validation, key provider + migration, runner PTY/result-file/tools
   check). Commit.
3. One commit per CLI: Copilot, Qwen Code, Goose, opencode, Vibe, Kiro (ACP driver last).
4. Docs (`docs/cli-agents.md` per-CLI facts with sources, safety table, setup, pinned versions) and
   CHANGELOG 0.1.4, with each step.

## Decisions

The user decided:

- Harness CLIs (Qwen Code, opencode, Goose) get a provider choice with the key, with migration 0023.
- The six new CLIs are off by default. `ARGUS_CLI_AGENTS_ALLOWED` stays `claude,codex,gemini`, and
  operators opt in per CLI.
- They ship tested against fake CLIs. The first real run happens on the user's server.

Builder's calls (the user had no preference or did not weigh in):

- Qwen Code and Mistral Vibe use API keys only. Their sign-ins live in their interactive screens,
  and Vibe's only produces a Mistral API key anyway.
- Kiro runs over its documented Agent Client Protocol mode rather than its undocumented
  `stream-json`.
- Kiro and Goose are pinned by exact version URL with the vendors' published SHA-256 checksums
  (Kiro's release manifest, GitHub's asset digest).

## Left to the builder

Exact file split for the new parsers and the ACP driver, the provider lists' labels, copy wording,
and how the per-turn config files are named.
