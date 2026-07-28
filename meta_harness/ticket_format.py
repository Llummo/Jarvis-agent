"""How a ticket is written out for each tracker.

The two trackers present tickets differently on purpose — Linear follows
the team's house template exactly (named criteria with Given/When/Then on
their own bullets), ClickUp keeps a more compact form. Both live here
because three places now need them: creating tickets, creating issues, and
reformatting an existing one in place.

Pure text composition — no I/O, no tracker calls.
"""

from __future__ import annotations

import re


CODE_FENCE = "```"


def bulleted_preserving_code(text: str) -> list:
    """Bullet each line of a notes block, leaving fenced code untouched.

    Technical notes read best as bullets, but a ``` block must survive
    exactly as written — prefixing its lines with "* " (or dropping the
    blank lines inside it) turns a readable SQL snippet or JSON payload
    into nonsense, and breaks the fence so it renders as literal
    backticks.
    """
    rendered: list = []
    in_code = False
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith(CODE_FENCE):
            in_code = not in_code
            rendered.append(stripped)
            continue
        if in_code:
            rendered.append(raw_line)  # verbatim, blank lines and indentation included
            continue
        if not stripped:
            continue
        rendered.append(stripped if stripped.startswith(("*", "-")) else f"* {stripped}")

    # An unbalanced fence would swallow the rest of the description.
    if in_code:
        rendered.append(CODE_FENCE)
    return rendered


def criterion_text(criterion) -> str:
    """One-line Gherkin rendering — ClickUp keeps criteria compact, unlike
    Linear's bulleted house format (see routes_linear._format_description)."""
    if criterion.text:
        return f"{criterion.name}: {criterion.text}" if criterion.name else criterion.text
    clause = f"Dado que {criterion.given}, cuando {criterion.when}, entonces {criterion.then}."
    return f"{criterion.name}: {clause}" if criterion.name else clause


def format_clickup_description(ticket) -> str:
    """Render the team's standard ticket description template:
    epic/title header, UI route + backend endpoint, a Spanish user story +
    concrete description, optional visual-references placeholder, numbered
    Gherkin acceptance criteria (plus a trailing placeholder for more), and
    optional technical notes."""
    epic = ticket.epic or ticket.title.upper()
    lines = [
        f"📄 USER STORY: {epic}",
        f"Título: {ticket.title}",
        "",
        f"📍 Ruta / Vista UI: {ticket.ui_route or '(no aplica)'}",
        f"🔌 Endpoint Backend: {ticket.backend_endpoint or '(no aplica)'}",
        "",
        "📝 DESCRIPCIÓN",
    ]
    if ticket.user_story:
        lines.append(ticket.user_story)
        lines.append("")
    lines.append(ticket.description)
    lines += ["", "🖼️ RECURSOS VISUALES Y REFERENCIAS (OPCIONAL)"]
    existing = getattr(ticket, "visual_resources", None) or []
    if existing:
        lines.extend(existing)
    else:
        lines.append(
            "- No se proporcionaron recursos visuales; agregar capturas, diagramas o enlaces de referencia si están disponibles."
        )
    lines += ["", "✅ CRITERIOS DE ACEPTACIÓN"]
    for index, criterion in enumerate(ticket.acceptance_criteria, start=1):
        lines.append(f"📌 Criterio {index}: {criterion_text(criterion)}")
    lines.append("📌 Criterio X: [Espacio para criterios adicionales]")
    lines += [
        "",
        "🛠️ NOTAS TÉCNICAS Y ADICIONALES (opcional)",
        ticket.technical_notes or "(sin notas adicionales)",
    ]
    return "\n".join(lines)


