"""A CLI run that fails while reporting success must not become content."""

import pytest

from meta_harness.claude_output import (
    ClaudeUnavailableError,
    assert_usable,
    detect_cli_failure,
)

FAILURES = [
    "Credit balance is too low to run this request.",
    "You have insufficient credits. Please purchase more credits to continue.",
    "Error: usage limit reached for this account.",
    "Rate limit exceeded, try again later.",
    "Your quota has been exceeded.",
    "Not authenticated. Please run `claude login` to continue.",
    "Invalid API key provided.",
    "Authentication failed.",
    "You have exceeded your monthly usage.",
]

REAL_ANSWERS = [
    '[{"title": "Alta de persona", "description": "Formulario de registro."}]',
    '{"verdict": "related", "confidence": 0.9, "rationale": "toca el módulo"}',
    "El formulario muestra los campos obligatorios y valida el correo.",
]


@pytest.mark.parametrize("text", FAILURES)
def test_operational_messages_are_detected(text):
    assert detect_cli_failure(text) is not None


@pytest.mark.parametrize("text", REAL_ANSWERS)
def test_real_answers_pass_through(text):
    assert detect_cli_failure(text) is None
    assert assert_usable(text) == text


def test_empty_output_is_left_to_the_caller():
    """Not a billing problem, and each caller already says something clearer
    about it in terms of what it was doing."""
    assert detect_cli_failure("") is None
    assert detect_cli_failure("   \n ") is None


def test_a_ticket_about_billing_is_not_mistaken_for_a_failure():
    """The system under test handles payroll and contracts; tickets mentioning
    credits and quotas are ordinary content."""
    ticket = (
        '[{"title": "Registrar la modalidad de pago del colaborador", '
        '"description": "El sistema debe permitir registrar si el pago es por planilla, '
        'recibo por honorarios o factura, e indicar el saldo de credito disponible del '
        'cliente y su cuota mensual asignada."}]'
    )
    assert detect_cli_failure(ticket) is None


def test_the_error_names_the_cause_not_the_prompt():
    with pytest.raises(ClaudeUnavailableError) as exc:
        assert_usable("Credit balance is too low.", action="ticket generation")
    message = str(exc.value)
    assert "credits" in message
    assert "ticket generation" in message
    # The point of this class: not a parse error, so nobody goes prompt-hunting.
    assert "JSON" not in message


def test_failure_text_far_into_a_long_answer_is_ignored():
    """Only the head is inspected, so a long legitimate answer that happens to
    discuss limits is not rejected."""
    long_answer = "Contenido válido del ticket. " * 60 + "usage limit reached"
    assert detect_cli_failure(long_answer) is None
