"""Time limits shared by the API's browser client and the browser service.

``ARGUS_BROWSER_ACTION_TIMEOUT_S`` is the one knob, and both containers read it (the compose file
passes the same value to each). In the browser service it is the whole budget for one action: the
action itself gets at most 60% of it, and reading the page afterwards (snapshot, URL, title,
screenshot) gets what is left.

Actions that can load a page (``open``, and a ``click`` that follows a link) get more when that
budget is short: agent-browser itself waits up to :data:`AGENT_BROWSER_LOAD_WAIT_S` for the page's
load event, and the service outwaits it so that agent-browser's own answer (the page, or its own
error) is what comes back, with time left to read the page.

The API waits the service's longest action plus a margin for the service's answer, and for a new
session it waits out the service's worst case: the wait for a free slot plus a Chrome start, plus
the same margin.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

ACTION_TIMEOUT_ENV = "ARGUS_BROWSER_ACTION_TIMEOUT_S"
DEFAULT_ACTION_TIMEOUT_S = 30.0
# The bounds the API's settings enforce (argus.core.config); the service checks the same.
MIN_ACTION_TIMEOUT_S = 1.0
MAX_ACTION_TIMEOUT_S = 300.0
# Share of the action budget the action command itself may use; the rest reads the page back.
ACTION_SHARE = 0.6
# agent-browser 0.41.2 waits this long for a page's load event after ``open`` (hard-coded in
# BrowserManager: default_timeout_ms = 25_000; no flag or variable changes it for ``open``).
AGENT_BROWSER_LOAD_WAIT_S = 25.0
# On top of that wait: the CLI process itself and, on a session's first command, starting its
# daemon and attaching to Chrome.
NAVIGATION_SLACK_S = 7.0
NAVIGATION_CLI_S = AGENT_BROWSER_LOAD_WAIT_S + NAVIGATION_SLACK_S
# Reading the page back after a navigating action gets at least this long.
READBACK_MIN_S = 10.0
# Actions that may load a page. A click only waits for the click itself, but the next command
# queues behind any load agent-browser is still waiting for, so a click gets the same room.
NAVIGATING_ACTIONS = frozenset({"open", "click"})
# A session request waits this long for a free slot before the service answers "busy".
SLOT_WAIT_S = 30.0
# Chrome must open its debugging port within this time of being started.
START_TIMEOUT_S = 20.0
# Slack on the API side for the HTTP round trip, process spawns and the CDP guard's attach.
CLIENT_MARGIN_S = 15.0


def check_action_timeout(value: float) -> float:
    """``value`` if it is a number of seconds within the API's bounds, else ``ValueError``."""
    if isinstance(value, bool) or not math.isfinite(value):
        raise ValueError(f"{ACTION_TIMEOUT_ENV} must be a number of seconds")
    if not MIN_ACTION_TIMEOUT_S <= value <= MAX_ACTION_TIMEOUT_S:
        raise ValueError(
            f"{ACTION_TIMEOUT_ENV} must be from {MIN_ACTION_TIMEOUT_S:g} to {MAX_ACTION_TIMEOUT_S:g} "
            f"seconds; got {value:g}"
        )
    return float(value)


def action_timeout_from_env(environ: Mapping[str, str]) -> float:
    """The action budget from the environment, as the browser service reads it.

    Raises ``ValueError`` with a message naming the variable when it is not a number from 1 to
    300 (the same bounds the API's settings enforce), so a bad value fails the service at startup.
    """
    raw = (environ.get(ACTION_TIMEOUT_ENV) or "").strip()
    if not raw:
        return DEFAULT_ACTION_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{ACTION_TIMEOUT_ENV} must be a number of seconds; got {raw!r}") from None
    return check_action_timeout(value)


def action_budget(action: str, action_timeout_s: float) -> float:
    """The service's whole budget for one action: the command plus reading the page back."""
    if action in NAVIGATING_ACTIONS:
        return max(action_timeout_s, NAVIGATION_CLI_S + READBACK_MIN_S)
    return action_timeout_s


def command_limit(action: str, action_timeout_s: float) -> float:
    """How long the action's own agent-browser command may run.

    For a navigating action always longer than agent-browser's own load wait, so its answer (or
    its own timeout error) arrives before the service gives up on it.
    """
    share = action_timeout_s * ACTION_SHARE
    if action in NAVIGATING_ACTIONS:
        return max(share, NAVIGATION_CLI_S)
    return share


def longest_action_s(action_timeout_s: float) -> float:
    """The service's longest budget for any one action."""
    return max(action_budget(a, action_timeout_s) for a in (*NAVIGATING_ACTIONS, "snapshot"))


def client_timeouts(action_timeout_s: float) -> tuple[float, float]:
    """``(action, open_session)`` read timeouts for the API's client.

    Each is longer than the service's own worst case for that request, so the service always
    answers (with a result, a timeout error or "busy") before the client gives up on it.
    """
    return (
        longest_action_s(action_timeout_s) + CLIENT_MARGIN_S,
        SLOT_WAIT_S + START_TIMEOUT_S + CLIENT_MARGIN_S,
    )
