"""Walk the flow a ticket describes, and write down what each view offers.

Reading one screen was enough for SIG-109, whose form sits one click from the
listing. It is not enough in general: a criterion that spans «abrir el alta →
rellenar → confirmar → ver el resultado en el listado» touches four views, and
an author that has only seen the first is guessing about three of them.

So the route is explored breadth-first and every view reached is recorded with
the clicks that reach it. That path is the useful part — it turns «the field is
called Teléfono» into «the field is called Teléfono and it is behind Agregar
persona», which is what lets a plan be written in the right order.

Two bounds keep this honest rather than a crawler:

    what gets clicked   an allow-list of words that open or advance. Never a
                        deny-list — being too cautious costs a view, being
                        wrong once costs whoever owns the QA data
    how far it goes     a view budget and a depth limit, both reported, so a
                        partial map is never mistaken for a complete one

Each view is reached by replaying its click path from a fresh load rather than
by navigating back. Going back is unreliable — a dialog closes differently from
a wizard step — and a wrong guess about where you are poisons every view found
after it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from meta_harness.qa.vocabulary import (
    _COUNT_FIELDS_JS,
    _HARVEST_JS,
    OPEN_WAIT_S,
    Vocabulary,
    _merge,
)

# Opens something new. Additive verbs only.
OPENING_WORDS = (
    "agregar", "añadir", "anadir", "nuevo", "nueva", "crear", "registrar",
    "add", "new", "create", "register",
)

# Advances an already-open flow. Kept apart from the openers because these are
# only worth following once inside something.
ADVANCING_WORDS = ("siguiente", "continuar", "next", "continue", "avanzar")

# How deep and how wide. A ticket's flow is a handful of screens; anything past
# that is exploring the product, not the flow.
MAX_DEPTH = 3
MAX_VIEWS = 8
MAX_BRANCHES_PER_VIEW = 4

# Cuánto se han de parecer dos lecturas para ser la misma pantalla. Alto a
# propósito: fundir dos vistas distintas pierde un camino del flujo, que es
# peor que listar la misma dos veces.
SAME_VIEW_OVERLAP = 0.8


@dataclass
class View:
    """One screen, and the clicks that reach it from the route."""

    name: str
    path: List[str] = field(default_factory=list)
    vocabulary: Vocabulary = field(default_factory=Vocabulary)

    @property
    def depth(self) -> int:
        return len(self.path)

    def how_to_reach(self) -> str:
        if not self.path:
            return "es la pantalla inicial"
        return " → ".join(f"«{step}»" for step in self.path)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "path": list(self.path),
            "depth": self.depth,
            **self.vocabulary.to_dict(),
        }


@dataclass
class FlowMap:
    """Every view reached from a route, and what stopped the walk."""

    route: str
    views: List[View] = field(default_factory=list)
    # Por qué se paró: agotó las ramas, o chocó con un tope. Se declara porque
    # un mapa parcial que se lee como completo es peor que no tener mapa.
    stopped_because: str = "se recorrió todo el flujo alcanzable"

    @property
    def empty(self) -> bool:
        return not self.views

    def merged(self) -> Vocabulary:
        """Everything the flow offers, flattened — for matching a bare name."""
        combined = Vocabulary()
        for view in self.views:
            _merge(combined.actions, view.vocabulary.actions)
            _merge(combined.form_actions, view.vocabulary.form_actions)
            _merge(combined.fields, view.vocabulary.fields)
            _merge(combined.texts, view.vocabulary.texts)
        return combined

    def where_is(self, label: str) -> Optional[View]:
        """The shallowest view offering `label`, so a plan opens the right one."""
        wanted = label.strip().lower()
        for view in sorted(self.views, key=lambda v: v.depth):
            vocabulary = view.vocabulary
            everything = (
                vocabulary.actions + vocabulary.form_actions
                + vocabulary.fields + vocabulary.texts
            )
            if any(item.strip().lower() == wanted for item in everything):
                return view
        return None

    def to_dict(self) -> dict:
        return {
            "route": self.route,
            "stopped_because": self.stopped_because,
            "views": [view.to_dict() for view in self.views],
        }


def _worth_clicking(label: str, *, inside: bool) -> bool:
    lowered = label.lower()
    if any(word in lowered for word in OPENING_WORDS):
        return True
    # Avanzar solo tiene sentido si ya se abrió algo: «Siguiente» en un listado
    # es paginación, y paginar no descubre ninguna vista nueva.
    return inside and any(word in lowered for word in ADVANCING_WORDS)


def _heading(vocabulary: Vocabulary) -> str:
    return vocabulary.texts[0].lower() if vocabulary.texts else ""


def _same_view(known: Vocabulary, reached: Vocabulary) -> bool:
    """Whether these are one screen rather than two.

    Sameness is not equality. «Agregar experiencia» inside the person form
    reveals more fields on the same page; comparing field sets for equality
    called that a new view, and three such blocks ate a map meant to hold the
    whole flow. So: same heading, and one field set contained in the other.
    """
    if _heading(known) != _heading(reached):
        return False
    a = {item.lower() for item in known.fields}
    b = {item.lower() for item in reached.fields}
    if not a or not b:
        return a == b
    # Solapamiento y no contención estricta: el bloque de educación renombra
    # alguna etiqueta al desplegarse, así que el conjunto crece y además cambia,
    # y exigir subconjunto dejaba la misma pantalla partida en tres.
    return len(a & b) / min(len(a), len(b)) >= SAME_VIEW_OVERLAP


def _read_current(browser) -> Vocabulary:
    raw = browser.eval(_HARVEST_JS) or {}
    vocabulary = Vocabulary()
    _merge(vocabulary.actions, raw.get("actions") or [])
    _merge(vocabulary.fields, raw.get("fields") or [])
    _merge(vocabulary.texts, raw.get("texts") or [])
    _merge(vocabulary.form_actions, raw.get("submitters") or [])
    return vocabulary


def _replay(browser, url: str, path: List[str]) -> bool:
    """Get back to a view by redoing its clicks from a fresh load."""
    browser.goto(url)
    for label in path:
        selector = browser.find_by_text(label)
        if not selector:
            return False
        before = browser.eval(_COUNT_FIELDS_JS) or 0
        browser.click(selector)
        try:
            browser.wait_for(
                f"return document.querySelectorAll('input, select, textarea').length !== {before};",
                timeout_s=OPEN_WAIT_S,
                message="",
            )
        except Exception:  # noqa: BLE001 - no toda transición cambia los campos
            pass
    return True


def explore(
    browser,
    url: str,
    *,
    max_depth: int = MAX_DEPTH,
    max_views: int = MAX_VIEWS,
) -> FlowMap:
    """Map the flow reachable from `url`.

    Never raises: an unreadable screen yields whatever was mapped before it,
    which is still more than the author had.
    """
    flow = FlowMap(route=url)
    try:
        browser.goto(url)
        landing = _read_current(browser)
    except Exception as exc:  # noqa: BLE001
        flow.stopped_because = f"no se pudo leer la pantalla inicial: {exc}"
        return flow

    start = View(name=landing.texts[0] if landing.texts else "inicio", vocabulary=landing)
    flow.views.append(start)

    queue: List[View] = [start]
    while queue:
        current = queue.pop(0)
        if current.depth >= max_depth:
            continue

        inside = current.depth > 0
        branches = [
            label
            for label in current.vocabulary.actions
            if _worth_clicking(label, inside=inside) and label not in current.path
        ][:MAX_BRANCHES_PER_VIEW]

        for label in branches:
            if len(flow.views) >= max_views:
                flow.stopped_because = (
                    f"se alcanzó el tope de {max_views} vistas; el mapa está incompleto"
                )
                return flow
            try:
                if not _replay(browser, url, current.path):
                    continue
                selector = browser.find_by_text(label)
                if not selector:
                    continue
                before = browser.eval(_COUNT_FIELDS_JS) or 0
                browser.click(selector)
                try:
                    browser.wait_for(
                        "return document.querySelectorAll('input, select, textarea')"
                        f".length !== {before};",
                        timeout_s=OPEN_WAIT_S,
                        message="",
                    )
                except Exception:  # noqa: BLE001
                    pass
                reached = _read_current(browser)
            except Exception:  # noqa: BLE001 - una rama rota no cancela el mapa
                continue

            # Un bloque que despliega más campos de la misma pantalla no es una
            # vista nueva: sus campos se suman a la que ya existe y ahí acaba.
            known = next((v for v in flow.views if _same_view(v.vocabulary, reached)), None)
            if known is not None:
                _merge(known.vocabulary.fields, reached.fields)
                _merge(known.vocabulary.actions, reached.actions)
                _merge(known.vocabulary.form_actions, reached.form_actions)
                _merge(known.vocabulary.texts, reached.texts)
                continue

            view = View(
                name=reached.texts[0] if reached.texts else label,
                path=current.path + [label],
                vocabulary=reached,
            )
            flow.views.append(view)
            queue.append(view)

    return flow


def describe(flow: Optional[FlowMap]) -> str:
    """The map as prompt text, view by view, with how each is reached."""
    if flow is None or flow.empty:
        return ""

    import json

    lines = ["\n\nMapa del flujo, leído del navegador ahora mismo:"]
    for view in sorted(flow.views, key=lambda v: (v.depth, v.name)):
        lines.append(f"\n### {view.name} — {view.how_to_reach()}")
        lines.append(json.dumps(view.vocabulary.to_dict(), ensure_ascii=False, indent=2))
    lines.append(
        "\nCada vista dice cómo se llega a ella. Los pasos del plan tienen que "
        "seguir ese camino: para usar un campo de una vista profunda, primero "
        "hay que pulsar lo que la abre, en ese orden.\n"
        "Copia los target literalmente de la vista donde están."
    )
    return "\n".join(lines)
