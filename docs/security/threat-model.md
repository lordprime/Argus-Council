# Threat Model: Argus Council platform

> **Author:** security audit (sec-review threat-model tool)  
> **Created:** 2026-10-27T15:54:51Z  

## What Are We Building?

Multi-user, multi-agent LLM council platform. A FastAPI backend authenticates users with JWT access/refresh tokens and scoped API keys (roles admin/member/viewer), runs councils of LLM agents that call tools (web_fetch, sandboxed shell/code, knowledge search, skills, external MCP servers, remote A2A/ACP agents, sub-councils) under guardrails and human approvals, exposes MCP/A2A/ACP protocol endpoints with a hash-chained ledger, delivers signed webhooks, accepts uploads (skill zips, knowledge documents), and stores LLM provider keys encrypted at rest. A React SPA is served by nginx; Postgres, Redis and a code sandbox run as separate containers. Companion to docs/security/audit.md (finding IDs SEC-xx).

## What Data Does It Touch?

- User accounts: email, display name, Argon2id password hashes (Postgres)
- JWT access (60 min) / refresh (14 d) tokens; refresh token in browser sessionStorage; revocation state in Redis
- API keys (SHA-256 hashes stored, plaintext shown once) with scopes
- LLM provider API keys, MCP headers, remote-agent tokens, webhook secrets (Fernet-encrypted, key from ARGUS_ENCRYPTION_KEY or ARGUS_SECRET_KEY)
- Run topics, agent transcripts, artifacts and blobs (Postgres + /data volume)
- Knowledge-base documents and memory items
- Protocol ledger (hash-chained) and audit log with client IPs
- Account export archives (zip, secrets redacted)

## Technologies

Python 3.12, FastAPI, SQLAlchemy, PyJWT (HS256), argon2-cffi, httpx/httpcore, Redis, PostgreSQL, React 19, react-markdown + rehype-sanitize, nginx, Docker Compose

## What Can Go Wrong?

### TM-1: A member (or anyone who can steer an agent through a fetched page) makes the server request internal addresses: cloud metadata (169.254.169.254), Redis/Postgres/sandbox on the Docker networks, or LAN services, via web_fetch, webhook URLs, MCP server URLs, remote-agent URLs or provider base URLs; or registers a public hostname and re-points its DNS to an internal IP after validation (DNS rebinding).

- **Impact:** Theft of cloud credentials, probing and interacting with internal services, reading internal HTTP responses through the agent.
- **Likelihood:** high
- **Data Affected:** cloud credentials, internal service data
- **Status:** mitigated

**Mitigation:** argus/core/netguard.py: every outbound client for connections and web_fetch resolves and vets each address at connect time and connects to the vetted IP; private targets need ARGUS_CONNECTIONS_ALLOW_PRIVATE_URLS / ARGUS_WEB_FETCH_ALLOW_PRIVATE_URLS; link-local always refused; run guardrails can only tighten. Residual: provider base URLs are validated at save time only (SEC-14).

**Evidence:**

- [code] `backend/argus/core/netguard.py` — GuardedBackend.connect_tcp vets resolved IPs before connecting
- [code] `backend/argus/tools/web.py:120` — server-side ceiling on private networks for web_fetch
  ```
  block_private = policy.block_private_ips or not self.allow_private
  ```
- [code] `backend/argus/services/llm.py:47` — validate_base_url always refuses link-local

### TM-2: An attacker brute-forces or credential-stuffs the login, spreading attempts across spoofed client IPs: nginx appended the client's X-Forwarded-For and uvicorn (FORWARDED_ALLOW_IPS=*) used the left-most, attacker-controlled entry as the client IP.

- **Impact:** Account takeover; Argon2 hashing load as a denial-of-service lever; spoofed IPs in the audit log.
- **Likelihood:** high
- **Data Affected:** user accounts
- **Status:** mitigated

**Mitigation:** nginx overwrites X-Forwarded-For with $remote_addr; login is additionally throttled per account. Recommend MFA for admins (not implemented).

**Evidence:**

- [code] `frontend/nginx.conf` — proxy_set_header X-Forwarded-For $remote_addr
- [code] `backend/argus/api/v1/auth.py:90` — per-IP and per-account login throttles

### TM-3: A stolen refresh or access token stays usable after the user logs out or changes the password; two parallel uses of one refresh token both succeed.

- **Impact:** Persistent account access for up to the refresh lifetime (14 days).
- **Likelihood:** medium
- **Data Affected:** user session, everything the user owns
- **Status:** mitigated

**Mitigation:** Session id (sid) claim shared by a login's tokens; /auth/logout revokes it; refresh is single-use with an atomic counter and reuse revokes the session; password change revokes all other sessions. Revocation lives in Redis.

**Evidence:**

- [code] `backend/argus/services/sessions.py` — revocation helpers
- [code] `backend/argus/api/v1/auth.py:110` — atomic single-use refresh with family revocation

### TM-4: An admin's read-only or narrowly scoped API key leaks and is used to promote accounts, deactivate users or run data retention, because admin routes checked the role but not the key scope.

- **Impact:** Privilege escalation and destructive admin actions from a key meant to be read-only.
- **Likelihood:** medium
- **Data Affected:** user roles, retained data
- **Status:** mitigated

**Mitigation:** Admin mutations require a JWT session or a '*'-scope key (AdminWriter).

**Evidence:**

- [code] `backend/argus/api/deps.py:187` — require_admin_writer

### TM-5: Code the agents write and run in the sandbox (possibly steered by injected instructions in fetched content) reaches other services, or reads/modifies another user's concurrent sandbox workspace because every command runs as the same uid in one shared container.

