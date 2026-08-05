"""Read the words a screen actually shows, before writing steps about it.

The plan was authored from the ticket text alone. That is why a run against
SIG-109 failed six criteria out of six without a single one of them being a
real defect: the ticket lists the fields in English — «First name, Paternal
last name, Email, Phone» — and the form renders them in Spanish, and the model,
never having seen the screen, wrote `wait_text 'First name'` and invented
buttons called «New» and «Nueva persona» for a button that reads «Agregar
Persona». No amount of fuzzy matching bridges «First name» and «Nombres»: they
share no letters. The gap is not in the matching, it is in writing the plan
blind.

So the screen is read first and its vocabulary handed to the author, which
turns the wording from a guess into a lookup.

Fields usually live behind a click, so one level of opening is explored. What
gets clicked is an allow-list of words that create things, never a deny-list:
being too cautious costs some vocabulary, while being wrong once costs whoever
owns the QA database their afternoon.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import List, Optional

# Only these open something. Deliberately additive verbs in both languages,
# because the authoring pass clicks them unattended.
OPENING_WORDS = (
    "agregar", "añadir", "anadir", "nuevo", "nueva", "crear", "registrar",
    "add", "new", "create", "register",
)

# How many openers to try. The point is the create form; a screen with more
# than a handful of them is not going to be understood by clicking anyway.
MAX_OPENERS = 4

# How long to give a dialog to mount before deciding the click opened nothing.
OPEN_WAIT_S = 6.0

_FIELD_SELECTOR = "input, select, textarea"
_COUNT_FIELDS_JS = f"return document.querySelectorAll('{_FIELD_SELECTOR}').length;"

# Ceilings per list, as a backstop against a screen with hundreds of controls.
# Generous on purpose: the toolbar sits *after* the side menu in document order,
# so a tight cap would cut «Agregar persona» and keep forty nav links.
MAX_ACTIONS = 80
MAX_FIELDS = 40
MAX_TEXTS = 25


@dataclass
class Vocabulary:
    """What a screen offers, in the words it uses."""

    actions: List[str] = field(default_factory=list)
    # Lo que solo existe con el formulario abierto. Separado porque el botón
    # que envía está aquí y en ningún otro sitio: sin distinguirlos el autor
    # del plan escribía «Guardar», que en Sigo no existe.
    form_actions: List[str] = field(default_factory=list)
    fields: List[str] = field(default_factory=list)
    texts: List[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.actions or self.fields)

    def to_dict(self) -> dict:
        return {
            "acciones": self.actions[:MAX_ACTIONS],
            "acciones_con_el_formulario_abierto": self.form_actions[:MAX_ACTIONS],
            "campos": self.fields[:MAX_FIELDS],
            "textos": self.texts[:MAX_TEXTS],
        }


# Collects what a person would see and could act on. Invisible elements are
# skipped: a hidden button is not a button as far as a test is concerned.
_HARVEST_JS = r"""
// Se intentó acotar la lectura a [role=dialog]: en Sigo no existe, el alta de
// persona es una vista completa con «Volver», no un modal. Se lee entero.
const root = document.querySelector('[role=dialog], dialog[open], [aria-modal="true"]') || document;

const seen = new Set();
const visible = (el) => {
  const box = el.getBoundingClientRect();
  return box.width > 0 && box.height > 0;
};
const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();

// Se descartan solo las filas de una tabla: son datos de prueba, y una pantalla
// de listado aporta un nombre y un menú por fila que ahogan al resto.
//
// No se separa el menú lateral por dónde vive. Se intentó, y en Sigo salió al
// revés: la barra lateral son divs sueltos y la barra de la pantalla es un
// <header>, así que «Agregar persona» acabó clasificada como navegación. Los
// enlaces del menú se quedan — son ruido tolerable, y un criterio que empieza
// navegando los necesita.
const rows = 'table, [role=table], [role=row], tbody';

const actions = [];
const submitters = [];
for (const el of root.querySelectorAll('button, a, [role=button], input[type=submit]')) {
  if (!visible(el) || el.closest(rows)) continue;
  const label = clean(el.innerText || el.value || el.getAttribute('aria-label'));
  if (!label || label.length > 60 || seen.has('a:' + label)) continue;
  seen.add('a:' + label);
  actions.push(label);
  // Un botón deshabilitado en una pantalla de alta es, casi siempre, el que
  // envía: está esperando a que los campos obligatorios estén completos. Es la
  // única señal fiable aquí, porque el de enviar se llama igual que el que
  // abrió el formulario y por nombre no se distinguen.
  if (el.disabled === true || el.getAttribute('aria-disabled') === 'true') {
    submitters.push(label);
  }
}

