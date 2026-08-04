"""El ejecutor, contra aplicaciones de mentira que fallan de formas concretas."""

import json
import threading
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from e2e_driver import chromium_available
from meta_harness.qa.environments import resolve_environment
from meta_harness.qa.plan import Expectation, TestCase, TestPlan, TestStep
from meta_harness.qa.report import render_markdown
from meta_harness.qa.runner import run_plan

pytestmark = pytest.mark.skipif(
    not chromium_available(), reason="Chromium and/or websockets not available"
)

# Tres aplicaciones. Se ven iguales; solo una funciona.
PAGE = """<!doctype html><html><body>
<h1>Alta de persona</h1>
<input id="nombre">
<button id="guardar">Guardar</button>
<div id="salida"></div>
<script>
document.getElementById('guardar').addEventListener('click', async () => {
  %s
});
</script>
</body></html>"""

# Guarda y avisa — la correcta.
GOOD = """
  const r = await fetch('/api/v1/talent/people', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify({nombre: document.getElementById('nombre').value})});
  if (r.ok) document.getElementById('salida').textContent = 'Persona registrada';
"""
# Avisa sin guardar — el falso positivo clásico.
LIAR = """
  document.getElementById('salida').textContent = 'Persona registrada';
"""
# Guarda pero el servidor falla, y aun así avisa.
BROKEN_API = """
  await fetch('/api/v1/talent/people', {method:'POST'});
  document.getElementById('salida').textContent = 'Persona registrada';
"""


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        body = PAGE % self.server.script
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body.encode())))
        self.end_headers()
        self.wfile.write(body.encode())

    def do_POST(self):
        status = self.server.api_status
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        payload = json.dumps({"ok": status < 400}).encode()
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class FakeApp:
    def __init__(self, script, api_status=201):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.script = script
        self.httpd.api_status = api_status
        self.port = self.httpd.server_address[1]

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


def _plan():
    return TestPlan(
        ticket_id="SIG-109",
        ticket_title="Formulario de alta de persona",
        route="/",
        derived_from="criterios de aceptación",
        cases=[
            TestCase(
                name="alta-persona",
                criterion="Dado un formulario válido, cuando se confirme, entonces la persona queda registrada",
                steps=[
                    TestStep("goto", "/"),
                    TestStep("type", "#nombre", "Ana Pérez"),
                    TestStep("click_text", "Guardar"),
                    TestStep("wait_text", "Persona registrada"),
                ],
                expectation=Expectation(
                    ui_text="Persona registrada",
                    api_path="/api/v1/talent/people",
                    api_method="POST",
                ),
            )
        ],
    )


def _run(app, tmp_path):
    env = resolve_environment("local", ui_url=app.url)
    return run_plan(_plan(), env, evidence_root=tmp_path)


def test_a_working_feature_passes(tmp_path):
    with FakeApp(GOOD) as app:
        run = _run(app, tmp_path)
    case = run.cases[0]
    assert case.passed
    assert case.ui_ok and case.api_ok
    assert run.pass_rate == 1.0


def test_a_screen_that_lies_is_caught(tmp_path):
    """Muestra «Persona registrada» sin guardar nada. La UI pasa, la API no."""
    with FakeApp(LIAR) as app:
        run = _run(app, tmp_path)
    case = run.cases[0]
    assert not case.passed
    assert case.ui_ok, "la UI efectivamente muestra el texto"
    assert not case.api_ok, "pero nunca llamó al endpoint"
    assert "nunca llamó" in case.api_detail


def test_an_api_that_fails_is_caught(tmp_path):
    """Llama al endpoint, recibe 500, y aun así dice que guardó."""
    with FakeApp(BROKEN_API, api_status=500) as app:
        run = _run(app, tmp_path)
    case = run.cases[0]
    assert not case.passed
    assert case.ui_ok
    assert not case.api_ok
    assert "500" in case.api_detail


def test_a_missing_button_fails_the_step_not_the_run(tmp_path):
    plan = _plan()
    plan.cases[0].steps[2] = TestStep("click_text", "Botón que no existe")
    with FakeApp(GOOD) as app:
        env = resolve_environment("local", ui_url=app.url)
        run = run_plan(plan, env, evidence_root=tmp_path)
    case = run.cases[0]
    assert not case.passed
    assert "no se encontró" in case.why()
    # El paso que falló queda marcado, los anteriores no.
    assert [s.ok for s in case.steps] == [True, True, False]


def test_evidence_is_captured_per_step(tmp_path):
    with FakeApp(GOOD) as app:
        run = _run(app, tmp_path)
    shots = list(run.evidence_dir.glob("*.png"))
    assert len(shots) == 4, "una captura por paso"
    assert all(shot.stat().st_size > 0 for shot in shots)


def test_the_network_log_records_what_the_page_called(tmp_path):
    with FakeApp(GOOD) as app:
        run = _run(app, tmp_path)
    paths = [call["path"] for call in run.cases[0].api_calls]
    assert "/api/v1/talent/people" in paths


def test_one_failing_case_does_not_stop_the_others(tmp_path):
    plan = _plan()
    broken = TestCase(
        name="caso-roto",
        criterion="algo que no existe",
        steps=[TestStep("goto", "/"), TestStep("click_text", "Inexistente")],
        expectation=Expectation(ui_text="nada", api_path="/api/v1/talent/people"),
    )
    plan.cases.insert(0, broken)
    with FakeApp(GOOD) as app:
        env = resolve_environment("local", ui_url=app.url)
        run = run_plan(plan, env, evidence_root=tmp_path)
    assert len(run.cases) == 2
    assert not run.cases[0].passed
    assert run.cases[1].passed, "el segundo criterio se ejecutó igual"


# --- reporte ------------------------------------------------------------------


def test_the_report_puts_failures_first(tmp_path):
    with FakeApp(LIAR) as app:
        run = _run(app, tmp_path)
    md = render_markdown(run)
    assert md.index("Fallaron") < md.index("Un criterio pasa solo si") + len(md)
    assert "❌" in md
    assert "/api/v1/talent/people" in md
    assert "Persona registrada" in md


def test_the_report_shows_both_halves(tmp_path):
    with FakeApp(GOOD) as app:
        run = _run(app, tmp_path)
    md = render_markdown(run)
    assert "**UI:**" in md and "**API:**" in md


def test_the_report_links_the_evidence(tmp_path):
    with FakeApp(GOOD) as app:
        run = _run(app, tmp_path)
    md = render_markdown(run)
    assert ".png)" in md, "las capturas quedan enlazadas"
