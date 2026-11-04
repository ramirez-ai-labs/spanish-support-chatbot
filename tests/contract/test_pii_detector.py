"""Contract for PIIDetector: every adapter must pass every test here (ADR 0008).

Synthetic values only: made-up names, example.com, the standard test card and IBAN, and Spain's
textbook NIF. Each market's own IDs (SSN, CURP, CLABE, DUI) join this suite with their recognizers.
The detector is built and run with sockets blocked: it must never reach the network.
"""

import socket
from collections.abc import Iterator
from typing import Any

import pytest

from adapters.shared.presidio import PresidioDetector
from core.pii import DetectorUnavailable, PIISpan
from core.ports import PIIDetector

# The types redaction will remove (ADR 0008). Places, dates and amounts must never be one of them.
REDACTED = {
    "PERSON",
    "EMAIL_ADDRESS",
    "CREDIT_CARD",
    "IBAN_CODE",
    "PHONE_NUMBER",
    "ES_NIF",
    "ES_NIE",
}


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
    spans: dict[str, set[str]] = {}
    for span in detector.detect(text):
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
    ],
)
def test_it_finds_each_built_in_type(
    detector: PIIDetector, text: str, entity_type: str, value: str
) -> None:
    assert value in found(detector, text).get(entity_type, set())


def test_a_card_number_with_a_wrong_check_digit_is_not_a_card(detector: PIIDetector) -> None:
    assert "CREDIT_CARD" not in found(detector, "Mi tarjeta es 4111 1111 1111 1112.")


def test_places_dates_and_amounts_are_never_a_redacted_type(detector: PIIDetector) -> None:
    text = "Quiero enviar 500 dólares de Los Ángeles a El Salvador el 3 de mayo, o a México."
    leaked = {
        value
        for kind, values in found(detector, text).items()
        if kind in REDACTED
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
    assert {span.entity_type for span in spans} & REDACTED == set()


def test_it_fails_closed() -> None:
    broken = PresidioDetector.__new__(PresidioDetector)
    broken._engine = None  # type: ignore[assignment]
    with pytest.raises(DetectorUnavailable):
        broken.detect("Mi correo es maria.f@example.com")
