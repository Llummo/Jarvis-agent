"""Pull the testable facts out of a ticket written in the house format.

The house format already states, in fixed places, the two things a browser test
needs and must never invent:

    📍 Ruta / Vista UI:    where the feature lives
    🔌 Endpoint Backend:   what it must call

and then lists the acceptance criteria as Given/When/Then. Parsing those out in
code — rather than asking a model for them — is what keeps the route and the
endpoint anchored to what the ticket actually promised. The model's job is to
turn a criterion into clicks, not to decide which endpoint is correct.

A ticket that does not declare both is not testable this way, and says so.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

ROUTE_MARKER = "📍 Ruta / Vista UI:"
ENDPOINT_MARKER = "🔌 Endpoint Backend:"
CRITERIA_MARKER = "✅ CRITERIOS DE ACEPTACIÓN"
CRITERION_RE = re.compile(r"📌\s*Criterio\s*\d+\s*:\s*(.+)")

# What the formatter writes when a field was left empty. Treated as absent
# rather than as a value, or every plan would try to visit "(no aplica)".
NOT_APPLICABLE = ("(no aplica)", "no aplica", "n/a", "-", "")

# The criterion the template leaves behind for the author to fill in — see
# ticket_format.py, which writes «📌 Criterio X: [Espacio para criterios
# adicionales]». Matched against the *name* and anchored, never as a substring
# of the body: «completar» and «describe» are ordinary Spanish verbs, and
# hunting for them anywhere in the text silently discarded a real criterion
# («cuando intente guardar sin completar First name…») as if it were filler.
PLACEHOLDER_NAME_RE = re.compile(r"^\s*(?:criterio\s*[xn]?|\[.*\]|<.*>)\s*$", re.IGNORECASE)

_HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")


class TicketShapeError(ValueError):
    """The ticket does not carry what a browser test needs."""


@dataclass
class Criterion:
    """One acceptance criterion, as written."""

    name: str
    given: str = ""
    when: str = ""
    then: str = ""

    @property
    def text(self) -> str:
        parts = [part for part in (self.given, self.when, self.then) if part]
        return " ".join(parts)

    @property
    def is_placeholder(self) -> bool:
        """Whether this is the template's blank slot rather than a criterion.

        Two ways to tell, both about structure rather than vocabulary: the name
        is the template's own («Criterio X», or bracketed filler), or there is
        no Dado/Cuando/Entonces at all — nothing a browser could carry out.
        """
        if PLACEHOLDER_NAME_RE.match(self.name):
            return True
        return not self.text


@dataclass
class TicketShape:
    """The testable facts a ticket declares."""

    ticket_id: str
    title: str
    route: str = ""
    endpoint_method: str = ""
    endpoint_path: str = ""
    criteria: List[Criterion] = field(default_factory=list)
    # Names of the criteria that were read but left out, so a plan can say what
    # it is not covering. A criterion that vanishes without a trace is how a
    # ticket ends up «fully tested» with a third of it never exercised.
    skipped: List[str] = field(default_factory=list)

    @property
    def testable(self) -> bool:
        return bool(self.route and self.endpoint_path and self.criteria)

    def why_not_testable(self) -> str:
        missing = []
        if not self.route:
            missing.append("no declara una ruta de UI")
        if not self.endpoint_path:
            missing.append("no declara un endpoint de backend")
        if not self.criteria:
            missing.append("no tiene criterios de aceptación utilizables")
        return f"{self.ticket_id}: " + ", ".join(missing) if missing else ""


def _value_after(text: str, marker: str) -> str:
    index = text.find(marker)
    if index == -1:
        return ""
    line = text[index + len(marker):].split("\n", 1)[0].strip()
    return "" if line.lower() in NOT_APPLICABLE else line


def _split_endpoint(raw: str) -> tuple:
    """Split «POST /api/v1/talent/people» into method and path.

    A bare path is allowed and defaults to POST, because most criteria that
    are worth testing in a browser are writes.
    """
    if not raw:
        return "", ""
    parts = raw.split(None, 1)
    if len(parts) == 2 and parts[0].upper() in _HTTP_METHODS:
        return parts[0].upper(), parts[1].strip()
    if raw.startswith("/"):
        return "POST", raw.strip()
    # Something like «(no aplica)» that slipped past, or prose.
    match = re.search(r"(/[A-Za-z0-9/_{}.-]+)", raw)
    return ("POST", match.group(1)) if match else ("", "")


def _parse_criteria(description: str) -> tuple:
    """Every criterion the ticket states, split into usable and skipped."""
    start = description.find(CRITERIA_MARKER)
    if start == -1:
        return [], []
    body = description[start + len(CRITERIA_MARKER):]

    criteria: List[Criterion] = []
    skipped: List[str] = []
    matches = list(CRITERION_RE.finditer(body))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        block = body[match.end():end]
        criterion = Criterion(name=match.group(1).strip())
        for line in block.splitlines():
            cleaned = line.strip().lstrip("*-• ").strip()
            lowered = cleaned.lower()
            # Cada cláusula se acumula: un criterio con dos «entonces» encadenados
            # perdía todos menos el último, y con él la mitad de lo que exigía.
            if lowered.startswith("dado"):
                criterion.given = _join(criterion.given, cleaned)
            elif lowered.startswith("cuando"):
                criterion.when = _join(criterion.when, cleaned)
            elif lowered.startswith("entonces"):
                criterion.then = _join(criterion.then, cleaned)
            elif criterion.then and lowered.startswith(("y ", "and ")):
                # «Y además muestra…» cuelga del entonces anterior.
                criterion.then = _join(criterion.then, cleaned)
        if criterion.is_placeholder:
            skipped.append(criterion.name)
        else:
            criteria.append(criterion)
    return criteria, skipped


def _join(existing: str, addition: str) -> str:
    return f"{existing} {addition}".strip() if existing else addition


def parse_ticket(ticket_id: str, title: str, description: str) -> TicketShape:
    """Read a house-format ticket into the facts a test can be built from."""
    method, path = _split_endpoint(_value_after(description, ENDPOINT_MARKER))
    criteria, skipped = _parse_criteria(description)
    return TicketShape(
        ticket_id=ticket_id,
        title=title,
        route=_value_after(description, ROUTE_MARKER),
        endpoint_method=method,
        endpoint_path=path,
        criteria=criteria,
        skipped=skipped,
    )
