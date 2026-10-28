"""Agent runner: launches the vendors' official CLIs (claude, codex, gemini) as council agents.

The runner is a separate, internal-only service (see ``deploy/docker-compose.yml``). It never
handles vendor logins itself: each CLI authenticates with its own login state in the user's HOME
on the runner volume (created by ``scripts/agent-login.sh``), or with an API key the API injects
into that one process. Argus only builds a fixed, validated argv and reads the CLI's JSON output.
"""
