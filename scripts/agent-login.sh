#!/usr/bin/env bash
# Sign a user's vendor CLI in, inside the agent runner, with the CLI's own login flow.
#
#   ./scripts/agent-login.sh <user-email|user-id> <claude|codex|gemini>            # sign in
#   ./scripts/agent-login.sh <user-email|user-id> <claude|codex|gemini> --status   # check
#   ./scripts/agent-login.sh <user-email|user-id> <claude|codex|gemini> --logout   # sign out
#
# The CLI runs in the runner container with HOME set to that user's own directory on the runner
# volume, so the login is stored there and only that user's council seats can use it. Argus never
# sees, reads or copies the vendor token. Each vendor's terms govern its CLI: subscription logins
# are for the account owner's own use; on a shared instance use API keys (Connections > CLI agents).
# Needs CLI agents enabled (COMPOSE_PROFILES=cli-agents, ARGUS_CLI_AGENTS_ENABLED=true).

# shellcheck source=scripts/lib.sh
source "$(dirname "$0")/lib.sh"

usage() { sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; }

who="" cli="" action=login
while [ $# -gt 0 ]; do
  case "$1" in
    --status) action=status; shift ;;
    --logout) action=logout; shift ;;
    -h | --help) usage; exit 0 ;;
    -*) die "unknown option: $1 (see --help)" ;;
    *)
      if [ -z "$who" ]; then who="$1"
      elif [ -z "$cli" ]; then cli="$1"
      else die "unexpected argument: $1 (see --help)"
      fi
      shift ;;
  esac
done
[ -n "$who" ] && [ -n "$cli" ] || { usage >&2; exit 2; }
case "$cli" in
  claude | codex | gemini) ;;
  *) die "unknown CLI: $cli (claude, codex or gemini)" ;;
esac

require_docker
require_env
cli_agents_on || die "CLI agents are not enabled: set COMPOSE_PROFILES=cli-agents and ARGUS_CLI_AGENTS_ENABLED=true in deploy/.env, then ./scripts/deploy.sh"

uuid_re='^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
if [[ "$who" =~ $uuid_re ]]; then
  uid="$(printf '%s' "$who" | tr 'A-F' 'a-f')"
  found="$(printf '%s\n' "SELECT id FROM users WHERE id = :'uid';" | psql_q -tA -v "uid=$uid")" \
    || die "could not query the database (is the stack running?)"
else
  [[ "$who" =~ ^[^[:space:]@]+@[^[:space:]@]+$ ]] || die "not an email address or user id: $who"
  # The email travels as a psql variable, quoted by psql (:'email'), never spliced into SQL.
  found="$(printf '%s\n' "SELECT id FROM users WHERE lower(email) = lower(:'email');" | psql_q -tA -v "email=$who")" \
    || die "could not query the database (is the stack running?)"
fi
uid="$(printf '%s' "$found" | head -n1 | tr -d '[:space:]')"
[[ "$uid" =~ $uuid_re ]] || die "no Argus user $who"

home="/home/runner/users/$uid"
dc exec -T runner mkdir -p -m 0700 "$home" || die "the runner is not running (docker compose --profile cli-agents up -d runner)"

env_args=(-e "HOME=$home")
case "$cli:$action" in
  claude:login) cmd=(claude auth login) ;;
  claude:status) cmd=(claude auth status --text) ;;
  claude:logout) cmd=(claude auth logout) ;;
  codex:login) cmd=(codex login --device-auth) ;;
  codex:status) cmd=(codex login status) ;;
  codex:logout) cmd=(codex logout) ;;
  gemini:login)
    # Gemini CLI has no separate login command: start it, choose "Sign in with Google", follow the
    # printed link (NO_BROWSER: no browser in the container), then type /quit.
    env_args+=(-e NO_BROWSER=true)
    cmd=(gemini) ;;
  gemini:status | gemini:logout)
    # No documented status/logout command: report or remove the CLI's own cached sign-in file.
    if [ "$action" = status ]; then
      if dc exec -T runner test -f "$home/.gemini/oauth_creds.json"; then
        log "gemini: a Google sign-in is stored for $who (verified on first use)"
      else
        log "gemini: no Google sign-in stored for $who"
      fi
    else
      dc exec -T runner rm -f "$home/.gemini/oauth_creds.json"
      log "gemini: removed the stored Google sign-in for $who"
    fi
    exit 0 ;;
esac

tty=(-T)
[ -t 0 ] && [ -t 1 ] && tty=(-it)
[ "$action" = login ] && log "starting the $cli CLI's own sign-in for $who (user $uid)"
dc exec "${tty[@]}" "${env_args[@]}" runner "${cmd[@]}"
