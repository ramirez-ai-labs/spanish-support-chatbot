"""PIIDetector: Presidio with spaCy's Spanish `es_core_news_md`, run in the container (ADR 0008).

Both are pinned in `uv.lock` (the model as a wheel with its SHA-256) and installed into the image,
so instances never download a model. Spanish is the only language.

- **Built in for `es`:** names (spaCy NER), email, cards (Luhn), IBAN, and Spain's NIF and NIE.
- **Registered for Spanish here:** Presidio's US SSN and ITIN recognizers (English-only by
  default) with Spanish context words, and phone numbers for the live markets' regions.
- **Ours:** Mexico's CURP and CLABE and El Salvador's DUI. Each match must pass its check digit
  (`core.redaction`), so a number with a wrong one is left alone.

Every recognizer runs on every message, whatever the market: a Mexican customer in the US market
can type a CURP. The core decides which types are redacted.

No network at runtime: Presidio's email check calls `tldextract`, which downloads the Public
Suffix List on first use (seconds, and an outbound call from the hot path). `_offline_tldextract`
swaps in an extractor that reads only the snapshot bundled with the package.

    python -m adapters.shared.presidio check   # load, time it, and catch PII in a synthetic message
"""

import logging
import resource
import sys
import time
from typing import Any

from core.pii import DetectorUnavailable, PIISpan
from core.redaction import clabe_is_valid, curp_is_valid, dui_is_valid

LANGUAGE = "es"
SPACY_MODEL = "es_core_news_md"
PHONE_REGIONS = ("US", "MX", "SV", "ES")  # the live markets; a country added adds its region

# Mexico's 32 state codes plus NE (born abroad), the CURP's characters 12-13.
_CURP_STATES = (
    "AS|BC|BS|CC|CL|CM|CS|CH|DF|DG|GT|GR|HG|JC|MC|MN|"
    "MS|NT|NL|OC|PL|QT|QR|SP|SL|SR|TC|TS|TL|VZ|YN|ZS|NE"
)
CURP_REGEX = (
    r"\b[A-Z][AEIOUX][A-Z]{2}\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])[HMX]"
    rf"(?:{_CURP_STATES})[B-DF-HJ-NP-TV-Z]{{3}}[A-Z\d]\d\b"
)


def _offline_tldextract() -> None:
    """Point `tldextract.extract`, which reads this module global on every call, at the snapshot."""
    import tldextract
    import tldextract.tldextract

    tldextract.tldextract.TLD_EXTRACTOR = tldextract.TLDExtract(
        suffix_list_urls=(), cache_dir=None, fallback_to_snapshot=True
    )


def _recognizers() -> list[Any]:
    """The recognizers this adapter adds for Spanish, beyond Presidio's built-in `es` ones."""
    from presidio_analyzer import Pattern, PatternRecognizer
    from presidio_analyzer.predefined_recognizers import (
        PhoneRecognizer,
        UsItinRecognizer,
        UsSsnRecognizer,
    )

    class CheckedRecognizer(PatternRecognizer):
        """A pattern that only counts when its check digit is right: then score 1, else dropped."""

        def __init__(self, entity: str, patterns: list[Any], context: list[str], check: Any):
            super().__init__(
                entity, patterns=patterns, context=context, supported_language=LANGUAGE
            )
            self._check = check

        def validate_result(self, pattern_text: str) -> bool:
            return bool(self._check(pattern_text))

    return [
        CheckedRecognizer("MX_CURP", [Pattern("curp", CURP_REGEX, 0.5)], ["curp"], curp_is_valid),
        CheckedRecognizer(
            "MX_CLABE",
            [
                Pattern("clabe", r"\b\d{18}\b", 0.3),
                Pattern("clabe spaced", r"\b\d{3} \d{3} \d{11} \d\b", 0.3),
            ],
            ["clabe", "interbancaria", "transferencia"],
            clabe_is_valid,
        ),
        CheckedRecognizer(
            "SV_DUI",
            [Pattern("dui", r"\b\d{8}-\d\b", 0.5), Pattern("dui digits", r"\b\d{9}\b", 0.1)],
            ["dui", "documento", "identidad"],
            dui_is_valid,
        ),
        UsSsnRecognizer(supported_language=LANGUAGE, context=["seguro", "social", "ssn"]),
        UsItinRecognizer(supported_language=LANGUAGE, context=["itin", "contribuyente"]),
        PhoneRecognizer(
            supported_language=LANGUAGE,
            supported_regions=PHONE_REGIONS,
            context=["teléfono", "telefono", "celular", "móvil", "whatsapp", "llamar", "número"],
        ),
    ]


