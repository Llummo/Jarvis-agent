"""Pruebas de navegador basadas en lo que el ticket promete.

El flujo de QA anterior podía decir «la ruta responde» y adjuntar una foto.
Esto ejecuta los criterios de aceptación: hace clic, escribe, y comprueba que
la pantalla muestre lo esperado **y** que el endpoint declarado haya respondido.

Híbrido a propósito. Un modelo explora una vez y escribe un plan; el plan se
ejecuta mecánicamente desde entonces. Entre los dos queda un documento que se
puede leer y corregir antes de que nada toque la base de datos.

Solo contra `local` y el entorno de QA desplegado. Producción no es un destino
válido: estas pruebas escriben datos de verdad.
"""

from meta_harness.qa.browser import Browser, BrowserError, NetworkCall
from meta_harness.qa.environments import (
    ENVIRONMENTS,
    ENV_LOCAL,
    ENV_QA,
    Environment,
    resolve_environment,
)
from meta_harness.qa.plan import (
    ACTIONS,
    Expectation,
    PlanError,
    TestCase,
    TestPlan,
    TestStep,
)
from meta_harness.qa.report import render_markdown, save_report
from meta_harness.qa.runner import CaseResult, RunResult, StepOutcome, run_plan

__all__ = [
    "ACTIONS",
    "Browser",
    "BrowserError",
    "CaseResult",
    "ENVIRONMENTS",
    "ENV_LOCAL",
    "ENV_QA",
    "Environment",
    "Expectation",
    "NetworkCall",
    "PlanError",
    "RunResult",
    "StepOutcome",
    "TestCase",
    "TestPlan",
    "TestStep",
    "render_markdown",
    "resolve_environment",
    "run_plan",
    "save_report",
]
