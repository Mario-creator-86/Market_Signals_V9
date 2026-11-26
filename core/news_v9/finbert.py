"""Modern Transformers adapter for ProsusAI FinBERT.

The legacy ProsusAI repository used ``pytorch_pretrained_bert``.  V9 loads the
published model through ``transformers.pipeline`` and pins both model revision
and runtime versions in configuration/requirements.  Import and model loading
are lazy so market ingestion remains available when the optional inference
runtime is unhealthy; the fallback is explicitly labelled and never presented
as a FinBERT result.
"""
from __future__ import annotations

import logging
import math
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from core.sentiment import SentimentEngine


logger = logging.getLogger("FinBERT")
Classifier = Callable[..., Any]

# All torch work happens on this one thread. torch's OpenMP runtime starts a team
# of worker threads for every new thread that runs the model and never gives it
# back; news batches ask from short-lived threads, so the server went from 114
# threads to 5,112 in 12 hours (34 more for each new asking thread).
_MODEL_THREAD = ThreadPoolExecutor(max_workers=1, thread_name_prefix="finbert-model")


def _on_model_thread(work: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run ``work`` on the model thread and hand back its result (or raise its error)."""
    return _MODEL_THREAD.submit(work, *args, **kwargs).result()


@dataclass(frozen=True)
class FinancialSentimentResult:
    label: str
    score: float
    confidence: float
    source: str
    model: str = ""
    revision: str = ""
    fallback_used: bool = False
    available: bool = True
    error: str = ""


class FinBERTAnalyzer:
    """Thread-safe, lazy FinBERT classifier with an injectable test seam."""

    def __init__(
        self,
        *,
        model: str,
        revision: str,
        classifier: Optional[Classifier] = None,
        local_files_only: bool = True,
        max_chars: int = 4_000,
    ):
        if not str(model or "").strip() or not str(revision or "").strip():
            raise ValueError("FinBERT model and immutable revision are required")
        self.model = str(model).strip()
        self.revision = str(revision).strip()
        self.local_files_only = bool(local_files_only)
        self.max_chars = max(64, int(max_chars))
        self._classifier = classifier
        self._load_attempted = classifier is not None
        self._load_error = ""
        self._lock = threading.RLock()

    def _load(self) -> Optional[Classifier]:
        with self._lock:
            if self._classifier is not None:
                return self._classifier
            if self._load_attempted:
                return None
            self._load_attempted = True
            if str(os.getenv("MS_PROCESS_ROLE", "") or "").strip().lower() == "engine":
                # The API process owns the sentiment model. The trading engine
                # (a second process) must never load a second copy of it.
                self._load_error = "FinBERT is not loaded in the engine process"
                return None
            try:
                self._classifier = _on_model_thread(self._build_classifier)
            except Exception as exc:  # optional runtime/model cache is allowed to be absent
                self._load_error = f"{type(exc).__name__}: {exc}"
                logger.warning("FinBERT unavailable; labelled fallback will be used: %s", self._load_error)
            return self._classifier

    def _build_classifier(self) -> Classifier:
        from transformers import pipeline

        return pipeline(
            "text-classification",
            model=self.model,
            revision=self.revision,
            local_files_only=self.local_files_only,
        )

    @staticmethod
    def _normalize_output(raw: Any) -> tuple[str, float]:
        if isinstance(raw, list) and raw:
            raw = raw[0]
        if not isinstance(raw, dict):
            raise ValueError("FinBERT classifier returned an invalid payload")
        label = str(raw.get("label") or "").strip().lower()
        aliases = {
            "label_0": "positive",
            "label_1": "negative",
            "label_2": "neutral",
            "positive": "positive",
            "negative": "negative",
            "neutral": "neutral",
        }
        label = aliases.get(label, label)
        if label not in {"positive", "negative", "neutral"}:
            raise ValueError(f"unsupported FinBERT label: {label or '<empty>'}")
        confidence = float(raw.get("score"))
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            raise ValueError("FinBERT confidence must be finite in [0, 1]")
        return label, confidence

    def analyze(self, text: str) -> FinancialSentimentResult:
        classifier = self._load()
        if classifier is None:
            return FinancialSentimentResult(
                label="neutral",
                score=0.0,
                confidence=0.0,
                source="finbert",
                model=self.model,
                revision=self.revision,
                available=False,
                error=self._load_error or "FinBERT classifier unavailable",
            )
        try:
            raw = _on_model_thread(classifier, str(text or "")[: self.max_chars], truncation=True)
            label, confidence = self._normalize_output(raw)
            signed_score = confidence if label == "positive" else -confidence if label == "negative" else 0.0
            return FinancialSentimentResult(
                label=label,
                score=signed_score,
                confidence=confidence,
                source="finbert",
                model=self.model,
                revision=self.revision,
            )
        except Exception as exc:
            return FinancialSentimentResult(
                label="neutral",
                score=0.0,
                confidence=0.0,
                source="finbert",
                model=self.model,
                revision=self.revision,
                available=False,
                error=f"{type(exc).__name__}: {exc}",
            )


class FinancialSentimentService:
    """Use FinBERT when available; otherwise retain the proven V7 lexicon."""

    def __init__(self, analyzer: Optional[FinBERTAnalyzer], *, fallback_enabled: bool = True):
        self.analyzer = analyzer
        self.fallback_enabled = bool(fallback_enabled)

    def analyze(self, text: str) -> FinancialSentimentResult:
        if self.analyzer is not None:
            result = self.analyzer.analyze(text)
            if result.available:
                return result
            finbert_error = result.error
        else:
            finbert_error = "FinBERT disabled"

        if not self.fallback_enabled:
            return FinancialSentimentResult(
                label="neutral", score=0.0, confidence=0.0, source="unavailable",
                available=False, error=finbert_error,
            )
        sentiment, raw_score, confidence = SentimentEngine.analyze(text)
        label = sentiment.value
        if label.startswith("very_"):
            label = label[5:]
        return FinancialSentimentResult(
            label=label,
            score=max(-1.0, min(1.0, float(raw_score) / 3.0)),
            confidence=max(0.0, min(1.0, float(confidence))),
            source="sentiment_lexicon",
            fallback_used=True,
            error=finbert_error,
        )


_SERVICE_LOCK = threading.RLock()
_SERVICE_CACHE: Dict[str, FinancialSentimentService] = {}


def get_financial_sentiment_service(config: Optional[Dict[str, Any]] = None) -> FinancialSentimentService:
    from core.config import DEFAULT_CONFIG

    if config is None:
        from core.config import ConfigManager

        config = dict(getattr(ConfigManager(), "_config", {}) or {})
    defaults = dict(DEFAULT_CONFIG.get("news_v9") or {})
    supplied = dict((config or {}).get("news_v9") or {})
    settings = {**defaults, **supplied}
    finbert = {**dict(defaults.get("finbert") or {}), **dict(supplied.get("finbert") or {})}
    key = repr(sorted(finbert.items())) + f"|fallback={bool(settings.get('fallback_enabled', True))}"
    with _SERVICE_LOCK:
        cached = _SERVICE_CACHE.get(key)
        if cached is not None:
            return cached
        analyzer = None
        if bool(finbert.get("enabled", True)):
            analyzer = FinBERTAnalyzer(
                model=str(finbert.get("model") or ""),
                revision=str(finbert.get("revision") or ""),
                local_files_only=bool(finbert.get("local_files_only", True)),
                max_chars=int(finbert.get("max_chars", 4_000)),
            )
        service = FinancialSentimentService(
            analyzer,
            fallback_enabled=bool(settings.get("fallback_enabled", True)),
        )
        _SERVICE_CACHE[key] = service
        return service