class PresidioDetector:
    def __init__(self) -> None:
        # Imported here: spaCy and the model take a few seconds and ~300 MB, and only this adapter
        # needs them.
        from presidio_analyzer import AnalyzerEngine, RecognizerRegistry
        from presidio_analyzer.nlp_engine import NlpEngineProvider

        # One line per recognizer on every cold start, including other countries' that never run
        # on Spanish text: noise in the logs.
        logging.getLogger("presidio-analyzer").setLevel(logging.WARNING)
        _offline_tldextract()
        nlp = NlpEngineProvider(
            nlp_configuration={
                "nlp_engine_name": "spacy",
                "models": [{"lang_code": LANGUAGE, "model_name": SPACY_MODEL}],
            }
        ).create_engine()
        registry = RecognizerRegistry(supported_languages=[LANGUAGE])
        registry.load_predefined_recognizers(languages=[LANGUAGE], nlp_engine=nlp)
        registry.remove_recognizer("PhoneRecognizer", language=LANGUAGE)  # ours has the regions
        for recognizer in _recognizers():
            registry.add_recognizer(recognizer)
        self._engine = AnalyzerEngine(
            nlp_engine=nlp, registry=registry, supported_languages=[LANGUAGE]
        )
        # For the output scan: every recognizer except spaCy's NER, run without the NLP pipeline.
        # Typed Any: Presidio's hints require NLP artifacts, which pattern recognizers never read.
        self._patterns: list[Any] = [
            r
            for r in registry.get_recognizers(language=LANGUAGE, all_fields=True)
            if type(r).__name__ != "SpacyRecognizer"
        ]
        # The first run loads the rest lazily: pay for it at startup, not on a customer's message.
        # The address reaches tldextract, so a setup that would go to the network fails here.
        self.detect("hola, hola@example.com")

    def detect(self, text: str) -> list[PIISpan]:
        try:
            results = self._engine.analyze(text=text, language=LANGUAGE)
        except Exception as exc:  # whatever breaks inside spaCy or a recognizer, fail closed
            raise DetectorUnavailable(type(exc).__name__) from exc
        return [PIISpan(r.entity_type, r.start, r.end, r.score) for r in results]

    def detect_patterns(self, text: str) -> list[PIISpan]:
        try:
            results = [
                result
                for recognizer in self._patterns
                for result in recognizer.analyze(
                    text, recognizer.supported_entities, nlp_artifacts=None
                )
                or []
            ]
        except Exception as exc:  # fail closed, as detect does
            raise DetectorUnavailable(type(exc).__name__) from exc
        return [PIISpan(r.entity_type, r.start, r.end, r.score) for r in results]


def peak_rss_mb() -> int:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss  # KB on Linux, bytes on macOS
    return int(peak / 1e6 if sys.platform == "darwin" else peak / 1e3)


# Synthetic only: a made-up name, the example.com domain, and a standard test card number.
CHECK_TEXT = (
    "Hola, soy María Fernández. Mi correo es maria.f@example.com y mi tarjeta es "
    "4111 1111 1111 1111. Quiero enviar 500 dólares a El Salvador."
)
CHECK_EXPECTED = {"PERSON", "EMAIL_ADDRESS", "CREDIT_CARD"}


def check() -> None:
    started, before = time.perf_counter(), peak_rss_mb()
    detector = PresidioDetector()
    load_ms = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    spans = detector.detect(CHECK_TEXT)
    detect_ms = (time.perf_counter() - started) * 1000
    found = {span.entity_type for span in spans}
    print(
        f"presidio {SPACY_MODEL}: load {load_ms:.0f} ms, detect {detect_ms:.1f} ms, "
        f"peak RSS {peak_rss_mb()} MB (+{peak_rss_mb() - before} MB), found {sorted(found)}"
    )
    missing = CHECK_EXPECTED - found
    if missing:
        sys.exit(f"missed {sorted(missing)}")


if __name__ == "__main__":
    if sys.argv[1:] != ["check"]:
        sys.exit(__doc__)
    check()
