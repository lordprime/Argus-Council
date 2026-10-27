"""Council agents backed by the vendors' official CLIs (``claude``, ``codex``, ``gemini``).

The CLIs run in the agent runner service (``argus.runner``) and authenticate themselves. These
adapters plug into the normal provider interface: they turn the CLI's JSON event stream into
Argus stream events, keep the vendor session across a seat's turns, and expose Argus tools to the
model through a plain-text tool-call protocol so every call still goes through Argus guardrails,
approvals and the ledger.
"""

from argus.providers.cli.errors import CLIError
from argus.providers.cli.provider import CLI_PROVIDERS, CLIProvider, cli_for_provider

__all__ = ["CLI_PROVIDERS", "CLIError", "CLIProvider", "cli_for_provider"]
