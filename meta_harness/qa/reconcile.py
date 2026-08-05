"""Line up what a ticket claims is on screen against what is actually there.

Reading the screen before planning stopped the harness from waiting for text
that does not exist. It did not stop the other half of the problem: a ticket can
describe a screen that no longer exists at all.

SIG-109 is the case. Its criteria list «First name, Paternal last name, Email,
Phone, Source, LinkedIn» — the form that used to live in Requerimientos → add
candidate. That form is gone. The Personas form asks for different things. With
only the vocabulary in hand, the author quietly writes a plan against whatever
the screen does offer, the run comes back green-ish, and nobody learns that the
ticket describes a feature that was removed.

So each element the ticket names is matched against the real inventory, and the
result is one of three things, all of them stated out loud:

    exacto        the ticket's word is on the screen
    equivalente   «First name» → «Nombres *», with the score that says so
    ausente       nothing on the screen answers to it

«ausente» is the interesting one, and it is deliberately not resolved here: a
missing element is either a stale ticket or an unimplemented feature, and only a
person can say which. The plan carries it to them instead of guessing.

Extraction is the one part a model does — pulling «Paternal last name» out of a
Spanish sentence is reading comprehension. The matching afterwards is code, so
the same ticket and the same screen always reconcile the same way.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from typing import List, Optional

from meta_harness.claude_output import assert_usable
from meta_harness.qa.textmatch import best_match
from meta_harness.qa.ticket_shape import TicketShape
from meta_harness.qa.vocabulary import Vocabulary

FOUND_EXACT = "exacto"
FOUND_EQUIVALENT = "equivalente"
FOUND_MISSING = "ausente"

# Above this the wording is the same thing said twice, not a real difference.
EXACT_ENOUGH = 0.999

# The text pass only claims a match when the words are essentially the same.
# Deliberately far above the 0.6 the browser uses to pick something to click:
# there, a loose match is a best effort at carrying out a step; here it is a
# statement about whether a feature exists, and «Email» is one word away from
# matching the «Automatización de Email» entry in the side menu. What text
# cannot settle goes to the pass below, which is allowed to know that «Phone»
# and «Teléfono» are the same field.
TEXT_ENOUGH = 0.9

PROMPT = (
    "Extrae los elementos de interfaz que el ticket dice que deben existir.\n\n"
    "Recibirás los criterios de aceptación. Devuelve SOLO JSON:\n"
    '{"elementos": ["...", "..."]}\n\n'
    "Reglas:\n"
    "- Un elemento es algo que se ve y se usa: un campo, un botón, una columna, "
    "una pestaña, un mensaje concreto.\n"
    "- Cópialos como los escribe el ticket, sin traducir ni corregir.\n"
    "- No inventes elementos que el ticket no nombre.\n"
    "- No incluyas acciones ni condiciones («guardar correctamente», «validar el "
    "formato»), solo cosas nombradas.\n"
    "- Si el ticket no nombra ninguno, devuelve una lista vacía."
)


@dataclass
class Match:
    """One thing the ticket names, and what answers to it on screen."""

    term: str
    status: str
    label: str = ""
    score: float = 0.0
    where: str = ""
    # Si la equivalencia la estableció el sentido y no la escritura. Se declara
    # porque las dos merecen confianzas distintas: «Resumen» → «Resumen
    # profesional» se comprueba leyendo, «Phone» → «Teléfono *» hay que creérselo.
    by_meaning: bool = False

    @property
    def missing(self) -> bool:
        return self.status == FOUND_MISSING

    def to_dict(self) -> dict:
        return {
            "term": self.term,
            "status": self.status,
            "label": self.label,
            "score": round(self.score, 2),
            "where": self.where,
            "by_meaning": self.by_meaning,
        }

    def describe(self) -> str:
        if self.missing:
            return f"«{self.term}» no existe en la pantalla"
        if self.status == FOUND_EXACT:
            return f"«{self.term}» está tal cual"
        como = "equivale a" if self.by_meaning else "se escribe"
        return f"«{self.term}» {como} «{self.label}» en la pantalla ({self.where})"


def _strip_fences(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n", "", text)
        text = re.sub(r"\n```$", "", text)
    return text.strip()


def mentioned_elements(shape: TicketShape, timeout_s: float) -> List[str]:
    """The UI elements the ticket names, or an empty list if none can be read.

    Never raises. Failing to extract means reconciling nothing, which leaves the
    author exactly where it was before this module existed.
    """
    from meta_harness.ticket_generator import _find_claude

    criteria = [
        {"nombre": c.name, "dado": c.given, "cuando": c.when, "entonces": c.then}
        for c in shape.criteria
    ]
    if not criteria:
        return []

    try:
        completed = subprocess.run(
            [_find_claude(), "-p", PROMPT, "--output-format", "text"],
            input=json.dumps({"criterios": criteria}, ensure_ascii=False, indent=2),
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        if completed.returncode != 0:
            return []
        payload = json.loads(_strip_fences(assert_usable(completed.stdout, action="la extracción")))
    except Exception:  # noqa: BLE001 - reconciliation is an improvement, not a gate
        return []

    raw = payload.get("elementos") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return []

    seen = set()
    elements = []
    for item in raw:
        term = str(item).strip()
        if term and term.lower() not in seen:
            seen.add(term.lower())
            elements.append(term)
    return elements


def reconcile(terms: List[str], vocabulary: Optional[Vocabulary]) -> List[Match]:
    """Match each named element against the screen, in the order the ticket says.

    Searched by kind so the answer says where the thing lives — a term that
    matches a heading is not the same finding as one that matches a field.
    """
    if not terms or vocabulary is None:
        return []

    places = (
        ("campo", list(vocabulary.fields)),
        ("acción", list(vocabulary.actions) + list(vocabulary.form_actions)),
        ("texto", list(vocabulary.texts)),
    )

    matches: List[Match] = []
    for term in terms:
        best: Optional[Match] = None
        for where, labels in places:
            found = best_match(term, labels)
            if not found:
                continue
            label, value = found
            if value < TEXT_ENOUGH:
                continue
            if best is None or value > best.score:
                best = Match(
                    term=term,
                    status=FOUND_EXACT if value >= EXACT_ENOUGH else FOUND_EQUIVALENT,
                    label=label,
                    score=value,
                    where=where,
                )
        matches.append(best or Match(term=term, status=FOUND_MISSING))
    return matches


RELATE_PROMPT = (
    "Di a qué elemento real de la pantalla corresponde cada término del ticket.\n\n"
    "Recibirás los términos que no se pudieron emparejar por escritura y el "
    "inventario de lo que la pantalla ofrece de verdad.\n\n"
    "Devuelve SOLO JSON:\n"
    '{"correspondencias": [{"termino": "...", "elemento": "..." | null, '
    '"donde": "campo" | "acción" | "texto"}]}\n\n'
    "Reglas:\n"
    "- «elemento» debe ser una cadena copiada literalmente del inventario, o null.\n"
    "- Empareja por lo que la cosa ES, no por cómo se escribe: «Phone» es "
    "«Teléfono *» y «Source» es «Fuente» aunque no compartan letras.\n"
    "- null cuando la pantalla no ofrece nada que haga eso. Es una respuesta "
    "válida y frecuente: el ticket puede describir una pantalla que ya no "
    "existe. No fuerces un parecido.\n"
    "- Un término genérico («el listado», «el formulario») que no sea un "
    "elemento concreto del inventario es null."
)


def relate_remaining(
    matches: List[Match], vocabulary: Optional[Vocabulary], timeout_s: float
) -> List[Match]:
    """Resolve by meaning what wording could not, leaving the rest absent.

    «Phone» and «Teléfono» share no letters, so no amount of string comparison
    relates them; that is reading, and reading is what the model is for. The
    inventory is closed — an answer outside it is discarded — so the model can
    rename a field but cannot invent one.

    Never raises: if this cannot run, the terms stay absent, which is the
    cautious reading and the one that gets a human to look.
    """
    unresolved = [match for match in matches if match.missing]
    if not unresolved or vocabulary is None:
        return matches

    from meta_harness.ticket_generator import _find_claude

    payload = json.dumps(
        {
            "terminos": [match.term for match in unresolved],
            "inventario": vocabulary.to_dict(),
        },
        ensure_ascii=False,
        indent=2,
    )
    try:
        completed = subprocess.run(
            [_find_claude(), "-p", RELATE_PROMPT, "--output-format", "text"],
            input=payload, capture_output=True, text=True, timeout=timeout_s,
        )
        if completed.returncode != 0:
            return matches
        answer = json.loads(
            _strip_fences(assert_usable(completed.stdout, action="la correspondencia"))
        )
    except Exception:  # noqa: BLE001 - staying absent is the safe outcome
        return matches

    raw = answer.get("correspondencias") if isinstance(answer, dict) else None
    if not isinstance(raw, list):
        return matches

    # El inventario acota lo que puede responder: un elemento que no está en la
    # pantalla no puede aparecer aquí por mucho que el modelo lo escriba.
    real = {
        label.lower(): (label, where)
        for where, labels in (
            ("campo", vocabulary.fields),
            ("acción", list(vocabulary.actions) + list(vocabulary.form_actions)),
            ("texto", vocabulary.texts),
        )
        for label in labels
    }
    by_term = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        term = str(item.get("termino", "")).strip()
        element = item.get("elemento")
        if not term or not element:
            continue
        found = real.get(str(element).strip().lower())
        if found:
            by_term[term.lower()] = found

    for match in unresolved:
        found = by_term.get(match.term.lower())
        if not found:
            continue
        match.status = FOUND_EQUIVALENT
        match.label, match.where = found
        match.by_meaning = True
    return matches


def describe(matches: List[Match]) -> str:
    """The correspondence as prompt text, or empty when there is nothing to say."""
    if not matches:
        return ""

    present = [m for m in matches if not m.missing]
    missing = [m for m in matches if m.missing]

    lines = ["\n\nCorrespondencia entre lo que dice el ticket y lo que hay en pantalla:"]
    for match in present:
        lines.append(f"- {match.describe()}")
    for match in missing:
        lines.append(f"- {match.describe()}")
    lines.append(
        "\nUsa siempre el nombre de la pantalla, nunca el del ticket. "
        "Para lo que figura como ausente, escribe el paso igualmente contra el "
        "nombre del ticket: el criterio pide algo que no está, y que la prueba "
        "falle es precisamente el resultado que hay que ver — no lo sustituyas "
        "por lo más parecido."
    )
    return "\n".join(lines)


def summarize(matches: List[Match]) -> List[str]:
    """Notes for the plan, so a reviewer sees the gap before running anything."""
    return [
        f"El ticket nombra «{match.term}», que no existe en la pantalla: "
        "o el ticket está desactualizado o la función no está hecha."
        for match in matches
        if match.missing
    ]
