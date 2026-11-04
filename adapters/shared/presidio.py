"""PIIDetector: Presidio with spaCy's Spanish `es_core_news_md`, run in the container (ADR 0008).

Both are pinned in `uv.lock` (the model as a wheel with its SHA-256) and installed into the image,
so instances never download a model. Spanish is the only language. Built in for `es`: names
(spaCy NER), email, cards (Luhn), IBAN, phone, and Spain's NIF and NIE. Each market's own IDs
(SSN, CURP, CLABE, DUI) arrive with redaction, the next M3 slice.

No network at runtime: Presidio's email check calls `tldextract`, which downloads the Public
Suffix List on first use (seconds, and an outbound call from the hot path). `_offline_tldextract`
swaps in an extractor that reads only the snapshot bundled with the package.

    python -m adapters.shared.presidio check   # load, time it, and catch PII in a synthetic message
"""

import resource
import sys
import time

from core.pii import DetectorUnavailable, PIISpan

LANGUAGE = "es"
SPACY_MODEL = "es_core_news_md"


def _offline_tldextract() -> None:
    """Point `tldextract.extract`, which reads this module global on every call, at the snapshot."""
    import tldextract
    import tldextract.tldextract

    tldextract.tldextract.TLD_EXTRACTOR = tldextract.TLDExtract(
        suffix_list_urls=(), cache_dir=None, fallback_to_snapshot=True
    )


class PresidioDetector:
    def __init__(self) -> None:
        # Imported here: spaCy and the model take a few seconds and ~300 MB, and only this adapter
        # needs them.
        from presidio_analyzer import AnalyzerEngine, RecognizerRegistry
        from presidio_analyzer.nlp_engine import NlpEngineProvider

        _offline_tldextract()
        nlp = NlpEngineProvider(
            nlp_configuration={
                "nlp_engine_name": "spacy",
                "models": [{"lang_code": LANGUAGE, "model_name": SPACY_MODEL}],
            }
        ).create_engine()
        registry = RecognizerRegistry(supported_languages=[LANGUAGE])
        registry.load_predefined_recognizers(languages=[LANGUAGE], nlp_engine=nlp)
        self._engine = AnalyzerEngine(
            nlp_engine=nlp, registry=registry, supported_languages=[LANGUAGE]
        )
        # The first run loads the rest lazily: pay for it at startup, not on a customer's message.
        # The address reaches tldextract, so a setup that would go to the network fails here.
        self.detect("hola, hola@example.com")

    def detect(self, text: str) -> list[PIISpan]:
        try:
            results = self._engine.analyze(text=text, language=LANGUAGE)
        except Exception as exc:  # whatever breaks inside spaCy or a recognizer, fail closed
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