- **Impact:** Cross-tenant disclosure or tampering of code and outputs; attacks on the API from inside the deployment.
- **Likelihood:** medium
- **Data Affected:** run workspaces, artifacts
- **Status:** in_progress

**Mitigation:** Sandbox now has network_mode none and is reached over a Unix socket (no route to API, DB, cache or internet); background processes reaped after each command. Open: per-execution uid or container isolation (SEC-08).

**Evidence:**

- [code] `deploy/docker-compose.yml` — sandbox network_mode: none, Unix socket volume
- [code] `backend/argus/sandbox/executor.py` — commands share the server's uid; rlimits only

### TM-6: Untrusted content (web pages, MCP tool results, knowledge documents, remote agents) carries instructions that make agents call risky tools: shell, package install, remote agents, MCP tools, outbound fetches that exfiltrate data in URLs.

- **Impact:** Unwanted actions within the user's own run; data exfiltration through web_fetch query strings or remote agents.
- **Likelihood:** medium
- **Data Affected:** run data, knowledge base content
- **Status:** accepted

**Mitigation:** Tool outputs wrapped as untrusted and screened (secret redaction, injection patterns); risk-tagged tools need human approval by default (shell, package_install, remote_agent, network_write; MCP servers default to 'ask'); per-member tool allow-lists; domain allow-list for web_fetch; private networks blocked. Accepted: the run owner can remove approvals for their own runs.

**Accepted Risk:** Approval policy is the run owner's choice and only affects their own data; defaults are safe.

**Evidence:**

- [code] `backend/argus/runtime/agent.py` — approval gate before tool execution
- [code] `backend/argus/runtime/guardrails.py` — approval_required defaults and output screening

### TM-7: One user reads or changes another user's runs, councils, providers, webhooks, connections, knowledge bases, skills, templates, notifications or ledger by changing an id.

- **Impact:** Cross-tenant data access.
- **Likelihood:** low
- **Data Affected:** all per-user data
- **Status:** mitigated

**Mitigation:** Every router looks objects up by id and owner and returns 404 on mismatch; verified router by router in the audit.

**Evidence:**

- [code] `backend/argus/api/v1/runs.py` — _own_run owner check on every run route
- [code] `backend/argus/services/councils.py` — get_council filters by owner_id

### TM-8: A malicious skill zip or knowledge upload escapes its folder, decompresses to gigabytes, or ships active content.

- **Impact:** Disk/memory exhaustion or file overwrite.
- **Likelihood:** low
- **Data Affected:** server storage
- **Status:** mitigated

**Mitigation:** Skill zips are parsed in memory (never extracted to disk), with entry count, declared-size and per-file caps, safe relative paths and a text-suffix allow-list; knowledge uploads are capped at 5 MB, text types only, binary rejected.

**Evidence:**

- [code] `backend/argus/skills/package.py` — load_zip limits and _safe_relpath
- [code] `backend/argus/knowledge/text.py` — detect_type / extract_text

### TM-9: Model output rendered in the browser runs script (XSS) and steals the refresh token from sessionStorage.

- **Impact:** Account takeover.
- **Likelihood:** low
- **Data Affected:** session tokens
- **Status:** mitigated

**Mitigation:** react-markdown with skipHtml + rehype-sanitize, URL scheme allow-list, images not loaded; strict CSP (script-src 'self'); React 19 blocks javascript: URLs; artifacts served as text/plain or attachment with nosniff. Accepted: refresh token in sessionStorage.

**Evidence:**

- [code] `frontend/src/components/ui/Markdown.tsx` — sanitised rendering
- [code] `frontend/nginx.conf` — Content-Security-Policy

### TM-10: Anyone who can reach a deployment self-registers and spends the operator's system LLM keys, or wins the race to register the first (admin) account on a fresh install.

- **Impact:** Cost abuse; admin takeover of a new deployment.
- **Likelihood:** medium
- **Data Affected:** provider budget, admin account
- **Status:** in_progress

**Mitigation:** deploy/.env.example now disables self-registration; ARGUS_BOOTSTRAP_ADMIN_* creates the admin without a public window. Residual: compose fallback stays 'true' for CI.

**Evidence:**

- [code] `deploy/docker-compose.yml:64` — ARGUS_ALLOW_REGISTRATION fallback
- [code] `backend/argus/api/v1/auth.py` — first account becomes admin

### TM-11: Secrets leak through API responses, exports, logs or error messages (provider keys, MCP headers, agent tokens, webhook secrets, password hashes).

- **Impact:** Credential theft.
- **Likelihood:** low
- **Data Affected:** provider API keys, connection credentials
- **Status:** mitigated

**Mitigation:** Secrets encrypted at rest and never returned (key hints only, header names only); account export strips *_enc, hashes and secrets; tool arguments redacted in events; audit details scrubbed. Recommend a dedicated ARGUS_ENCRYPTION_KEY.

**Evidence:**

- [code] `backend/argus/services/account_export.py:54` — _secret column filter
- [code] `backend/argus/core/crypto.py` — SecretBox key derivation

### TM-12: A webhook receiver accepts forged or replayed deliveries.

- **Impact:** Receiver acts on fake run events.
- **Likelihood:** low
- **Data Affected:** receiver systems
- **Status:** mitigated

**Mitigation:** Deliveries carry HMAC-SHA256 over timestamp and raw body plus a unique delivery id; receivers must verify within 5 minutes and de-duplicate ids (documented in services/webhooks.py).

**Evidence:**

- [code] `backend/argus/services/webhooks.py:37` — sign/verify

---
*Generated by mcp-security-review threat modeling tool*
