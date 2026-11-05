"""Redaction rules (ADR 0008), with a scripted detector: which spans go, and what replaces them.

Synthetic values only: the published examples of a CURP (RENAPO) and a CLABE (Banxico), and
sequences with a computed check digit.
"""

import pytest

from core.pii import DetectorUnavailable, PIISpan
from core.redaction import (
    HOLDBACK,
    MIN_SCORE,
    PROTECTED,
    OutputScanner,
    Redactor,
    card_is_valid,
    clabe_is_valid,
    curp_is_valid,
    dui_is_valid,
    plain,
    ssn_is_valid,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("４１１１ １１１１", "4111 1111"),  # fullwidth digits and an ideographic space
        ("٤١١١", "4111"),  # another script's digits
        ("4111​1111­2222⁠", "411111112222"),  # zero-width, soft hyphen, word joiner
        ("4111\u00001111", "41111111"),  # a control character
        ("4111‑1111–1111", "4111-1111-1111"),  # non-breaking hyphen, en dash
        ("4111 1111", "4111 1111"),  # non-breaking space
        ("ｍａｒｉａ＠ｅｘａｍｐｌｅ．ｃｏｍ", "maria@example.com"),  # fullwidth letters and signs
        ("María", "María"),  # a decomposed accent composes
        ("4️111", "4111"),  # a variation selector
    ],
)
def test_plain_removes_what_can_hide_a_value(text: str, expected: str) -> None:
    assert plain(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "¿Cuánto cuesta enviar dinero a El Salvador? ñ, ü, «sí» y 1.º de mayo",
        "línea uno\nlínea dos\tcon tabulador",
        "Hola 😀",
        "",
    ],
)
def test_plain_keeps_ordinary_text_as_it_is(text: str) -> None:
    assert plain(text) == text


def test_what_is_detected_and_what_goes_on_is_the_plain_text() -> None:
    # A detector that only knows the ASCII digits catches a value typed in fullwidth.
    text, counts = redact("Mi tarjeta ４１１１", ("4111", "CREDIT_CARD", 1.0))
    assert text == "Mi tarjeta <CREDIT_CARD_1>"
    assert counts == {"CREDIT_CARD": 1}


@pytest.mark.parametrize(
    "word",
    ["Crédito", "credito", "DÉBITO", "hola", "Buenos Días", "buenos  dias", "Transferencias"],
)
def test_a_person_span_that_is_only_a_banking_word_or_greeting_stays(word: str) -> None:
    text, counts = redact(f"Tema: {word}.", (word, "PERSON", 0.85))
    assert text == f"Tema: {word}."
    assert counts == {}


def test_a_person_span_with_a_name_in_it_goes_even_next_to_those_words() -> None:
    text, _ = redact("Hola Ana", ("Hola Ana", "PERSON", 0.85))
    assert text == "<PERSON_1>"


def test_those_words_only_excuse_a_person_span() -> None:
    text, _ = redact("Mi correo: hola", ("hola", "EMAIL_ADDRESS", 1.0))
    assert text == "Mi correo: <EMAIL_ADDRESS_1>"


@pytest.mark.parametrize(
    "value", ["4111111111111111", "4111.1111.1111.1111", "4111/1111/1111/1111"]
)
def test_a_card_passes_the_luhn_check_whatever_separates_it(value: str) -> None:
    assert card_is_valid(value)


@pytest.mark.parametrize("value", ["4111111111111112", "411111111111", "41111111111111111111"])
def test_a_card_fails_on_a_wrong_digit_or_length(value: str) -> None:
    assert not card_is_valid(value)


def test_an_ssn_is_any_nine_digits_from_a_range_that_was_issued() -> None:
    assert ssn_is_valid("219099999")
    assert ssn_is_valid("219-09-9999")


@pytest.mark.parametrize(
    "value", ["000099999", "666099999", "919099999", "219009999", "219090000", "21909999", "1" * 10]
)
def test_an_ssn_fails_outside_those_ranges(value: str) -> None:
    assert not ssn_is_valid(value)


