"""Contract for PIIDetector: every adapter must pass every test here (ADR 0008).

Synthetic values only: made-up names, example.com, the standard test card and IBAN, Spain's
textbook NIF, RENAPO's and Banxico's published examples, an SSN long void, and sequences with a
computed check digit. Every market's IDs are caught with a valid check digit and left alone with a
wrong one; places, dates and amounts survive. Colombia's and Peru's join with their markets.
The detector is built and run with sockets blocked: it must never reach the network.
"""

import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from adapters.shared.presidio import PresidioDetector
from core.pii import DetectorUnavailable, PIISpan
from core.ports import PIIDetector
from core.redaction import MIN_SCORE, REDACTED_TYPES, Redactor


def refuse(*args: Any, **kwargs: Any) -> None:
    raise AssertionError("the detector tried to open a network connection")


@pytest.fixture(scope="module")
def offline(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    import tldextract
    import tldextract.tldextract

    with pytest.MonkeyPatch.context() as patch:
        # Start as a new container does: tldextract's default extractor, which fetches the suffix
        # list, with an empty cache (a developer's ~/.cache would otherwise hide the fetch).
        empty = str(tmp_path_factory.mktemp("tldextract"))
        patch.setattr(
            tldextract.tldextract, "TLD_EXTRACTOR", tldextract.TLDExtract(cache_dir=empty)
        )
        patch.setattr(socket.socket, "connect", refuse)
        patch.setattr(socket, "create_connection", refuse)
        yield


@pytest.fixture(params=[pytest.param(PresidioDetector, id="presidio")], scope="module")
def detector(request: pytest.FixtureRequest, offline: None) -> PIIDetector:
    build: type[PIIDetector] = request.param
    return build()


def found(detector: PIIDetector, text: str) -> dict[str, set[str]]:
    """What redaction would act on: each type's values at or above the threshold."""
    spans: dict[str, set[str]] = {}
    for span in detector.detect(text):
        if span.score >= MIN_SCORE:
            spans.setdefault(span.entity_type, set()).add(text[span.start : span.end])
    return spans


@pytest.mark.parametrize(
    ("text", "entity_type", "value"),
    [
        ("Me llamo María Fernández y tengo una duda.", "PERSON", "María Fernández"),
        ("Escríbeme a maria.f@example.com, por favor.", "EMAIL_ADDRESS", "maria.f@example.com"),
        ("Mi tarjeta es 4111 1111 1111 1111.", "CREDIT_CARD", "4111 1111 1111 1111"),
        ("Mi IBAN es ES91 2100 0418 4502 0005 1332.", "IBAN_CODE", "ES91 2100 0418 4502 0005 1332"),
        ("Mi NIF es 12345678Z.", "ES_NIF", "12345678Z"),
        ("Llámame al +52 55 1234 5678.", "PHONE_NUMBER", "+52 55 1234 5678"),
        ("Mi celular es 55 1234 5678.", "PHONE_NUMBER", "55 1234 5678"),
        ("Mi teléfono es 7012-3456.", "PHONE_NUMBER", "7012-3456"),
        ("Mi número de seguro social es 219-09-9999.", "US_SSN", "219-09-9999"),
        ("Mi seguro social: 219099999.", "US_SSN", "219099999"),
        ("Mi ITIN es 900-70-1234.", "US_ITIN", "900-70-1234"),
        ("Mi CURP es HEGG560427MVZRRL04.", "MX_CURP", "HEGG560427MVZRRL04"),
        ("mi curp: xaxx010101hdfxxx06", "MX_CURP", "xaxx010101hdfxxx06"),
        ("La CLABE es 032180000118359719.", "MX_CLABE", "032180000118359719"),
        ("CLABE 012 345 67890123456 8", "MX_CLABE", "012 345 67890123456 8"),
        ("Mi DUI es 12345678-4.", "SV_DUI", "12345678-4"),
        ("DUI 123456784", "SV_DUI", "123456784"),
        ("Mi DUI es 12345678 4.", "SV_DUI", "12345678 4"),
        ("Mi tarjeta es 4111.1111.1111.1111.", "CREDIT_CARD", "4111.1111.1111.1111"),
        ("4111/1111/1111/1111", "CREDIT_CARD", "4111/1111/1111/1111"),
        ("es 219099999", "US_SSN", "219099999"),
        ("La CLABE es 032-180-00011835971-9.", "MX_CLABE", "032-180-00011835971-9"),
        ("clabe 032.180.00011835971.9", "MX_CLABE", "032.180.00011835971.9"),
        ("HOLA SOY MARIA FERNANDEZ Y TENGO UNA DUDA", "PERSON", "MARIA FERNANDEZ"),
        ("ME LLAMO ANA RUIZ", "PERSON", "ANA RUIZ"),
        ("Mi nombre es MARIA FERNANDEZ LOPEZ", "PERSON", "MARIA FERNANDEZ LOPEZ"),
    ],
)
def test_it_finds_each_type(detector: PIIDetector, text: str, entity_type: str, value: str) -> None:
    assert value in found(detector, text).get(entity_type, set())


@pytest.mark.parametrize(
    "text",
    [
        "Mi tarjeta es ４１１１１１１１１１１１１１１１.",
        "Mi tarjeta es 4111​1111​1111​1111.",
        "Mi tarjeta es 4­1­1­1 1111 1111 1111.",
        "Mi tarjeta es 4111‑1111‑1111‑1111.",
        "Mi tarjeta es 4111 1111 1111 1111.",
    ],
)
def test_hidden_characters_do_not_hide_a_number(detector: PIIDetector, text: str) -> None:
    result = Redactor(detector).redact(text)
    assert result.text == "Mi tarjeta es <CREDIT_CARD_1>."


def test_an_answer_cannot_hide_a_number_with_hidden_characters(detector: PIIDetector) -> None:
    scan = Redactor(detector).output_scanner()
    shown = scan.feed("Tu tarjeta ４１１１ 1111​ 1111 1111 quedó bloqueada.") + scan.finish()
    assert shown == "Tu tarjeta [dato protegido] quedó bloqueada."


def knowledge_text() -> list[str]:
    """Every smoke question and doc heading: real policy language, with no personal data in it."""
    root = Path(__file__).resolve().parents[2] / "knowledge"
    smoke = yaml.safe_load((root / "smoke.yaml").read_text(encoding="utf-8"))
    questions = [q["question"] for q in smoke["questions"]] + smoke["out_of_scope"]
    headings = [
        line.lstrip("# ").strip()
        for path in sorted((root / "docs").rglob("*.md"))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("#")
    ]
    return sorted({*questions, *headings})


def test_policy_language_is_never_redacted(detector: PIIDetector) -> None:
    redactor = Redactor(detector)
    changed = {t: redactor.redact(t).text for t in knowledge_text() if redactor.redact(t).counts}
    assert changed == {}


@pytest.mark.parametrize(
    "text",
    [
        "Quiero bloquear mi Tarjeta de Crédito",
        "Tarjeta de Débito",
        "hola",
        "hola, buenos días",
        "buenos dias, tengo una duda",
        "¿Puedo pagar con Zelle?",
        "QUIERO BLOQUEAR MI TARJETA DE CREDITO",
        "NO SIRVE LA APP",
        "YA TE DIJE QUE NO",
        "NECESITO UN AGENTE",
        "¿El límite es de 1,500,000 pesos?",
        "El pago vence el 2026.11.05 a las 9.30",
    ],
)
def test_everyday_messages_are_not_redacted(detector: PIIDetector, text: str) -> None:
    assert Redactor(detector).redact(text).text == text


def test_a_name_next_to_a_greeting_still_goes(detector: PIIDetector) -> None:
    assert "Ana" not in Redactor(detector).redact("Hola Ana").text


@pytest.mark.parametrize(
    "text",
    [
        "Mi tarjeta es 4111.1111.1111.1112.",  # wrong Luhn digit
        "es 000099999",  # area never issued
        "es 666099999",
        "es 219009999",  # group never issued
        "es 219090000",  # serial never issued
    ],
)
def test_numbers_that_cannot_be_that_id_are_not_flagged_as_it(
    detector: PIIDetector, text: str
) -> None:
    assert not {"CREDIT_CARD", "US_SSN"} & found(detector, text).keys()


@pytest.mark.parametrize(
    ("text", "entity_type"),
    [
        ("Mi CURP es HEGG560427MVZRRL05.", "MX_CURP"),
        ("La CLABE es 032180000118359718.", "MX_CLABE"),
        ("Mi DUI es 12345678-5.", "SV_DUI"),
    ],
)
def test_an_id_with_a_wrong_check_digit_is_not_that_id(
    detector: PIIDetector, text: str, entity_type: str
) -> None:
    assert entity_type not in found(detector, text)


def test_a_card_number_with_a_wrong_check_digit_is_not_a_card(detector: PIIDetector) -> None:
    assert "CREDIT_CARD" not in found(detector, "Mi tarjeta es 4111 1111 1111 1112.")


def test_places_dates_and_amounts_are_never_a_redacted_type(detector: PIIDetector) -> None:
    text = "Quiero enviar 500 dólares de Los Ángeles a El Salvador el 3 de mayo, o a México."
    leaked = {
        value
        for kind, values in found(detector, text).items()
        if kind in REDACTED_TYPES
        for value in values
    }
    assert leaked == set()


def test_spans_point_inside_the_text_with_a_score(detector: PIIDetector) -> None:
    text = "Soy María Fernández, maria.f@example.com"
    spans = detector.detect(text)
    assert spans
    for span in spans:
        assert isinstance(span, PIISpan)
        assert 0 <= span.start < span.end <= len(text)
        assert 0.0 <= span.score <= 1.0


def test_text_without_pii_gives_no_redacted_spans(detector: PIIDetector) -> None:
    spans = detector.detect("¿Cuánto cuesta enviar dinero a otro país?")
    assert {s.entity_type for s in spans if s.score >= MIN_SCORE} & REDACTED_TYPES == set()


def test_with_the_core_rules_a_whole_message_keeps_only_what_the_policy_question_needs(
    detector: PIIDetector,
) -> None:
    text = (
        "Hola, soy María Fernández, mi CURP es HEGG560427MVZRRL04 y mi correo maria.f@example.com. "
        "¿Puedo enviar 500 dólares a El Salvador el 3 de mayo?"
    )
    result = Redactor(detector).redact(text)
    assert result.text == (
        "Hola, soy <PERSON_1>, mi CURP es <MX_CURP_1> y mi correo <EMAIL_ADDRESS_1>. "
        "¿Puedo enviar 500 dólares a El Salvador el 3 de mayo?"
    )


def test_it_fails_closed() -> None:
    broken = PresidioDetector.__new__(PresidioDetector)
    broken._engine = None  # type: ignore[assignment]
    with pytest.raises(DetectorUnavailable):
        broken.detect("Mi correo es maria.f@example.com")


# detect_patterns: the output scan, with no NER.


@pytest.mark.parametrize(
    ("text", "entity_type", "value"),
    [
        ("Te escribimos a maria.f@example.com.", "EMAIL_ADDRESS", "maria.f@example.com"),
        ("Tu tarjeta 4111 1111 1111 1111 está bloqueada.", "CREDIT_CARD", "4111 1111 1111 1111"),
        ("Te llamaremos al +52 55 1234 5678.", "PHONE_NUMBER", "+52 55 1234 5678"),
        ("Tu número 219-09-9999 está protegido.", "US_SSN", "219-09-9999"),
        ("Tu CURP HEGG560427MVZRRL04 está registrada.", "MX_CURP", "HEGG560427MVZRRL04"),
        ("La CLABE 032180000118359719 es correcta.", "MX_CLABE", "032180000118359719"),
        ("Tu DUI 12345678-4 está registrado.", "SV_DUI", "12345678-4"),
        ("Tu IBAN ES91 2100 0418 4502 0005 1332.", "IBAN_CODE", "ES91 2100 0418 4502 0005 1332"),
        ("Tu NIF 12345678Z.", "ES_NIF", "12345678Z"),
    ],
)
def test_the_pattern_scan_finds_each_id_type(
    detector: PIIDetector, text: str, entity_type: str, value: str
) -> None:
    spans = detector.detect_patterns(text)
    assert value in {text[s.start : s.end] for s in spans if s.entity_type == entity_type}
    assert all(s.score >= MIN_SCORE for s in spans if s.entity_type == entity_type)


def test_the_pattern_scan_never_reports_names(detector: PIIDetector) -> None:
    spans = detector.detect_patterns("Claro, María Fernández, con gusto te ayudo.")
    assert "PERSON" not in {s.entity_type for s in spans}


def test_the_pattern_scan_fails_closed() -> None:
    broken = PresidioDetector.__new__(PresidioDetector)
    broken._patterns = [None]  # type: ignore[assignment]
    with pytest.raises(DetectorUnavailable):
        broken.detect_patterns("Tu correo es maria.f@example.com")


def test_an_answer_streamed_one_character_at_a_time_leaks_nothing(detector: PIIDetector) -> None:
    secrets = ["4111 1111 1111 1111", "maria.f@example.com", "HEGG560427MVZRRL04", "12345678-4"]
    answer = (
        f"Listo: la tarjeta {secrets[0]} quedó bloqueada y te avisaremos a {secrets[1]}. "
        f"Tu CURP {secrets[2]} y tu DUI {secrets[3]} no cambian. ¿Algo más, <PERSON_1>?"
    )
    scan = Redactor(detector).output_scanner()
    shown = "".join(scan.feed(char) for char in answer) + scan.finish()
    assert not any(secret in shown for secret in secrets)
    assert "<PERSON_1>" not in shown
    assert shown.startswith("Listo: la tarjeta [dato protegido] quedó bloqueada")
    assert shown.endswith("¿Algo más, [dato protegido]?")


def test_presidio_loads_without_a_warning_per_skipped_recognizer(
    offline: None, caplog: pytest.LogCaptureFixture
) -> None:
    # Presidio's own: about 20 lines on every cold start, for other languages' recognizers.
    import logging

    with caplog.at_level(logging.WARNING, logger="presidio-analyzer"):
        PresidioDetector()
        # Back to warnings once loaded, so detection still warns. Checked inside the block, since
        # leaving it puts back the level pytest found.
        assert logging.getLogger("presidio-analyzer").level == logging.WARNING

    assert "Recognizer not added to registry" not in caplog.text
