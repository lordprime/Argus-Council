"""Time limits shared by the API's browser client and the browser service.

``ARGUS_BROWSER_ACTION_TIMEOUT_S`` is the one knob, and both containers read it (the compose file
passes the same value to each). In the browser service it is the whole budget for one action: the
action itself gets at most 60% of it, and reading the page afterwards (snapshot, URL, title,
screenshot) gets what is left. The API waits that long plus a margin for the service's answer, and
for a new session it waits out the service's worst case: the wait for a free slot plus a Chrome
start, plus the same margin.
"""

from __future__ import annotations

from collections.abc import Mapping

ACTION_TIMEOUT_ENV = "ARGUS_BROWSER_ACTION_TIMEOUT_S"
DEFAULT_ACTION_TIMEOUT_S = 30.0
# Share of the action budget the action command itself may use; the rest reads the page back.
ACTION_SHARE = 0.6
# A session request waits this long for a free slot before the service answers "busy".
SLOT_WAIT_S = 30.0
# Chrome must open its debugging port within this time of being started.
START_TIMEOUT_S = 20.0
# Slack on the API side for the HTTP round trip, process spawns and the CDP guard's attach.
CLIENT_MARGIN_S = 15.0


def action_timeout_from_env(environ: Mapping[str, str]) -> float:
    """The action budget from the environment, as the browser service reads it."""
    return float(environ.get(ACTION_TIMEOUT_ENV) or DEFAULT_ACTION_TIMEOUT_S)


def client_timeouts(action_timeout_s: float) -> tuple[float, float]:
    """``(action, open_session)`` read timeouts for the API's client.

    Each is longer than the service's own worst case for that request, so the service always
    answers (with a result, a timeout error or "busy") before the client gives up on it.
    """
    return action_timeout_s + CLIENT_MARGIN_S, SLOT_WAIT_S + START_TIMEOUT_S + CLIENT_MARGIN_S
