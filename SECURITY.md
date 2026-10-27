# Security policy

## Supported versions

Argus Council is released from the `main` branch, and only the latest commit there receives
security fixes. If you run an older build, update before reporting, and check whether the issue
still reproduces.

## Reporting a vulnerability

**Do not open a public issue, pull request or discussion for a security problem.**

Report it privately through GitHub's **"Report a vulnerability"** button, on this repository's
**Security** tab (GitHub private vulnerability reporting). If that option is not available to you,
open a public issue that asks the maintainers for a private contact. Leave out every vulnerability
detail.

Please include:

- the affected component (API route, agent tool, protocol endpoint, frontend page, deployment
  file) and the commit or image version;
- steps to reproduce, or a proof of concept, and the impact you observed;
- any configuration that matters (for example `ARGUS_MCP_ALLOW_STDIO`,
  `ARGUS_CONNECTIONS_ALLOW_PRIVATE_URLS`, `ARGUS_WEB_FETCH_ALLOW_PRIVATE_URLS`, or self-hosted
  providers);
- whether you would like to be credited.

## What to expect

- We aim to acknowledge a report within **5 working days**.
- We aim to send a first assessment, including severity, within **10 working days**.
- We will keep you updated until a fix ships. We will agree a disclosure date with you, normally
  within 90 days of the report, or sooner once a fix is released.
- Fixed issues are listed in `docs/security/audit.md`, with credit if you want it.

## Scope

In scope:

- the code in this repository: the backend API, agent runtime, tools and protocol endpoints, the
  sandbox service, the frontend;
- the shipped deployment files (`deploy/`, the Dockerfiles, `frontend/nginx.conf`).

Out of scope:

- vulnerabilities in third-party LLM providers, MCP servers or remote agents you connect to;
- the behaviour of models, including prompt injection that stays within the guardrails and
  approvals a run owner configured for their own runs. Report it if it bypasses an approval gate,
  a tool allow-list, the SSRF guard or the sandbox;
- issues that need a deployment option documented as unsafe, such as stdio MCP servers or private
  networks for connections, unless the documented safeguards themselves fail;
- denial of service through sheer request volume, and findings from automated scanners without a
  demonstrated impact.

## Safe harbour

We will not pursue or support legal action against good-faith research that:

- stays within this policy;
- avoids privacy violations, data destruction and service disruption;
- uses only accounts and deployments you own or have permission to test;
- gives us reasonable time to fix the issue before public disclosure.

## Hardening guidance for operators

See `docs/security/audit.md` and `docs/security/threat-model.md`. In short:

- Set strong `ARGUS_SECRET_KEY` and `ARGUS_ENCRYPTION_KEY` values.
- Keep `ARGUS_ALLOW_REGISTRATION=false` after creating the admin; prefer the
  `ARGUS_BOOTSTRAP_ADMIN_*` variables.
- Terminate TLS in front of the web container.
- Leave the private-network switches off unless you need them.
- Treat the shared code sandbox as one trust domain: see SEC-08 in the audit.