def test_clabe_accepts_dots_as_well_as_hyphens_and_spaces() -> None:
    assert clabe_is_valid("032.180.00011835971.9")
    assert clabe_is_valid("032-180-00011835971-9")
    assert clabe_is_valid("032 180 00011835971 9")


class ScriptedDetector:
    """Reports each listed value wherever it appears, as the given type and score."""

    def __init__(self, *found: tuple[str, str, float]) -> None:
        self.found = found

    def detect_patterns(self, text: str) -> list[PIISpan]:
        return self.detect(text)

    def detect(self, text: str) -> list[PIISpan]:
        spans = []
        for value, kind, score in self.found:
            start = text.find(value)
            while start != -1:
                spans.append(PIISpan(kind, start, start + len(value), score))
                start = text.find(value, start + 1)
        return spans


def redact(text: str, *found: tuple[str, str, float]) -> tuple[str, dict[str, int]]:
    result = Redactor(ScriptedDetector(*found)).redact(text)
    return result.text, dict(result.counts)


def test_each_value_becomes_a_numbered_placeholder() -> None:
    text, counts = redact(
        "Soy Ana Ruiz, mi correo es ana@example.com y mi hermana es Luz Ruiz.",
        ("Ana Ruiz", "PERSON", 0.85),
        ("Luz Ruiz", "PERSON", 0.85),
        ("ana@example.com", "EMAIL_ADDRESS", 1.0),
    )
    assert text == ("Soy <PERSON_1>, mi correo es <EMAIL_ADDRESS_1> y mi hermana es <PERSON_2>.")
    assert counts == {"PERSON": 2, "EMAIL_ADDRESS": 1}


def test_the_same_value_keeps_its_number_however_it_is_typed() -> None:
    text, _ = redact(
        "Mi tarjeta 4111 1111 1111 1111, repito: 4111-1111-1111-1111.",
        ("4111 1111 1111 1111", "CREDIT_CARD", 1.0),
        ("4111-1111-1111-1111", "CREDIT_CARD", 1.0),
    )
    assert text == "Mi tarjeta <CREDIT_CARD_1>, repito: <CREDIT_CARD_1>."


@pytest.mark.parametrize("kind", ["LOCATION", "DATE_TIME", "ORGANIZATION", "NRP", "URL"])
def test_places_dates_organizations_and_urls_stay(kind: str) -> None:
    text = "Quiero enviar 500 dólares a El Salvador."
    assert redact(text, ("El Salvador", kind, 0.85)) == (text, {})


def test_the_threshold_is_inclusive() -> None:
    text = "Llámame al 7012-3456."
    assert (
        redact(text, ("7012-3456", "PHONE_NUMBER", MIN_SCORE))[0] == "Llámame al <PHONE_NUMBER_1>."
    )
    assert redact(text, ("7012-3456", "PHONE_NUMBER", MIN_SCORE - 0.01))[0] == text


def test_overlapping_spans_keep_the_most_certain() -> None:
    text, counts = redact(
        "Mi DUI es 12345678-4.",
        ("12345678-4", "PHONE_NUMBER", 0.4),
        ("12345678-4", "SV_DUI", 1.0),
    )
    assert text == "Mi DUI es <SV_DUI_1>."
    assert counts == {"SV_DUI": 1}


def test_with_equal_scores_the_longer_span_wins() -> None:
    text, _ = redact(
        "Soy María José Pérez.",
        ("María José", "PERSON", 0.85),
        ("María José Pérez", "PERSON", 0.85),
    )
    assert text == "Soy <PERSON_1>."


def test_the_summary_names_types_and_counts_never_values() -> None:
    result = Redactor(ScriptedDetector(("ana@example.com", "EMAIL_ADDRESS", 1.0))).redact(
        "ana@example.com y ana@example.com"
    )
    assert result.summary() == "EMAIL_ADDRESS:2"
    assert "ana" not in result.summary()


def test_a_detector_failure_reaches_the_caller() -> None:
    class Broken:
        def detect(self, text: str) -> list[PIISpan]:
            raise DetectorUnavailable("RuntimeError")

    with pytest.raises(DetectorUnavailable):
        Redactor(Broken()).redact("hola")


