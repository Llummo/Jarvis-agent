"""Turn a run into something a person reads in thirty seconds.

Failures first, and each one says *why* in the order a person would look: did a
step break, did the screen not show it, or did the request never happen. A
report that lists twenty greens above one red buries the only line that matters.

Both halves of every criterion are shown even when they pass, because «UI ok /
API ok» is the claim being made and it should be visible, not implied.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

from meta_harness.qa.runner import CaseResult, RunResult

TICK = "✅"
CROSS = "❌"


def _case_block(case: CaseResult, evidence_dir: Path) -> List[str]:
    mark = TICK if case.passed else CROSS
    lines = [f"### {mark} {case.name}", ""]
    if case.criterion:
        lines += [f"> {case.criterion}", ""]

    lines += [
        f"- **UI:** {TICK if case.ui_ok else CROSS} {case.ui_detail}",
        f"- **API:** {TICK if case.api_ok else CROSS} {case.api_detail}",
    ]
    if not case.passed:
        lines.append(f"- **Motivo:** {case.why()}")
    lines.append("")

    lines.append("| # | Paso | Resultado | Evidencia |")
    lines.append("|---|---|---|---|")
    for index, step in enumerate(case.steps, start=1):
        evidence = ""
        if step.screenshot:
            name = Path(step.screenshot).name
            evidence = f"[{name}]({name})"
        status = TICK if step.ok else f"{CROSS} {step.error}"
        lines.append(f"| {index} | {step.description} | {status} | {evidence} |")
    lines.append("")

    if case.api_calls:
        lines.append("<details><summary>Llamadas de red observadas</summary>")
        lines.append("")
        lines.append("| Método | Ruta | Estado |")
        lines.append("|---|---|---|")
        for call in case.api_calls:
            lines.append(f"| {call['method']} | `{call['path']}` | {call['status'] or '—'} |")
        lines.append("")
        lines.append("</details>")
        lines.append("")
    return lines


def render_markdown(run: RunResult) -> str:
    """The run as a Markdown report, failures first."""
    env = run.environment
    lines = [
        f"# Pruebas de {run.ticket_id}" + (f" — {run.ticket_title}" if run.ticket_title else ""),
        "",
        f"**Resultado:** {run.summary_line()}",
        "",
        "| | |",
        "|---|---|",
        f"| Entorno | `{env.get('name')}` — {env.get('ui_url')} |",
        f"| Ruta | `{run.route}` |",
        f"| Ejecutado | {run.started_at} |",
        f"| Evidencia | `{run.evidence_dir}` |",
        "",
        "> Un criterio pasa solo si **la UI muestra lo esperado y el endpoint declarado respondió**.",
        "> Cualquiera de los dos por separado deja pasar features rotas.",
        "",
    ]

    if run.failed:
        lines += ["---", "", f"## {CROSS} Fallaron ({len(run.failed)})", ""]
        for case in run.failed:
            lines += _case_block(case, run.evidence_dir)

    if run.passed:
        lines += ["---", "", f"## {TICK} Pasaron ({len(run.passed)})", ""]
        for case in run.passed:
            lines += _case_block(case, run.evidence_dir)

    return "\n".join(lines).rstrip() + "\n"


def save_report(run: RunResult) -> Path:
    """Write the report next to the screenshots it references.

    Same directory on purpose: the relative links then work wherever the folder
    is moved or attached.
    """
    path = Path(run.evidence_dir) / "REPORTE.md"
    path.write_text(render_markdown(run), encoding="utf-8")
    return path
