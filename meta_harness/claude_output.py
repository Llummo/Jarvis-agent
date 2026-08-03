"""Detect a Claude CLI run that failed while reporting success.

Every caller here checks `returncode != 0` and treats anything else as an
answer. That is not enough. The CLI can exit 0 having printed an operational
message instead of a response — an exhausted credit balance, an expired login,
a usage cap. Downstream that string is handled as if the model had produced it.

Today the damage is contained: each caller parses the output as JSON, so an
English sentence fails and the run stops. But it stops with *"Claude did not
return valid JSON"*, which sends whoever is debugging into the prompt when the
real problem is billing. And the containment is incidental — it holds only for
as long as every path keeps parsing JSON.

So the check lives here, once, and each runner calls it before parsing.
"""

from __future__ import annotations

import re
from typing import Optional

# Matched against the first part of the output only. These phrases are how the
# CLI reports that it cannot work, not things a model writing Spanish tickets
# about a recruitment system would say.
_FAILURE_PATTERNS = (
    (r"credit balance is too low", "the Claude account has no credits left"),
    (r"insufficient credits?\b", "the Claude account has no credits left"),
    (r"out of credits?\b", "the Claude account has no credits left"),
    (r"purchase more credits", "the Claude account has no credits left"),
    (r"usage limit reached", "the Claude usage limit has been reached"),
    (r"rate limit(ed|s)? exceeded", "the Claude API rate limit was exceeded"),
    (r"quota (has been )?exceeded", "the Claude quota was exceeded"),
    (r"please run [`']?claude login", "the Claude CLI is not logged in"),
    (r"invalid api key", "the Claude API key was rejected"),
    (r"authentication (failed|error)", "the Claude CLI could not authenticate"),
    (r"you (have )?exceeded your", "a Claude account limit was exceeded"),
)

# An operational message is short. A real answer that happens to discuss
# billing is long, and looking only at the head keeps a ticket about payment
# processing from being mistaken for a failure.
_HEAD_CHARS = 600


def detect_cli_failure(output: str) -> Optional[str]:
    """A human-readable reason if this output is a CLI failure, else None.

    Empty output is deliberately *not* treated as a failure here. It is a
    different condition with a different cause, and every caller already
    reports it in terms of what it was doing — «the pasted image(s) produced
    no description» beats a generic complaint about the CLI.
    """
    if not output:
        return None

    head = output[:_HEAD_CHARS].lower()
    for pattern, reason in _FAILURE_PATTERNS:
        if re.search(pattern, head):
            return reason
    return None


class ClaudeUnavailableError(RuntimeError):
    """The CLI ran but could not do the work — billing, auth or quota.

    Deliberately separate from a parse error: nothing about the prompt or the
    document needs looking at, and no number of retries will help.
    """


def assert_usable(output: str, *, action: str = "the request") -> str:
    """Return `output`, or raise if it is a failure message in disguise.

    Raising here rather than letting the JSON parser fail turns a misleading
    *"the model returned invalid JSON"* into *"you are out of credits"*.
    """
    reason = detect_cli_failure(output)
    if reason is not None:
        snippet = " ".join(output.split())[:200]
        raise ClaudeUnavailableError(
            f"Could not complete {action}: {reason}. Claude CLI said: {snippet!r}"
        )
    return output