def format_linear_description(ticket) -> str:
    """Linear's house ticket format, followed strictly.

    Deliberately NOT shared with the ClickUp renderer: the two trackers
    present tickets differently on purpose, and this one has to match the
    team's template exactly — section order, the Como/quiero/para split
    across three lines, named criteria with Given/When/Then on their own
    bullets, the trailing "Criterio X" template, and bulleted technical
    notes.
    """
    epic = ticket.epic or ticket.title.upper()
    lines = [
        f"📄 USER STORY: {epic}",
        "",
        f"Título: {ticket.title}",
        "",
        f"📍 Ruta / Vista UI: {ticket.ui_route or '(no aplica)'}",
        "",
        f"🔌 Endpoint Backend: {ticket.backend_endpoint or '(no aplica)'}",
        "",
        "📝 DESCRIPCIÓN",
    ]

    # "Como …, / quiero …, / para …" each on its own line, per the template.
    if ticket.user_story:
        story_lines = [line.strip() for line in ticket.user_story.splitlines() if line.strip()]
        for story_line in story_lines:
            lines += [story_line, ""]
    lines += [ticket.description, ""]

    lines.append("🖼️ RECURSOS VISUALES Y REFERENCIAS (OPCIONAL)")
    existing = getattr(ticket, "visual_resources", None) or []
    if existing:
        # Carried over from the original ticket — never replaced by the
        # placeholder, or reformatting would destroy its screenshots.
        lines.extend(existing)
    else:
        lines += [
            "* Mockup / UI Route: [Link a Figma]",
            "* Diagrama / Adjuntos: [Adjuntar diagramas o capturas de referencia]",
        ]
    lines += [
        "",
        "- En general sería ideal agregar cualquier imagen de referencia.",
        "",
        "✅ CRITERIOS DE ACEPTACIÓN",
        "",
    ]
    for index, criterion in enumerate(ticket.acceptance_criteria, start=1):
        name = criterion.name or f"Criterio {index}"
        lines.append(f"📌 Criterio {index}: {name}")
        if criterion.given or criterion.when or criterion.then:
            lines += [
                f"* Dado que {criterion.given},",
                f"* cuando {criterion.when},",
                f"* entonces {criterion.then}.",
            ]
        elif criterion.text:
            lines.append(f"* {criterion.text}")
        lines.append("")

    lines += [
        "📌 Criterio X: [Espacio para criterios adicionales]",
        "* Dado que [condición inicial],",
        "* cuando [acción o evento],",
        "* entonces [resultado esperado].",
        "",
        "🛠️ NOTAS TÉCNICAS Y ADICIONALES (opcional)",
    ]
    if ticket.technical_notes:
        lines.extend(bulleted_preserving_code(ticket.technical_notes))
    else:
        lines.append("* (sin notas adicionales)")

    return "\n".join(lines)


# Images in existing tickets come as markdown with long signed URLs, or
# occasionally as raw <img> tags. Both are matched so reformatting can carry
# them across verbatim instead of silently dropping them.
_IMAGE_MARKDOWN = re.compile(r"!\[[^\]]*\]\([^)\s]+(?:\s+\"[^\"]*\")?\)")
_IMAGE_HTML = re.compile(r"<img\s[^>]*?src=[\"'][^\"']+[\"'][^>]*>", re.IGNORECASE)
# A bare attachment/upload link that isn't in image syntax but still points at
# a file worth keeping.
# The (?<!!) guard matters: without it this also matches the inner half of a
# markdown image, reporting every image twice.
_ATTACHMENT_LINK = re.compile(
    r"(?<!!)\[[^\]]+\]\((https?://[^)\s]*(?:uploads|attachments|files)[^)\s]*)\)", re.IGNORECASE
)


def extract_visual_resources(description: str) -> list:
    """Pull every image and attachment reference out of a ticket body.

    Deliberately done in code, not by the model: these are long signed URLs
    that have to survive byte-for-byte, and asking an LLM to copy them back
    verbatim is exactly how an image gets quietly corrupted or dropped.
    Order is preserved and duplicates are removed.
    """
    if not description:
        return []
    found: list = []
    for pattern in (_IMAGE_MARKDOWN, _IMAGE_HTML, _ATTACHMENT_LINK):
        for match in pattern.finditer(description):
            item = match.group(0).strip()
            if item not in found:
                found.append(item)
    return found