const fields = [];
for (const el of root.querySelectorAll('input, select, textarea')) {
  if (el.type === 'hidden' || !visible(el)) continue;
  let label = '';
  if (el.id) {
    const tag = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
    if (tag) label = clean(tag.innerText);
  }
  if (!label) label = clean(el.closest('label') ? el.closest('label').innerText : '');
  if (!label) label = clean(el.getAttribute('aria-label') || el.placeholder || el.name);
  if (label && label.length <= 60 && !seen.has('f:' + label)) {
    seen.add('f:' + label);
    fields.push(label);
  }
}

// Encabezados y etiquetas sueltas: el «entonces» de un criterio suele hablar de
// un título de sección o de un mensaje, que no es ni acción ni campo.
const texts = [];
for (const el of root.querySelectorAll('h1, h2, h3, th, legend')) {
  if (!visible(el)) continue;
  const label = clean(el.innerText);
  if (label && label.length <= 60 && !seen.has('t:' + label)) {
    seen.add('t:' + label);
    texts.push(label);
  }
}

return { actions: actions, submitters: submitters, fields: fields, texts: texts };
"""


def _opens_something(label: str) -> bool:
    lowered = label.lower()
    return any(word in lowered for word in OPENING_WORDS)


def _merge(into: List[str], more: List[str]) -> None:
    known = {item.lower() for item in into}
    for item in more:
        if item.lower() not in known:
            known.add(item.lower())
            into.append(item)


def harvest(browser, url: str, *, explore: bool = True) -> Vocabulary:
    """The vocabulary of the screen at `url`, one click deep.

    Never raises: a screen that cannot be read leaves the author working from
    the ticket alone, which is exactly what it did before this existed.
    """
    vocabulary = Vocabulary()
    try:
        browser.goto(url)
        landing = browser.eval(_HARVEST_JS) or {}
    except Exception:  # noqa: BLE001 - authoring must survive an unreadable screen
        return vocabulary

    _merge(vocabulary.actions, landing.get("actions") or [])
    _merge(vocabulary.fields, landing.get("fields") or [])
    _merge(vocabulary.texts, landing.get("texts") or [])

    if not explore:
        return vocabulary

    openers = [label for label in vocabulary.actions if _opens_something(label)][:MAX_OPENERS]
    for label in openers:
        try:
            selector = browser.find_by_text(label)
            if not selector:
                continue
            before = browser.eval(_COUNT_FIELDS_JS) or 0
            browser.click(selector)
            # Esperar a que el diálogo monte. Sin esto se mide el DOM anterior y
            # el formulario se lee vacío — que es como el plan acabó escribiendo
            # los nombres de campo del ticket en vez de los de la pantalla.
            try:
                browser.wait_for(
                    f"return document.querySelectorAll('{_FIELD_SELECTOR}').length > {before};",
                    timeout_s=OPEN_WAIT_S,
                    message=f"«{label}» no abrió ningún formulario",
                )
            except Exception:  # noqa: BLE001 - no todo botón abre un formulario
                pass
            opened = browser.eval(_HARVEST_JS) or {}
            # También las acciones: el botón que confirma el formulario solo
            # existe con el diálogo abierto. Sin esto el autor del plan tiene
            # que adivinar cómo se llama el de guardar.
            landing_actions = {known.lower() for known in vocabulary.actions}
            _merge(
                vocabulary.form_actions,
                [
                    item for item in (opened.get("actions") or [])
                    if item.lower() not in landing_actions
                ]
                + (opened.get("submitters") or []),
            )
            _merge(vocabulary.fields, opened.get("fields") or [])
            _merge(vocabulary.texts, opened.get("texts") or [])
            # Recargar en vez de buscar el botón de cerrar: el diálogo puede
            # cerrarse de varias formas y ninguna es fiable a ciegas.
            browser.goto(url)
        except Exception:  # noqa: BLE001 - one unopenable dialog is not fatal
            try:
                browser.goto(url)
            except Exception:  # noqa: BLE001
                break
    return vocabulary


def describe(vocabulary: Optional[Vocabulary]) -> str:
    """The vocabulary as prompt text, or empty when nothing could be read."""
    if vocabulary is None or vocabulary.empty:
        return ""
    return (
        "\n\nPalabras que REALMENTE aparecen en esa pantalla, leídas del "
        "navegador ahora mismo:\n"
        + json.dumps(vocabulary.to_dict(), ensure_ascii=False, indent=2)
        + "\n\nUsa estas palabras literalmente en target. El criterio puede "
        "nombrar un campo en inglés («First name») mientras la pantalla lo "
        "muestra en español («Nombres»): manda la pantalla, no el ticket. "
        "Si lo que el criterio pide no está en esta lista, escribe el paso "
        "igualmente — que falle es el resultado correcto."
    )