def one_digit_changed(value: str) -> list[str]:
    """Every way to change the last character to another digit."""
    return [value[:-1] + d for d in "0123456789" if d != value[-1]]


@pytest.mark.parametrize(
    ("check", "valid"),
    [
        (curp_is_valid, "HEGG560427MVZRRL04"),  # RENAPO's published example
        (curp_is_valid, "XAXX010101HDFXXX06"),
        (clabe_is_valid, "032180000118359719"),  # Banxico's published example
        (clabe_is_valid, "012345678901234568"),
        (dui_is_valid, "12345678-4"),
    ],
)
def test_check_digits(check: object, valid: str) -> None:
    assert callable(check)
    assert check(valid)
    assert check(valid.lower())
    assert not any(check(other) for other in one_digit_changed(valid))


def test_check_digits_accept_separators_and_refuse_other_lengths() -> None:
    assert clabe_is_valid("012 345 67890123456 8")
    assert dui_is_valid("123456784")
    assert not clabe_is_valid("01234567890123456")
    assert not dui_is_valid("1234567-4")
    assert not curp_is_valid("HEGG560427MVZRRL0")


# The output scan: the model's answer, scanned as it streams.

CARD = "4111 1111 1111 1111"
ANSWER = (
    f"Listo, la tarjeta {CARD} quedó bloqueada. Te avisaremos a ana@example.com cuando la nueva "
    "esté en camino, <PERSON_1>. Si necesitas algo más, aquí estoy."
)
EXPECTED = (
    "Listo, la tarjeta [dato protegido] quedó bloqueada. Te avisaremos a [dato protegido] cuando "
    "la nueva esté en camino, [dato protegido]. Si necesitas algo más, aquí estoy."
)


def scanner() -> OutputScanner:
    detector = ScriptedDetector(
        (CARD, "CREDIT_CARD", 1.0), ("ana@example.com", "EMAIL_ADDRESS", 1.0)
    )
    return Redactor(detector).output_scanner()


def stream(text: str, size: int, scan: OutputScanner | None = None) -> tuple[list[str], str]:
    scan = scan or scanner()
    released = [scan.feed(text[i : i + size]) for i in range(0, len(text), size)]
    released.append(scan.finish())
    return released, "".join(released)


@pytest.mark.parametrize("size", [1, 2, 3, 5, 7, 13, 64, 65, 80, len(ANSWER)])
def test_however_the_answer_is_split_the_page_gets_the_same_safe_text(size: int) -> None:
    assert stream(ANSWER, size)[1] == EXPECTED


def test_it_counts_what_it_replaced_by_type() -> None:
    scan = scanner()
    stream(ANSWER, 4, scan)
    assert dict(scan.counts) == {"CREDIT_CARD": 1, "EMAIL_ADDRESS": 1, "PLACEHOLDER": 1}


def test_an_answer_without_pii_passes_unchanged() -> None:
    text = "Para bloquear tu tarjeta, entra en la app y elige «Bloquear». " * 3
    assert stream(text, 9)[1] == text


def test_it_holds_back_only_the_last_characters_while_streaming() -> None:
    released, _ = stream("a" * 200, 10)
    assert released[0] == ""  # nothing until more than HOLDBACK characters have arrived
    assert len("".join(released[:-1])) == 200 - HOLDBACK
    assert released[-1] == "a" * HOLDBACK


def test_a_match_across_the_cut_waits_until_it_is_whole() -> None:
    scan = scanner()
    text = "x" * (HOLDBACK + 10) + CARD  # the card sits across the release point
    out = scan.feed(text) + scan.finish()
    assert CARD not in out
    assert out == "x" * (HOLDBACK + 10) + PROTECTED


def test_a_scan_failure_reaches_the_caller() -> None:
    class Broken:
        def detect(self, text: str) -> list[PIISpan]:
            return []

        def detect_patterns(self, text: str) -> list[PIISpan]:
            raise DetectorUnavailable("RuntimeError")

    scan = Redactor(Broken()).output_scanner()
    with pytest.raises(DetectorUnavailable):
        scan.feed("x" * (HOLDBACK + 1))
