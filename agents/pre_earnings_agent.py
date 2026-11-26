import copy
import contextvars
import hashlib
import json
import logging
import math
import os
import re
import threading
import time
from urllib.parse import urlparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any, Dict, List, Optional, Tuple

from agents.economic_calendar_agent import (
    get_economic_calendar_agent,
    get_economic_calendar_agent_if_ready,
)
from agents.news_tab_agent import get_news_tab_agent, get_news_tab_agent_if_ready
from core.agent_toggles import is_agent_enabled
from core.agent_metrics import track_agent_run
from core.config import DATA_DIR, ConfigManager, STATE_PATH
from core.stock_data_client import StockDataClient
from core.market_hours import earnings_session_reported, is_pre_earnings_entry_window
from core.ai.role_resolution import call_timeout_for
from core.ollama_client import ollama
from core.request_context import get_request_context
from core.pre_earnings.score import (
    DIRECTION_INPUT_MAX_SHARE_MULTIPLE, cap_direction_input_shares,
    compute_data_confidence, compute_pre_earnings_score,
)
from core.pre_earnings.decision import decision_inputs_fingerprint, evaluate_pre_earnings_decision

logger = logging.getLogger("PreEarningsAgent")

# Unlike ordinary common-word tickers, bare uppercase DAO remains ambiguous in
# finance/crypto headlines because it is the standard acronym for a
# decentralized autonomous organization. Require explicit exchange syntax or
# Youdao/provider corroboration for this symbol.
AMBIGUOUS_FINANCE_ACRONYM_TICKERS = {"DAO"}

# Dedupe state for provider-failure warnings (see _log_provider_error_once).
_PROVIDER_ERROR_LOG_STATE: Dict[str, float] = {}


def _log_provider_error_once(
    channel: str,
    symbol: str,
    exc: BaseException,
    window_seconds: float = 600.0,
) -> None:
    """Surface provider failures with per-signature dedupe.

    Provider calls are wrapped in broad ``except Exception`` so a broken
    dependency (e.g. yfinance's missing ``lxml``, which silently zeroed
    GapRisk for 85% of the radar) or a rate limit degrades coverage instead of
    crashing the cycle. That resilience must not hide the failure: emit one
    warning per (channel, error signature) per window so an infrastructure
    break can never again masquerade as "No historical quarters".
    """
    try:
        signature = f"{channel}:{type(exc).__name__}:{str(exc)[:120]}"
        now = time.monotonic()
        if now - _PROVIDER_ERROR_LOG_STATE.get(signature, -1e12) < window_seconds:
            return
        _PROVIDER_ERROR_LOG_STATE[signature] = now
        logger.warning(
            "Provider call failed (%s) symbol=%s: %s: %s — serving cache/empty; "
            "radar coverage degrades until this is fixed",
            channel,
            symbol,
            type(exc).__name__,
            str(exc)[:200],
        )
    except Exception:
        pass


PRE_EARNINGS_OUTPUT_PATH = DATA_DIR / "pre_earnings.json"
PRE_EARNINGS_FEEDBACK_PATH = DATA_DIR / "pre_earnings_feedback.json"
PRE_EARNINGS_AI_CACHE_PATH = DATA_DIR / "pre_earnings_ai_cache.json"
PRE_EARNINGS_PULSE_HISTORY_PATH = Path(
    os.getenv("PRE_EARNINGS_PULSE_HISTORY_PATH")
    or (DATA_DIR / "pre_earnings_pulse_history.json")
)

# Curated universe of liquid US large-caps so the pre-earnings radar covers the broad
# market's upcoming reporters, not just watchlist tickers. Earnings dates are resolved
# via the economic-calendar agent (yfinance, cached ~6h), so this is free (no API key).
PRE_EARNINGS_UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "AMD", "NFLX",
    "INTC", "CSCO", "ORCL", "CRM", "ADBE", "QCOM", "TXN", "IBM", "NOW", "UBER",
    "JPM", "BAC", "WFC", "GS", "MS", "C", "V", "MA", "AXP", "PYPL",
    "JNJ", "UNH", "LLY", "PFE", "MRK", "ABBV", "TMO", "AMGN",
    "XOM", "CVX", "COP", "WMT", "COST", "HD", "TGT", "LOW",
    "DIS", "KO", "PEP", "MCD", "NKE", "SBUX", "BA", "CAT", "GE", "HON",
    "MU", "MRVL", "SMCI", "PLTR", "COIN", "SHOP", "SNOW", "DELL",
]

# Peer read-through theme clusters (owner requirement, 2026-11). A pure
# `sector` join misses groups that trade as ONE theme — live: HUT/MARA/CLSK/
# RIOT/WULF classify as "Financial Services" while CIFR is "Technology", yet
# all are BTC miners whose earnings read through to each other. The peer-key
# chain therefore starts with this curated override, then industry, then
# sector. Keep the map CONSERVATIVE: only clusters that genuinely trade as
# one. Extendable via config `pre_earnings.peer_theme_overrides`
# (symbol -> theme key), merged over these defaults.
PEER_THEME_OVERRIDES: Dict[str, str] = {
    # BTC / AI-datacenter miners (HUT & CIFR & co — the owner's example).
    "HUT": "BTC_MINERS",
    "CIFR": "BTC_MINERS",
    "MARA": "BTC_MINERS",
    "CLSK": "BTC_MINERS",
    "RIOT": "BTC_MINERS",
    "WULF": "BTC_MINERS",
    "IREN": "BTC_MINERS",
    "BITF": "BTC_MINERS",
    "CORZ": "BTC_MINERS",
    "APLD": "BTC_MINERS",
    "BTBT": "BTC_MINERS",
    "SDIG": "BTC_MINERS",
    "ARBK": "BTC_MINERS",
    "HIVE": "BTC_MINERS",
    "BTDR": "BTC_MINERS",
    # AI hardware / optical / quantum high-beta cluster.
    "AAOI": "AI_HARDWARE_QUANTUM",
    "ALAB": "AI_HARDWARE_QUANTUM",
    "SNDK": "AI_HARDWARE_QUANTUM",
    "IONQ": "AI_HARDWARE_QUANTUM",
    "RGTI": "AI_HARDWARE_QUANTUM",
    "QBTS": "AI_HARDWARE_QUANTUM",
    "QUBT": "AI_HARDWARE_QUANTUM",
    "SMCI": "AI_HARDWARE_QUANTUM",
    "MRVL": "AI_HARDWARE_QUANTUM",
    "COHR": "AI_HARDWARE_QUANTUM",
    "LITE": "AI_HARDWARE_QUANTUM",
}

PEER_THEME_LABELS: Dict[str, str] = {
    "BTC_MINERS": "BTC Miners",
    "AI_HARDWARE_QUANTUM": "AI Hardware & Quantum",
}

NEGATIVE_SEVERITY_TERMS = {
    "offering": 1.0,
    "dilution": 1.0,
    "sec": 0.9,
    "investigation": 0.9,
    "restatement": 1.0,
    "fraud": 1.0,
    "lawsuit": 0.8,
    "guidance cut": 0.9,
    "ceo resigns": 0.8,
    "downgrade": 0.6,
}

COMMODITY_EXCLUDED_SYMBOLS = {
    "GC=F", "SI=F",
    "XAUUSD", "XAGUSD",
    "XAU-USD", "XAG-USD",
    "XAUUSD=X", "XAGUSD=X",
    "GLD", "SLV", "IAU", "SIVR",
}

POSITIVE_SEVERITY_TERMS = {
    "fda approval": 1.0,
    "major contract": 0.9,
    "acquisition": 0.8,
    "raises guidance": 0.9,
    "strategic partnership": 0.8,
    "upgrade": 0.6,
}

POSITIVE_SENTIMENT_TERMS = {
    "beats": 0.6,
    "beat": 0.6,
    "raises guidance": 0.8,
    "strong demand": 0.5,
    "approval": 0.7,
    "contract": 0.5,
    "partnership": 0.4,
    "upgrade": 0.7,
}

NEGATIVE_SENTIMENT_TERMS = {
    "misses": -0.7,
    "miss": -0.7,
    "cuts guidance": -0.8,
    "guidance cut": -0.8,
    "offering": -0.9,
    "dilution": -0.9,
    "investigation": -0.8,
    "fraud": -1.0,
    "lawsuit": -0.7,
    "downgrade": -0.7,
    "sec": -0.6,
}

ANALYST_POSITIVE_TERMS = ("upgrade", "outperform", "overweight", "buy rating", "price target raised")
ANALYST_NEGATIVE_TERMS = ("downgrade", "underweight", "sell rating", "price target cut")
EARNINGS_MENTION_RE = re.compile(
    r"\b("
    r"reports?\s+q[1-4]\b|"
    r"q[1-4]\s+(earnings|results)\b|"
    r"(earnings|results)\s+(call|transcript|preview|report)\b|"
    r"(eps|revenue)\s+(beat|beats|miss|misses|guidance)\b|"
    r"quarterly\s+results\b|"
    r"fiscal\s+q[1-4]\b"
    r")",
    re.IGNORECASE,
)
EARNINGS_FUTURE_CUE_RE = re.compile(
    r"\b("
    r"preview|"
    r"on the horizon|"
    r"to report|"
    r"set to report|"
    r"due to report|"
    r"scheduled to report|"
    r"expected to report|"
    r"expected to (?:grow|beat|miss)|"
    r"earnings expected|"
    r"what to expect|"
    r"on deck|"
    r"should you buy|"
    r"analyst bets?|"
    r"(?:will|can|could) .{0,160}beat|"
    r"next earnings report|"
    r"beat estimates again|"
    r"announc(?:e|es|ed|ing) (?:the )?date|"
    r"results conference call on|"
    r"ahead of .{0,160}earnings|"
    r"before earnings|"
    r"upcoming earnings|"
    r"reports?\s+q[1-4]\b|"
    r"earnings\s+(preview|outlook)"
    r")",
    re.IGNORECASE,
)
EARNINGS_PAST_CUE_RE = re.compile(
    r"\b("
    r"earnings call transcript|"
    r"earnings call highlights|"
    r"surpass(?:es|ed)?|"
    r"beat(?:s|en)?|"
    r"miss(?:es|ed)?|"
    r"reported|"
    r"results?\s+(beat|beats|miss|missed|surpass(?:ed|es)?)|"
    r"post-earnings|"
    r"after earnings|"
    r"outlook\s+raised|"
    r"raised\s+(?:its\s+)?(?:guidance|outlook)|"
    r"record revenue"
    r")",
    re.IGNORECASE,
)
EARNINGS_RESULT_RELEASE_RE = re.compile(
    r"\b("
    r"announc(?:e|es|ed|ing)\s+(?:its\s+)?(?:first|second|third|fourth|q[1-4]|quarterly|fiscal).*results|"
    r"releas(?:e|es|ed|ing)\s+(?:its\s+)?(?:first|second|third|fourth|q[1-4]|quarterly|fiscal).*results|"
    r"reports?\s+(?:record\s+)?(?:first|second|third|fourth|q[1-4]|quarterly|fiscal).*results|"
    r"(?:first|second|third|fourth|q[1-4])[-\s]+quarter\s+(?:20\d{2}\s+)?(?:financial\s+)?results|"
    r"earnings\s+call\s+(?:transcript|highlights|summary)|"
    r"(?:eps|earnings|revenue|sales)\s+(?:beat|beats|miss|misses|surprise|surprises)"
    r")",
    re.IGNORECASE,
)
AI_INELIGIBLE_RE = re.compile(
    r"\b(already\s+reported|earnings\s+(?:has|have|had)\s+already\s+occurred|"
    r"no\s+longer\s+qualif(?:y|ies)|completed\s+event|"
    r"(?:earnings|results)\s+(?:were|was|have\s+been|has\s+been)\s+(?:released|reported))\b",
    re.IGNORECASE,
)


def _ai_decision_qualifies_pre_earnings(ai_decision: Optional[Dict[str, Any]]) -> bool:
    """Return event eligibility without overriding an explicit AI boolean.

    Rationales often describe historical *post-earnings reactions* as evidence
    for the upcoming event.  That wording is not proof that the current event
    has already happened.  The structured boolean is authoritative; rationale
    inference is only a compatibility fallback for legacy AI payloads.
    """
    if not isinstance(ai_decision, dict):
        return True
    explicit = ai_decision.get("qualifies_pre_earnings")
    if isinstance(explicit, bool):
        return explicit
    return not bool(AI_INELIGIBLE_RE.search(str(ai_decision.get("rationale") or "")))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat()


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _safe_float(value: Any, default: Optional[float] = 0.0) -> Optional[float]:
    try:
        if value is None:
            return default
        if isinstance(value, str):
            text = value.strip().replace("%", "").replace(",", "")
            if not text:
                return default
            return float(text)
        return float(value)
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _norm_text(value: Any) -> str:
    return str(value or "").strip().lower()


def _cap_bucket(market_cap: Optional[float]) -> str:
    cap = _safe_float(market_cap, None)
    if cap is None:
        return "UNKNOWN"
    if cap >= 10_000_000_000:
        return "MEGA"
    if cap >= 2_000_000_000:
        return "MID"
    return "SMALL"


def _quarter_key(dt: Optional[datetime] = None) -> str:
    now = dt or _utc_now()
    quarter = ((int(now.month) - 1) // 3) + 1
    return f"{int(now.year)}Q{quarter}"


def _is_earnings_eligible_symbol(symbol: str) -> bool:
    sym = str(symbol or "").upper().strip()
    if not sym:
        return False
    if sym in COMMODITY_EXCLUDED_SYMBOLS:
        return False
    if any(token in sym for token in ("-USD", "USDT", "=X", "=F")):
        return False
    if "/" in sym or ":" in sym:
        return False
    return bool(re.fullmatch(r"[A-Z]{1,6}(\.[A-Z])?", sym))


def _headline_blob(row: Dict[str, Any]) -> str:
    return " ".join(
        [
            str(row.get("title") or ""),
            str(row.get("summary") or row.get("snippet") or ""),
            str(row.get("text") or ""),
        ]
    ).strip()


def _headline_short_blob(row: Dict[str, Any]) -> str:
    return " ".join(
        [
            str(row.get("title") or ""),
            str(row.get("summary") or row.get("snippet") or ""),
        ]
    ).strip()


_CRYPTO_ONLY_NEWS_PROVIDERS = {
    "theblock", "the block", "cryptopotato", "crypto_news", "crypto.news",
    "coindesk", "cointelegraph", "decrypt", "bitcoinist", "newsbtc",
}


def _is_crypto_only_equity_news(row: Dict[str, Any], symbol: str = "") -> bool:
    """Reject crypto-token collisions from the equity Pre-Earnings model."""
    if bool((row or {}).get("is_crypto")):
        return True
    provider = str(
        (row or {}).get("publisher")
        or (row or {}).get("source")
        or ""
    ).lower().strip()
    if provider not in _CRYPTO_ONLY_NEWS_PROVIDERS:
        return False
    sym = str(symbol or "").upper().strip()
    if not sym:
        return False
    text = _headline_short_blob(row)
    escaped = re.escape(sym)
    # Crypto publishers can still cover a listed company such as COIN. Keep
    # the row only when it contains an unambiguous equity-market identifier.
    explicit_equity = bool(
        re.search(rf"\${escaped}\b", text, flags=re.IGNORECASE)
        or re.search(
            rf"\b(?:NASDAQ|NYSE|AMEX|NYSEARCA|NYSEMKT|TSX)\s*:\s*{escaped}\b",
            text,
            flags=re.IGNORECASE,
        )
    )
    return not explicit_equity


def _extract_news_row_symbols(row: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    seen = set()

    def _push(raw: Any) -> None:
        token = str(raw or "").upper().strip()
        if not token or token in {"MARKET", "GENERAL"}:
            return
        if not _is_earnings_eligible_symbol(token):
            return
        if token in seen:
            return
        seen.add(token)
        out.append(token)

    _push(row.get("ticker"))
    _push(row.get("symbol"))
    for key in ("related_tickers", "symbols", "tickers"):
        value = row.get(key)
        if isinstance(value, list):
            for entry in value:
                _push(entry)
        elif isinstance(value, str):
            for entry in re.split(r"[,\s;|]+", value):
                _push(entry)
    return out


def _has_strict_symbol_mention(text: str, symbol: str) -> bool:
    if not text or not symbol:
        return False
    sym = str(symbol or "").upper().strip()
    if not sym:
        return False
    if re.search(rf"\${re.escape(sym)}\b", text, re.IGNORECASE):
        return True
    if re.search(
        rf"\((?:(?:NASDAQ|NYSE|AMEX|NYSEARCA|NYSEMKT)\s*:\s*)?{re.escape(sym)}\)",
        text,
    ):
        return True
    if re.search(
        rf"\b(?:NASDAQ|NYSE|AMEX|NYSEARCA|NYSEMKT)\s*:\s*{re.escape(sym)}\b",
        text,
    ):
        return True
    # A bare uppercase DAO most commonly means a crypto/decentralized
    # autonomous organization, not NYSE:DAO (Youdao).  The same fail-closed
    # rule applies to the shared ambiguous-word list: require an explicit
    # market-ticker form or company-name/provider metadata corroboration.
    if sym in AMBIGUOUS_FINANCE_ACRONYM_TICKERS:
        return False
    return bool(re.search(rf"\b{re.escape(sym)}\b", text))


def _news_publisher(row: Dict[str, Any]) -> str:
    """Return the actual publisher/domain, not the ingestion channel."""
    for key in ("publisher", "publisher_name", "provider_name"):
        value = str(row.get(key) or "").strip().lower()
        if value:
            return value[:120]
    source = str(row.get("source") or "").strip().lower()
    ingestion_channels = {
        "yahoo", "yahoo_rss", "finnhub", "finnhub_news", "rss",
        "news_cache", "news_tab", "unknown",
    }
    if source and source not in ingestion_channels:
        return source[:120]
    url = str(row.get("url") or "").strip()
    try:
        host = (urlparse(url).hostname or "").lower().strip(".")
    except Exception:
        host = ""
    if host:
        for prefix in ("www.", "m.", "amp."):
            if host.startswith(prefix):
                host = host[len(prefix):]
        return host[:120]
    return source[:120] or "unknown"


def _explicit_tickers(text: str) -> set:
    if not text:
        return set()
    found = set()
    patterns = (
        r"\$([A-Z]{1,6}(?:\.[A-Z])?)\b",
        r"\((?:NASDAQ|NYSE|AMEX)?\s*:?\s*([A-Z]{1,6}(?:\.[A-Z])?)\)",
        r"\b(?:NASDAQ|NYSE|AMEX)\s*:\s*([A-Z]{1,6}(?:\.[A-Z])?)\b",
    )
    for pattern in patterns:
        found.update(re.findall(pattern, text))
    return {value.upper() for value in found if _is_earnings_eligible_symbol(value)}


def _is_future_earnings_context(text: str) -> bool:
    if not text:
        return False
    has_future = bool(EARNINGS_FUTURE_CUE_RE.search(text))
    has_past = bool(EARNINGS_PAST_CUE_RE.search(text))
    if has_future:
        return True
    if has_past:
        return False
    return False


def _score_historical_median_move(median_move_pct: Optional[float]) -> float:
    median = _safe_float(median_move_pct, None)
    if median is None:
        return 0.3
    if median >= 12:
        return 1.0
    if median >= 8:
        return 0.7
    if median >= 5:
        return 0.4
    return 0.2


def _score_news_intensity(news_count: int) -> float:
    if news_count >= 12:
        return 1.0
    if news_count >= 6:
        return 0.7
    if news_count >= 3:
        return 0.4
    return 0.2


def _score_implied_move(expected_move_pct: Optional[float]) -> Optional[float]:
    move = _safe_float(expected_move_pct, None)
    if move is None:
        return None
    if move >= 15:
        return 1.0
    if move >= 10:
        return 0.8
    if move >= 7:
        return 0.6
    if move >= 4:
        return 0.4
    return 0.2


def _weighted_avg(scores: Dict[str, Tuple[Optional[float], float]]) -> Optional[float]:
    total_weight = 0.0
    weighted_sum = 0.0
    for value, weight in scores.values():
        if value is None:
            continue
        total_weight += float(weight)
        weighted_sum += float(value) * float(weight)
    if total_weight <= 0:
        return None
    return weighted_sum / total_weight


def compute_gap_risk_score(
    historical_move_score: float,
    news_intensity_score: float,
    catalyst_severity_score: float,
    liquidity_risk_score: float,
    implied_move_score: Optional[float],
    weight_overrides: Optional[Dict[str, float]] = None,
) -> Tuple[int, List[Dict[str, Any]]]:
    base_weights = {
        "historical_move": 0.25,
        "news_intensity": 0.10,
        "catalyst_severity": 0.20,
        "liquidity_risk": 0.10,
        "implied_move": 0.35,
    }
    if isinstance(weight_overrides, dict):
        for key, value in weight_overrides.items():
            if key in base_weights:
                w = _safe_float(value, None)
                if w is not None and w > 0:
                    base_weights[key] = float(w)

    raw = {
        "historical_move": (historical_move_score, base_weights["historical_move"], "Median prior earnings move proxy"),
        "news_intensity": (news_intensity_score, base_weights["news_intensity"], "News volume in lookback window"),
        "catalyst_severity": (catalyst_severity_score, base_weights["catalyst_severity"], "Highest severity catalyst in cached headlines"),
        "liquidity_risk": (liquidity_risk_score, base_weights["liquidity_risk"], "Micro/small-cap and low-liquidity proxy"),
        "implied_move": (implied_move_score, base_weights["implied_move"], "Expected move into earnings"),
    }
    weighted = {
        key: (value, weight)
        for key, (value, weight, _note) in raw.items()
    }
    avg = _weighted_avg(weighted)
    if avg is None:
        avg = 0.3
    gap_risk = int(round(100 * _clamp(avg, 0.0, 1.0)))

    used_weight = sum(weight for value, weight in weighted.values() if value is not None) or 1.0
    contributors: List[Dict[str, Any]] = []
    for key, (value, weight, note) in raw.items():
        if value is None:
            continue
        contributors.append(
            {
                "name": key,
                "value": round(float(value), 4),
                "weight": round(float(weight) / used_weight, 4),
                "note": note,
            }
        )
    return gap_risk, contributors


def compute_direction_bias_score(
    symbol: str,
    enriched_data: dict,
    news_sentiment: float,
    retail: Optional[Dict[str, Any]] = None,
    retail_weight: float = 0.10,
    retail_sign: float = 1.0,
    retail_support_rank_ceiling: int = 20000,
) -> dict:
    # Component 1 - Historical Beat Rate (25%)
    # Returns None when no real historical EPS data exists → excluded from weighted avg.
    # Sample-support damping (audit P0-2): a single completed quarter is a coin
    # flip, not a 100% (1/1) or 0% (0/1) beat rate, so the raw rate is shrunk
    # toward the neutral 50 by support = min(1, samples/4) — quarter-strength
    # at 1 sample, half at 2, full strength only at >=4 completed quarters.
    # Deterministic and explainable; the raw rate itself stays visible on the
    # item via beat_rate_historical / beat_rate_samples.
    _beat_rate_raw = enriched_data.get("beat_rate_historical")
    _beat_samples = max(0, _safe_int(enriched_data.get("beat_rate_samples"), 0))
    if _beat_rate_raw is None:
        c1 = None
    else:
        _support = min(1.0, _beat_samples / 4.0)
        c1 = 50.0 + (float(_beat_rate_raw) * 100.0 - 50.0) * _support

    # Component 2 - Analyst Consensus (30%) — REAL data from yfinance:
    #   target-price upside (targetMeanPrice vs currentPrice) blended with the
    #   consensus rating (recommendationMean: 1=Strong Buy .. 5=Sell).
    # Returns None when no analyst coverage → excluded from weighted avg (not faked).
    _upside = enriched_data.get("analyst_target_upside_pct")
    _rec_mean = enriched_data.get("analyst_rec_mean")
    _c2_upside = max(0.0, min(100.0, 50.0 + float(_upside) * 1.5)) if _upside is not None else None
    _c2_rating = max(0.0, min(100.0, (5.0 - float(_rec_mean)) / 4.0 * 100.0)) if _rec_mean is not None else None
    if _c2_upside is not None and _c2_rating is not None:
        c2 = 0.65 * _c2_upside + 0.35 * _c2_rating
    elif _c2_upside is not None:
        c2 = _c2_upside
    elif _c2_rating is not None:
        c2 = _c2_rating
    else:
        c2 = None

    # Component 3 - Options Market Sentiment (25%)
    _calls_puts_raw = enriched_data.get("calls_vs_puts_ratio")
    c3 = (min(_calls_puts_raw / 2.0, 1.0) * 100) if _calls_puts_raw is not None else None

    # Component 4 - News AI Sentiment (20%) — always computable from news_sentiment
    c4 = ((news_sentiment + 1.0) / 2.0) * 100

    # Component 5 - Post-earnings history tilt (small weight): how the stock actually
    # reacted to its last few reports. Real & deterministic, but a weak predictor, so it
    # only nudges. tilt in [-1, 1] -> 0..100. None when no usable history.
    _tilt_raw = enriched_data.get("recent_reaction_tilt")
    c5 = ((float(_tilt_raw) + 1.0) / 2.0) * 100 if _tilt_raw is not None else None

    # Component 6 - Retail positioning (eToro crowd, small weight): buy/sell
    # holder skew as a confirming nudge. Damped by eToro popularity rank (the
    # client maps the holders field to the rank — a #300 name carries real
    # crowd evidence, a #30k one almost none; unknown rank → mild 0.4).
    # retail_sign lets the operator flip the crowd to contrarian once own
    # settled outcomes justify it; default 1.0 (skew confirms direction) at
    # a deliberately small weight (owner request 2026-09-05).
    c6 = None
    if retail is not None and retail_weight > 0:
        _buy = _safe_float(retail.get("buy_pct"), None)
        if _buy is not None:
            _sell = _safe_float(retail.get("sell_pct"), None)
            if _sell is None:
                _sell = 100.0 - _buy
            _skew = max(-100.0, min(100.0, _buy - _sell))
            _rank = _safe_int(retail.get("popularity_rank"), None)
            if _rank is not None:
                _support = max(0.2, min(1.0, 1.2 - _rank / float(retail_support_rank_ceiling)))
            else:
                _support = 0.4
            c6 = 50.0 + (_skew / 2.0) * _support * float(retail_sign)

    # Weighted average excluding missing (None) components.
    # Weights match the documented component intent above: c1 historical 25%,
    # c2 analyst consensus 30% (the strongest predictor — real yfinance analyst
    # data), c3 options 25%, c4 news 20%, c5 post-earnings history a small nudge,
    # c6 retail positioning the smallest nudge. c2 was previously 0.28,
    # contradicting its own "(30%)" spec comment and under-weighting the
    # highest-quality component; restored to 0.30. The sum is irrelevant —
    # final_bias divides by _total_w (available weight), so only the RELATIVE
    # weights matter.
    _components = {"c1_historical": (c1, 0.25), "c2_revision": (c2, 0.30), "c3_options": (c3, 0.25), "c4_news": (c4, 0.20), "c5_history": (c5, 0.12), "c6_retail": (c6, retail_weight)}
    _available = [(v, w) for v, w in _components.values() if v is not None]
    _total_w = sum(w for _, w in _available)
    # Share cap (wave 3): with most inputs missing, the remaining ones used to
    # absorb the whole score (retail positioning, nominal 0.10 of 1.22, became
    # a third of it). Each input is limited to DIRECTION_INPUT_MAX_SHARE_MULTIPLE
    # times its nominal share; the unassigned remainder counts as neutral 50.
    _capped = cap_direction_input_shares(_components)
    final_bias = float(_capped["score"]) if _total_w > 0 else 50.0

    direction = "BULLISH" if final_bias > 60 else ("BEARISH" if final_bias < 40 else "NEUTRAL")

    # Map to [-100, 100] legacy style for compatibility
    legacy_bias = (final_bias - 50) * 2

    # Track which components were missing — used for data_quality_flag and missing_reason
    missing = [k for k, (v, _) in _components.items() if v is None]
    data_quality_flag = "SUSPECT_INSUFFICIENT_DATA" if len(missing) >= 2 else None
    missing_reason = f"Missing components: {', '.join(missing)}" if missing else None

    return {
        "bias_score": round(legacy_bias, 2),
        "bias_score_100": round(final_bias, 2),
        "direction": direction,
        "components": {
            "historical": c1,
            "analyst_revision": round(c2, 4) if c2 is not None else None,
            "options": round(c3, 4) if c3 is not None else None,
            "news_ai": round(c4, 4),
            "post_earnings_history": round(c5, 4) if c5 is not None else None,
            "retail_positioning": round(c6, 4) if c6 is not None else None,
        },
        "missing_components": missing,
        "data_quality_flag": data_quality_flag,
        "missing_reason": missing_reason,
        "available_weight": round(_total_w, 4),
        # Share-cap disclosure: how much of the score real inputs carry.
        "nominal_weight": _capped.get("nominal_weight"),
        "input_shares": _capped.get("shares", {}),
        "input_share_cap_applied": bool(_capped.get("cap_applied")),
        "input_share_cap_multiple": DIRECTION_INPUT_MAX_SHARE_MULTIPLE,
        "evidence_factor": _capped.get("evidence_factor", 1.0),
        "uncapped_bias_score": round((float(_capped.get("uncapped_score", 50.0)) - 50.0) * 2.0, 2),
    }


def direction_label_from_score(score: int) -> str:
    val = int(_safe_int(score, 0))
    if val >= 15:
        return "LONG"
    if val <= -15:
        return "SHORT"
    return "NEUTRAL"


def direction_bias_score_from_item(item: Dict[str, Any]) -> int:
    row = item if isinstance(item, dict) else {}
    score = _safe_int(row.get("direction_bias_score"), None)
    if score is not None:
        return int(_clamp(score, -100, 100))
    normalized = _safe_float(row.get("direction_bias"), None)
    if normalized is not None:
        if -1.0 <= normalized <= 1.0:
            return int(round(_clamp(normalized, -1.0, 1.0) * 100.0))
        return int(_clamp(normalized, -100.0, 100.0))
    return 0


def detect_calendar_mismatch_symbols(
    news_index: Dict[str, List[Dict[str, Any]]],
    earnings_index: Dict[str, List[Dict[str, Any]]],
    now: Optional[datetime] = None,
    min_hits: int = 2,
    recent_hours: int = 96,
    lookahead_days: int = 7,
) -> Dict[str, Dict[str, Any]]:
    now_dt = now or _utc_now()
    global_start = now_dt - timedelta(days=1)
    global_end = now_dt + timedelta(days=max(1, int(lookahead_days)))
    mismatches: Dict[str, Dict[str, Any]] = {}

    for symbol, rows in (news_index or {}).items():
        symbol_u = str(symbol or "").upper().strip()
        if not symbol_u:
            continue
        if not _is_earnings_eligible_symbol(symbol_u):
            continue

        hits = []
        hit_times = []
        headline_keys = set()
        for row in rows or []:
            short_text = _headline_short_blob(row)
            if not EARNINGS_MENTION_RE.search(short_text):
                continue
            if not _has_strict_symbol_mention(short_text, symbol_u):
                continue
            if not _is_future_earnings_context(short_text):
                continue
            pub_dt = _parse_iso(row.get("published_at") or row.get("published_utc"))
            if pub_dt is None:
                continue
            title_key = str(row.get("title") or short_text).strip().lower()
            if title_key in headline_keys:
                continue
            headline_keys.add(title_key)
            hits.append(row)
            hit_times.append(pub_dt)

        if len(hits) < int(min_hits) or len(hit_times) < int(min_hits):
            continue

        latest_hit_dt = max(hit_times)
        if latest_hit_dt < (now_dt - timedelta(hours=max(12, int(recent_hours)))):
            continue

        events = list((earnings_index or {}).get(symbol_u, []) or [])
        has_near_latest_event = False
        has_visible_window_event = False
        latest_window_start = latest_hit_dt - timedelta(days=2)
        latest_window_end = latest_hit_dt + timedelta(days=2)
        for event in events:
            event_dt = _parse_iso(event.get("date") or event.get("date_formatted"))
            if not event_dt:
                continue
            if latest_window_start <= event_dt <= latest_window_end:
                has_near_latest_event = True
                break
            if global_start <= event_dt <= global_end:
                has_visible_window_event = True

        if has_near_latest_event or has_visible_window_event:
            continue

        latest_hit = max(
            hits,
            key=lambda r: _parse_iso(r.get("published_at") or r.get("published_utc"))
            or datetime.min.replace(tzinfo=timezone.utc),
        )
        sorted_hits = sorted(
            hits,
            key=lambda r: _parse_iso(r.get("published_at") or r.get("published_utc"))
            or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        recent_headlines: List[str] = []
        seen_headline = set()
        for row in sorted_hits:
            headline = str(row.get("title") or row.get("summary") or "").strip()
            if not headline:
                continue
            key = headline.lower()
            if key in seen_headline:
                continue
            seen_headline.add(key)
            recent_headlines.append(headline)
            if len(recent_headlines) >= 3:
                break
        mismatches[symbol_u] = {
            "symbol": symbol_u,
            "hits": len(hits),
            "recent_hits": len([dt for dt in hit_times if dt >= (now_dt - timedelta(hours=max(12, int(recent_hours))))]),
            "recent_headlines": recent_headlines,
            "lookahead_days": int(max(1, lookahead_days)),
            "lookback_hours": int(max(12, recent_hours)),
            "latest_news_utc": (
                latest_hit.get("published_at")
                or latest_hit.get("published_utc")
                or None
            ),
            "reason": "POSSIBLE_MISSING_EARNINGS_EVENT",
        }
    return mismatches


def detect_already_reported_symbols(
    news_index: Dict[str, List[Dict[str, Any]]],
    earnings_index: Dict[str, List[Dict[str, Any]]],
    now: Optional[datetime] = None,
    min_hits: int = 2,
    recent_hours: int = 72,
    event_window_days: int = 7,
) -> Dict[str, Dict[str, Any]]:
    """Detect tickers whose earnings has ALREADY been reported using the news, even
    when the calendar `earnings_time_utc` is a wrong future date.

    The date-based `_filter_stale_items` only drops items with `earnings_time_utc < now`,
    so a stale/wrong future calendar date survives. The mismatch detector
    (`detect_calendar_mismatch_symbols`) is intentionally blind to post-earnings
    coverage (`_is_future_earnings_context` returns False for "beat"/"reported"),
    so it never flags these. This detector closes that gap: it reads the news (the
    reason news are aggregated) and flags a ticker when there are ≥ `min_hits`
    distinct recent headlines that are clearly POST-earnings (EARNINGS_PAST_CUE_RE
    match, NOT a future cue, strict symbol mention, earnings-related, published
    within `recent_hours`) AND the calendar still shows a near-term event for it
    (within `event_window_days` of now — i.e. the "this earnings" the calendar is
    tracking is the one the news says already happened). A far-future next-quarter
    event is NOT flagged.

    Returns `{symbol: {"symbol", "reason": "ALREADY_REPORTED", "hits", "headlines",
    "latest_pub_utc"}}`.
    """
    now_dt = now or _utc_now()
    recent_cutoff = now_dt - timedelta(hours=max(1, int(recent_hours)))
    # The calendar event the tab is tracking ("this earnings"). Allow a 1-day past
    # grace so a BMO that printed this morning (event date == today) still counts;
    # a 60-day-out next-quarter print does not.
    event_start = now_dt - timedelta(days=1)
    event_end = now_dt + timedelta(days=max(1, int(event_window_days)))
    flagged: Dict[str, Dict[str, Any]] = {}

    for symbol, rows in (news_index or {}).items():
        symbol_u = str(symbol or "").upper().strip()
        if not symbol_u:
            continue
        if not _is_earnings_eligible_symbol(symbol_u):
            continue

        hits: List[Dict[str, Any]] = []
        decisive_hits: List[Dict[str, Any]] = []
        headline_keys = set()
        latest_pub: Optional[datetime] = None
        for row in rows or []:
            short_text = _headline_short_blob(row)
            # Expiring an upcoming event is destructive: summaries commonly
            # mention the previous quarter while the headline previews the next
            # one. Require post-results evidence in the headline/title itself.
            title_text = str(row.get("title") or "").strip()
            result_release = bool(EARNINGS_RESULT_RELEASE_RE.search(title_text))
            if not (EARNINGS_MENTION_RE.search(short_text) or result_release):
                continue
            direct_symbols = set(_extract_news_row_symbols(row))
            strict_mention = _has_strict_symbol_mention(short_text, symbol_u)
            # Provider ticker metadata is useful, but a single bad RSS mapping
            # must never be allowed to expire an earnings event. Direct-only
            # matches therefore need corroboration; a strict ticker mention can
            # stand on one decisive result-release headline.
            if not strict_mention and symbol_u not in direct_symbols:
                continue
            if not strict_mention and float(_safe_float(row.get("_pe_relevance"), 0.0) or 0.0) < 0.65:
                continue
            # Forward-looking preview → the earnings has NOT happened yet.
            if EARNINGS_FUTURE_CUE_RE.search(short_text):
                continue
            # Require clearly-post-earnings language.
            if not (EARNINGS_PAST_CUE_RE.search(title_text) or result_release):
                continue
            pub_dt = _parse_iso(row.get("published_at") or row.get("published_utc"))
            if pub_dt is None or pub_dt < recent_cutoff:
                continue
            title_key = str(row.get("title") or short_text).strip().lower()
            if title_key in headline_keys:
                continue
            headline_keys.add(title_key)
            hits.append(row)
            if strict_mention and result_release:
                decisive_hits.append(row)
            if latest_pub is None or pub_dt > latest_pub:
                latest_pub = pub_dt

        if len(hits) < int(min_hits) and not decisive_hits:
            continue

        events = list((earnings_index or {}).get(symbol_u, []) or [])
        has_near_term_event = False
        for event in events:
            event_dt = _parse_iso(event.get("date") or event.get("date_formatted"))
            if event_dt is not None and event_start <= event_dt <= event_end:
                has_near_term_event = True
                break
        if not has_near_term_event:
            continue

        headlines: List[str] = []
        for r in hits[:5]:
            h = str(r.get("title") or _headline_short_blob(r)).strip()
            if h:
                headlines.append(h)
        flagged[symbol_u] = {
            "symbol": symbol_u,
            "reason": "ALREADY_REPORTED",
            "hits": len(hits),
            "decisive_hits": len(decisive_hits),
            "headlines": headlines,
            "latest_pub_utc": _iso(latest_pub),
        }
    return flagged


def _latest_pre_earnings_snapshot_time(payload: Dict[str, Any]) -> Optional[datetime]:
    if not isinstance(payload, dict):
        return None
    status = payload.get("status") if isinstance(payload.get("status"), dict) else {}
    as_of = _parse_iso(payload.get("as_of_utc"))
    # Freshness of the shortlist is the age of the last WHOLE-universe build.
    # `last_success_time` is also bumped by single-symbol rescores and coverage
    # warmups (so UI polling can finish); counting those let one refreshed row
    # declare a 400-row snapshot FRESH while the full cycle was failing or
    # hours overdue (tab audit 2026-11-15).
    full_cycle = _parse_iso(status.get("last_full_cycle_time"))
    if full_cycle is not None:
        candidates = [full_cycle, as_of]
    else:
        # Legacy snapshot (written before last_full_cycle_time existed): keep
        # the completion time unless it is provably a partial-update stamp.
        last_success_raw = status.get("last_success_time")
        warm = status.get("coverage_warmup") if isinstance(status.get("coverage_warmup"), dict) else {}
        partial_stamps = {
            str(value)
            for value in (
                status.get("last_targeted_rescore_time"),
                warm.get("last_finished"),
            )
            if value
        }
        last_success = (
            None
            if last_success_raw and str(last_success_raw) in partial_stamps
            else _parse_iso(last_success_raw)
        )
        candidates = [last_success, as_of]
    valid = [value for value in candidates if value is not None]
    return max(valid) if valid else None


def _full_cycle_schedule_anchor(payload: Dict[str, Any]) -> Optional[datetime]:
    """Scheduling anchor for the next FULL Pre-Earnings cycle.

    Must track only whole-payload builds (`as_of_utc`): targeted rescorses,
    coverage warmups and other partial updates legitimately bump
    `status.last_success_time` so UI refresh polling can finish, and feeding
    those partial timestamps back into the scheduler starved the full cycle
    indefinitely while the Pre-Earnings tab stayed open (live: no run_cycle
    for hours while refresh-symbol kept resetting the clock). Legacy states
    without `as_of_utc` fall back to `last_success_time`.
    """
    if not isinstance(payload, dict):
        return None
    as_of = _parse_iso(payload.get("as_of_utc"))
    if as_of is not None:
        return as_of
    status = payload.get("status") if isinstance(payload.get("status"), dict) else {}
    return _parse_iso(status.get("last_success_time"))


def guard_pre_earnings_recommendation_freshness(
    payload: Dict[str, Any],
    *,
    now: Optional[datetime] = None,
    max_age_seconds: Optional[int] = None,
) -> Dict[str, Any]:
    """Prevent a cache-only read from presenting an old shortlist as current."""
    if not isinstance(payload, dict):
        return payload
    current = now or _utc_now()
    as_of = _latest_pre_earnings_snapshot_time(payload)
    ranking = dict(payload.get("ranking") or {})
    configured_max = max_age_seconds
    if configured_max is None:
        configured_max = _safe_int(ranking.get("recommendation_max_age_seconds"), 0)
    if not configured_max:
        configured_max = max(1800, 3 * max(30, _safe_int(payload.get("refresh_seconds"), 600)))
    configured_max = max(600, min(6 * 3600, int(configured_max)))
    age_seconds = max(0, int((current - as_of).total_seconds())) if as_of else None
    stale = age_seconds is None or age_seconds > configured_max
    ranking["snapshot_age_seconds"] = age_seconds
    ranking["recommendation_max_age_seconds"] = configured_max
    ranking["state"] = "STALE" if stale else "FRESH"
    payload["ranking"] = ranking
    flags = dict(payload.get("flags") or {})
    flags["recommendations_stale"] = bool(stale)
    if stale:
        payload["top_predictions"] = []
        flags["top_predictions_available"] = False
    payload["flags"] = flags
    return payload


class PreEarningsAgent:
    _MIN_REPRESENTATIVE_BEAT_SAMPLES = 4
    _ENRICHMENT_RETRY_SECONDS = {
        "beat_history": 6 * 3600,
        "historical_move_history": 12 * 3600,
        "analyst_consensus": 6 * 3600,
        "range_proxy": 15 * 60,
        "news_context": 30 * 60,
        "event_window": 60 * 60,
        "liquidity_context": 6 * 3600,
    }
    _VERIFIED_ENRICHMENT_TTL_SECONDS = {
        "beat_history": 12 * 3600,
        "historical_move_history": 12 * 3600,
        "recent_reactions": 12 * 3600,
        "analyst_consensus": 6 * 3600,
        "range_proxy": 15 * 60,
        "liquidity_context": 6 * 3600,
        "earnings_trend": 24 * 3600,
        "news_context": 30 * 60,
    }

    DEFAULT_CONFIG: Dict[str, Any] = {
        "refresh_seconds": 600,
        "startup_delay_seconds": 20,
        "external_call_budget": 30,
        "lookahead_days": 30,
        "lookback_hours": 336,
        # The 30-day US earnings calendar regularly contains 300+ reporters.
        # Keep enough rows to score the complete window before Top 10 ranking;
        # provider calls remain independently bounded by external_call_budget.
        "max_results": 400,
        "cache_ttl_seconds": 600,
        "mismatch_past_grace_days": 3,
        "schedule_conflict_min_spacing_days": 45,
        "schedule_conflict_max_news_rows": 0,
        "use_ollama": True,
        "ollama_max_tickers": 25,
        "ollama_timeout_seconds": 12,
        # Weight of the LLM verdict in the final direction_bias_score (0..1).
        # The AI is a first-class participant in the long/short call, but the
        # data-driven structured score stays primary — a contributor, not an
        # override. The previous hard-coded 0.6 let headlines alone flip a
        # data-driven call; 0.35 keeps AI meaningful without dominating.
        "ollama_decision_weight": 0.35,
        # News intelligence for the daily shortlist. The global News tab is the
        # source of truth, while Pre-Earnings performs a bounded deep refresh of
        # the most relevant names and rotates through the rest of the slate.
        "news_evidence_limit": 16,
        "news_prompt_char_budget": 12000,
        "news_deep_refresh_batch_size": 16,
        "news_priority_refresh_size": 10,
        "news_deep_refresh_seconds": 900,
        "news_change_reanalysis": True,
        # Trader-facing shortlist. This score is explicitly a conviction/ranking
        # score, never presented as a win probability.
        "top_predictions_limit": 10,
        "top_prediction_min_trader_score": 60,
        # Free point-in-time evidence captured before each event (2026-09-15).
        "estimate_revision_capture_enabled": True,
        "pre_event_feature_lookahead_days": 7,
        "pre_event_feature_max_fetch_per_cycle": 40,
        "top_prediction_min_coverage": 45,
        "top_prediction_min_abs_bias": 20,
        "top_prediction_require_verified_event": True,
        "top_prediction_require_ai": True,
        "top_prediction_min_news_articles": 3,
        "top_prediction_min_news_sources": 1,
        "top_prediction_max_news_age_hours": 96,
        "recommendation_max_age_seconds": 1800,
        "feedback_lookback_days": 365,
        "feedback_min_samples": 24,
        "recalibration_min_samples": 40,
        "high_gap_threshold_pct": 5.0,
        "reported_session_filter": True,
        "pulse_enabled": True,
        "pulse_ttl_seconds": 300,
        "pulse_timeout_seconds": 45,
        # The active card is cumulative for the same upcoming event. History
        # and materially unique archived/history records are intentionally
        # unbounded on disk: paid Pulse generations must survive refresh/restart.
        "pulse_active_max_per_side": 500,
        "pulse_history_response_limit": 20,
        "pulse_undated_retention_days": 45,
        # Peer read-through (owner requirement, 2026-11): settled peer outcomes
        # within this window feed the AI prompt and the verdict-staleness rule.
        "peer_readthrough_hours": 96,
        "peer_readthrough_max_peers": 5,
        # Retail positioning (owner request, 2026-09-05): eToro buy/sell skew as
        # direction component c6. Small weight by design — the crowd is often
        # contrarian into earnings; set retail_positioning_sign to -1.0 to flip
        # it once own settled outcomes justify, or weight to 0 to disable.
        "retail_positioning_weight": 0.10,
        "retail_positioning_sign": 1.0,
        "retail_support_rank_ceiling": 20000,
        # Operator additions to PEER_THEME_OVERRIDES: {symbol -> theme key}.
        "peer_theme_overrides": {},
        # History sector resolution (owner requirement, 2026-08-04): proactive
        # per-cycle backfill of UNKNOWN sectors over the recent history window
        # (estimates source only, small batch); the AI news classification rung
        # is reserved for the explicit UI refresh button.
        "sector_backfill_per_cycle": 8,
        "sector_ai_enabled": True,
        "sector_ai_timeout_sec": 20,
        "sector_ai_model": "",
    }

    ENV_MAP = {
        "PRE_EARNINGS_REFRESH_SECONDS": ("refresh_seconds", int),
        "PRE_EARNINGS_STARTUP_DELAY_SECONDS": ("startup_delay_seconds", int),
        "PRE_EARNINGS_EXTERNAL_CALL_BUDGET": ("external_call_budget", int),
        "PRE_EARNINGS_LOOKAHEAD_DAYS": ("lookahead_days", int),
        "PRE_EARNINGS_LOOKBACK_HOURS": ("lookback_hours", int),
        "PRE_EARNINGS_MAX_RESULTS": ("max_results", int),
        "PRE_EARNINGS_CACHE_TTL_SECONDS": ("cache_ttl_seconds", int),
        "PRE_EARNINGS_MISMATCH_PAST_GRACE_DAYS": ("mismatch_past_grace_days", int),
        "PRE_EARNINGS_SCHEDULE_CONFLICT_MIN_SPACING_DAYS": ("schedule_conflict_min_spacing_days", int),
        "PRE_EARNINGS_SCHEDULE_CONFLICT_MAX_NEWS_ROWS": ("schedule_conflict_max_news_rows", int),
        "PRE_EARNINGS_USE_OLLAMA": ("use_ollama", lambda v: str(v).strip().lower() in {"1", "true", "yes", "on"}),
        "PRE_EARNINGS_OLLAMA_MAX_TICKERS": ("ollama_max_tickers", int),
        "PRE_EARNINGS_OLLAMA_TIMEOUT_SECONDS": ("ollama_timeout_seconds", int),
        "PRE_EARNINGS_OLLAMA_DECISION_WEIGHT": ("ollama_decision_weight", float),
        "PRE_EARNINGS_NEWS_EVIDENCE_LIMIT": ("news_evidence_limit", int),
        "PRE_EARNINGS_NEWS_PROMPT_CHAR_BUDGET": ("news_prompt_char_budget", int),
        "PRE_EARNINGS_NEWS_DEEP_REFRESH_BATCH_SIZE": ("news_deep_refresh_batch_size", int),
        "PRE_EARNINGS_NEWS_PRIORITY_REFRESH_SIZE": ("news_priority_refresh_size", int),
        "PRE_EARNINGS_NEWS_DEEP_REFRESH_SECONDS": ("news_deep_refresh_seconds", int),
        "PRE_EARNINGS_NEWS_CHANGE_REANALYSIS": ("news_change_reanalysis", lambda v: str(v).strip().lower() in {"1", "true", "yes", "on"}),
        "PRE_EARNINGS_TOP_PREDICTIONS_LIMIT": ("top_predictions_limit", int),
        "PRE_EARNINGS_TOP_PREDICTION_MIN_TRADER_SCORE": ("top_prediction_min_trader_score", int),
        "PRE_EARNINGS_TOP_PREDICTION_MIN_COVERAGE": ("top_prediction_min_coverage", int),
        "PRE_EARNINGS_TOP_PREDICTION_MIN_ABS_BIAS": ("top_prediction_min_abs_bias", int),
        "PRE_EARNINGS_TOP_PREDICTION_REQUIRE_VERIFIED_EVENT": ("top_prediction_require_verified_event", lambda v: str(v).strip().lower() in {"1", "true", "yes", "on"}),
        "PRE_EARNINGS_TOP_PREDICTION_REQUIRE_AI": ("top_prediction_require_ai", lambda v: str(v).strip().lower() in {"1", "true", "yes", "on"}),
        "PRE_EARNINGS_TOP_PREDICTION_MIN_NEWS_ARTICLES": ("top_prediction_min_news_articles", int),
        "PRE_EARNINGS_TOP_PREDICTION_MIN_NEWS_SOURCES": ("top_prediction_min_news_sources", int),
        "PRE_EARNINGS_TOP_PREDICTION_MAX_NEWS_AGE_HOURS": ("top_prediction_max_news_age_hours", int),
        "PRE_EARNINGS_RECOMMENDATION_MAX_AGE_SECONDS": ("recommendation_max_age_seconds", int),
        "PRE_EARNINGS_FEEDBACK_LOOKBACK_DAYS": ("feedback_lookback_days", int),
        "PRE_EARNINGS_FEEDBACK_MIN_SAMPLES": ("feedback_min_samples", int),
        "PRE_EARNINGS_RECALIBRATION_MIN_SAMPLES": ("recalibration_min_samples", int),
        "PRE_EARNINGS_HIGH_GAP_THRESHOLD_PCT": ("high_gap_threshold_pct", float),
        "PRE_EARNINGS_REPORTED_SESSION_FILTER": ("reported_session_filter", lambda v: str(v).strip().lower() in {"1", "true", "yes", "on"}),
        "PRE_EARNINGS_PULSE_ENABLED": ("pulse_enabled", lambda v: str(v).strip().lower() in {"1", "true", "yes", "on"}),
        "PRE_EARNINGS_PULSE_TTL_SECONDS": ("pulse_ttl_seconds", int),
        "PRE_EARNINGS_PULSE_TIMEOUT_SECONDS": ("pulse_timeout_seconds", int),
        "PRE_EARNINGS_PULSE_ACTIVE_MAX_PER_SIDE": ("pulse_active_max_per_side", int),
        "PRE_EARNINGS_PULSE_HISTORY_RESPONSE_LIMIT": ("pulse_history_response_limit", int),
        "PRE_EARNINGS_PULSE_UNDATED_RETENTION_DAYS": ("pulse_undated_retention_days", int),
        "PRE_EARNINGS_PEER_READTHROUGH_HOURS": ("peer_readthrough_hours", int),
        "PRE_EARNINGS_PEER_READTHROUGH_MAX_PEERS": ("peer_readthrough_max_peers", int),
        "PRE_EARNINGS_RETAIL_POSITIONING_WEIGHT": ("retail_positioning_weight", float),
        "PRE_EARNINGS_RETAIL_POSITIONING_SIGN": ("retail_positioning_sign", float),
        "PRE_EARNINGS_RETAIL_SUPPORT_RANK_CEILING": ("retail_support_rank_ceiling", int),
        "PRE_EARNINGS_SECTOR_BACKFILL_PER_CYCLE": ("sector_backfill_per_cycle", int),
        "PRE_EARNINGS_SECTOR_AI_ENABLED": ("sector_ai_enabled", lambda v: str(v).strip().lower() in {"1", "true", "yes", "on"}),
        "PRE_EARNINGS_SECTOR_AI_TIMEOUT_SEC": ("sector_ai_timeout_sec", int),
        "PRE_EARNINGS_SECTOR_AI_MODEL": ("sector_ai_model", str),
    }

    @staticmethod
    def _default_coverage_warmup_status() -> Dict[str, Any]:
        return {
            "running": False,
            "job_id": None,
            "batch_size": 5,
            "symbols": [],
            "total": 0,
            "processed": 0,
            "success": 0,
            "failed": 0,
            "rescored": 0,
            "current_batch": 0,
            "active_symbol": None,
            "last_symbol": None,
            "last_error": None,
            "last_started": None,
            "last_finished": None,
            "last_success": None,
            "last_progress_at": None,
        }

    def __init__(self, config_manager: Optional[ConfigManager] = None):
        self.config_manager = config_manager or ConfigManager()
        self._lock = threading.RLock()
        self._feedback_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._pending_refresh = False
        self._feedback = self._load_feedback_store()
        self._ai_cache = self._load_ai_cache()
        self._state = self._load_state()
        self._state_revision = max(
            int(time.time() * 1000),
            _safe_int(self._state.get("state_revision"), 0),
        )
        self._state["state_revision"] = self._state_revision
        self._pulse_store_path = Path(PRE_EARNINGS_PULSE_HISTORY_PATH)
        self._pulse_store_lock = threading.RLock()
        self._pulse_store = self._load_pulse_store()
        self._migrate_legacy_pulse_if_needed()
        self._restore_persisted_pulse()
        self._state_read_fallback = copy.deepcopy(self._state)
        self._quote_ttl_cache: Dict[str, Any] = {}
        self._growth_ttl_cache: Dict[str, Any] = {}
        self._state_rows_snapshot: Dict[str, Any] = {"mtime": 0.0, "data": {}}
        self._coverage_thread: Optional[threading.Thread] = None
        self._coverage_pending_symbols: List[str] = []
        self._coverage_job_seq = 0
        self._concurrent_item_revision = 0
        self._concurrent_item_updates: Dict[str, Dict[str, Any]] = {}
        self._low_coverage_news_lock = threading.RLock()
        self._low_coverage_news_pending: List[str] = []
        self._low_coverage_news_thread: Optional[threading.Thread] = None
        self._low_coverage_news_idle = threading.Event()
        self._low_coverage_news_idle.set()
        self._did_initial_remote_earnings_refresh = False
        self._news_refresh_cursor = max(
            0,
            _safe_int((self._state.get("status") or {}).get("news_refresh_cursor"), 0),
        )
        self._deep_news_refresh_at: Dict[str, float] = {}

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True, name="PreEarningsAgent")
            self._thread.start()
            logger.info("PreEarningsAgent started")

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=3.0)
        logger.info("PreEarningsAgent stopped")

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive())

    def get_runtime_status(self) -> Dict[str, Any]:
        with self._lock:
            status = dict(self._state.get("status", {}) or {})
            running = bool(self._thread and self._thread.is_alive())
        return {
            "running": running,
            "last_run": status.get("last_run_time"),
            "last_success": status.get("last_success_time"),
            "last_error": status.get("last_error"),
            "next_run_in": status.get("next_run_in"),
        }

    def get_refresh_status(self) -> Dict[str, Any]:
        """Return the lightweight refresh state without copying the item universe."""
        with self._lock:
            status = copy.deepcopy(self._state.get("status", {}) or {})
            revision = _safe_int(self._state.get("state_revision"), 0)
        return {
            "state_revision": revision,
            "status": status,
            "disabled": not self._is_enabled(),
        }

    def get_symbol_analysis_snapshot(self, symbol: str) -> Dict[str, Any]:
        """Return freshness/provenance for one persisted analysis row.

        The continuous scanner uses this after ``last_targeted_rescore_time``
        advances. Keeping this read under the state lock guarantees that the AI
        stage observes the same atomic row published by the targeted rescore.
        """
        sym = str(symbol or "").upper().strip()
        if not sym:
            return {"status": "invalid_symbol", "symbol": sym}
        with self._lock:
            item = next(
                (
                    copy.deepcopy(row)
                    for row in (self._state.get("items", []) or [])
                    if isinstance(row, dict)
                    and str(row.get("symbol") or "").upper().strip() == sym
                ),
                None,
            )
            status = dict(self._state.get("status", {}) or {})
        if not isinstance(item, dict):
            return {"status": "not_found", "symbol": sym}

        verified = item.get("enrichment_verified_at_utc")
        verified = copy.deepcopy(verified) if isinstance(verified, dict) else {}
        completed_symbol = str(
            status.get("last_targeted_rescore_symbol") or ""
        ).upper().strip()
        targeted_at = (
            status.get("last_targeted_rescore_time")
            if completed_symbol == sym
            else None
        )
        ai = item.get("ai_decision") if isinstance(item.get("ai_decision"), dict) else {}
        digest = item.get("news_digest") if isinstance(item.get("news_digest"), dict) else {}
        return {
            "status": "ok",
            "symbol": sym,
            "earnings_time_utc": item.get("earnings_time_utc"),
            "updated_utc": item.get("updated_utc"),
            "data_verified_at": targeted_at or item.get("updated_utc"),
            "enrichment_verified_at_utc": verified,
            "enrichment_value_sources": copy.deepcopy(
                item.get("enrichment_value_sources") or {}
            ),
            "coverage_score": item.get("coverage_score"),
            "news_articles": _safe_int(digest.get("article_count"), 0),
            "news_sources": _safe_int(digest.get("unique_sources"), 0),
            "news_newest_utc": digest.get("newest_utc"),
            "ai_generated_at": ai.get("generated_at") if ai.get("used") else None,
        }

    def request_refresh(self) -> Dict[str, Any]:
        if not self._is_enabled():
            return {"status": "disabled"}
        with self._lock:
            self._pending_refresh = True
        self._wake_event.set()
        return {"status": "accepted"}

    def _seconds_until_scheduled_refresh(
        self,
        cfg: Dict[str, Any],
        *,
        now: Optional[datetime] = None,
    ) -> float:
        """Return the remaining delay before the next automatic full cycle.

        A process restart must not turn a fresh persisted snapshot into a
        provider/AI refresh. Explicit refresh requests always bypass this
        delay, while an empty, invalid, or stale snapshot is due immediately.
        """
        interval = float(max(30, _safe_int(cfg.get("refresh_seconds"), 600)))
        with self._lock:
            if self._pending_refresh:
                return 0.0
            items = self._state.get("items")

        if not isinstance(items, list) or not items:
            return 0.0
        last_success = _full_cycle_schedule_anchor(self._state)
        if last_success is None:
            return 0.0

        current = now or _utc_now()
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        elapsed = max(
            0.0,
            (current.astimezone(timezone.utc) - last_success.astimezone(timezone.utc)).total_seconds(),
        )
        return max(0.0, interval - elapsed)

    @staticmethod
    def _external_budget_exhausted() -> bool:
        ctx = get_request_context()
        if ctx is None:
            return False
        metadata = ctx.metadata if isinstance(ctx.metadata, dict) else {}
        if bool(metadata.get("external_budget_exhausted")):
            return True
        budget = getattr(ctx, "external_budget", None)
        if budget is None:
            return False
        try:
            remaining = budget.snapshot().get("remaining")
            return remaining is not None and int(remaining) <= 0
        except Exception:
            return False

    @staticmethod
    def _reserve_external_call(provider: str) -> bool:
        """Reserve one slot for a direct provider call.

        ``http_utils`` enforces the run budget for requests routed through its
        wrappers, but yfinance performs native requests and otherwise bypasses
        that cap.  This helper gives those native calls the same hard limit and
        shares the one-warning-per-context marker used by ``http_utils``.
        """
        ctx = get_request_context()
        budget = getattr(ctx, "external_budget", None) if ctx is not None else None
        if budget is None:
            return True
        provider_name = str(provider or "yfinance")
        try:
            if budget.reserve(provider_name):
                return True
        except Exception:
            return False

        agent_name = str(getattr(ctx, "agent_name", None) or "pre_earnings")
        metadata = ctx.metadata if isinstance(ctx.metadata, dict) else {}
        metadata["external_budget_exhausted"] = True
        if not metadata.get("external_budget_warning_logged"):
            metadata["external_budget_warning_logged"] = True
            logger.warning(
                "External-call budget exhausted for agent=%s",
                agent_name,
            )
        return False

    def refresh_symbol_calendar(
        self,
        symbol: str,
        *,
        request_rescore: bool = True,
    ) -> Dict[str, Any]:
        sym = str(symbol or "").upper().strip()
        if not sym or not _is_earnings_eligible_symbol(sym):
            return {"status": "invalid_symbol", "symbol": sym}
        cfg = self._get_config()
        rows = self._refresh_earnings_for_symbols(cfg, [sym], force_yfinance=True)
        matched = [
            row for row in (rows or [])
            if str((row or {}).get("symbol") or "").upper().strip() == sym
        ]
        if request_rescore and self._is_enabled():
            try:
                self.request_refresh()
            except Exception:
                pass
        return {
            "status": "ok",
            "symbol": sym,
            "events_found": len(matched),
            "refreshed_rows": matched[:10],
        }

    def refresh_symbol_analysis(self, symbol: str) -> Dict[str, Any]:
        """Refresh one ticker's calendar and enqueue its missing analysis inputs.

        A symbol refresh must not compete with the entire 200-name scan for the
        first 30 external-call slots. Calendar resolution is done inline, while
        news/EPS history/price context warm asynchronously and trigger a rescore
        after the caches are ready.
        """
        sym = str(symbol or "").upper().strip()
        if not sym or not _is_earnings_eligible_symbol(sym):
            return {"status": "invalid_symbol", "symbol": sym}
        calendar_result = self.refresh_symbol_calendar(sym, request_rescore=False)
        coverage_result = self.request_coverage_warmup(
            symbols=[sym],
            batch_size=1,
            only_needs_coverage=False,
            allow_remote_on_miss=True,
        )
        # If no enrichment worker could be launched, still rescore the
        # canonical calendar result. Accepted workers request the rescore only
        # after their news/EPS/price caches are populated.
        if str(coverage_result.get("status") or "").lower() not in {
            "accepted",
            "already_running",
            "queued",
        } and self._is_enabled():
            self.request_refresh()
        return {
            "status": "ok",
            "symbol": sym,
            "calendar": calendar_result,
            "coverage": coverage_result,
        }

    def request_coverage_warmup(
        self,
        symbols: Optional[List[str]] = None,
        batch_size: int = 5,
        only_needs_coverage: bool = True,
        allow_remote_on_miss: bool = True,
    ) -> Dict[str, Any]:
        bs = max(1, min(5, int(batch_size or 5)))
        targets = self._resolve_coverage_targets(symbols=symbols, only_needs_coverage=only_needs_coverage)
        if not targets:
            return {
                "status": "no_targets",
                "coverage_warmup": self.get_coverage_warmup_status(),
            }
        candidate_total = len(targets)
        next_rotation_cursor: Optional[int] = None
        explicit_symbols = bool(
            isinstance(symbols, list)
            and any(str(symbol or "").strip() for symbol in symbols)
        )
        if not explicit_symbols and len(targets) > 5:
            status_snapshot = dict(self._state.get("status", {}) or {})
            cursor = max(0, _safe_int(status_snapshot.get("coverage_rotation_cursor"), 0))
            cursor %= len(targets)
            rotated = targets[cursor:] + targets[:cursor]
            # Watchlist names that are due for enrichment are never rotated out
            # of the batch (their retry cooldown keeps them from starving it).
            watchlist = self._watchlist_symbol_set()
            pinned = [symbol for symbol in targets if symbol in watchlist][:5]
            targets = list(dict.fromkeys(pinned + rotated))[:5]
            next_rotation_cursor = (cursor + len(targets)) % candidate_total

        acquired = self._lock.acquire(timeout=0.35)
        if not acquired:
            warm = self.get_coverage_warmup_status()
            return {
                "status": "already_running" if bool(warm.get("running")) else "busy",
                "coverage_warmup": warm,
            }
        worker: Optional[threading.Thread] = None
        worker_start_error: Optional[str] = None
        job_id: Optional[str] = None
        try:
            status = dict(self._state.get("status", {}) or {})
            if next_rotation_cursor is not None:
                status["coverage_rotation_cursor"] = int(next_rotation_cursor)
            current = dict(status.get("coverage_warmup", {}) or {})
            if bool(current.get("running")) and self._coverage_thread and self._coverage_thread.is_alive():
                stale_seconds = 120.0
                now_dt = _utc_now()
                progress_dt = _parse_iso(current.get("last_progress_at")) or _parse_iso(current.get("last_started"))
                is_stale = bool(progress_dt and (now_dt - progress_dt).total_seconds() >= stale_seconds)
                if not is_stale:
                    if explicit_symbols:
                        active_symbols = {
                            str(symbol or "").upper().strip()
                            for symbol in (current.get("symbols") or [])
                            if str(symbol or "").strip()
                        }
                        queue_targets = [
                            symbol
                            for symbol in targets
                            if symbol not in active_symbols
                        ]
                        if not queue_targets:
                            return {
                                "status": "already_running",
                                "coverage_warmup": copy.deepcopy(current),
                            }
                        pending = list(getattr(self, "_coverage_pending_symbols", []) or [])
                        pending = list(dict.fromkeys(pending + queue_targets))
                        self._coverage_pending_symbols = pending
                        current["queued_symbols"] = list(pending)
                        status["coverage_warmup"] = current
                        self._state["status"] = status
                        return {
                            "status": "queued",
                            "queued_symbols": list(pending),
                            "coverage_warmup": copy.deepcopy(current),
                        }
                    return {
                        "status": "already_running",
                        "coverage_warmup": copy.deepcopy(current),
                    }
                # stale job: replace logical job id and start a fresh worker
                current["running"] = False
                current["last_error"] = "stale_job_replaced"
                current["last_finished"] = _iso(now_dt)
                status["coverage_warmup"] = current
                self._state["status"] = status

            self._coverage_job_seq += 1
            job_id = f"cov-{int(time.time())}-{self._coverage_job_seq}"
            warm = self._default_coverage_warmup_status()
            warm.update(
                {
                    "running": True,
                    "job_id": job_id,
                    "batch_size": bs,
                    "symbols": list(targets),
                    "total": len(targets),
                    "candidate_total": int(candidate_total),
                    "processed": 0,
                    "success": 0,
                    "failed": 0,
                    "current_batch": 0,
                    "active_symbol": None,
                    "last_symbol": None,
                    "last_error": None,
                    "last_started": _iso(_utc_now()),
                    "last_finished": None,
                    "last_progress_at": _iso(_utc_now()),
                }
            )
            status["coverage_warmup"] = warm
            self._state["status"] = status

            # Reserve the worker pointer and start it while the state lock is
            # still held. The worker blocks on this same lock before touching
            # state, while a concurrent request can no longer observe
            # ``running=True`` with ``_coverage_thread`` still unset and launch
            # a second job.
            worker = threading.Thread(
                target=self._run_coverage_warmup_worker,
                args=(job_id, targets, bs, bool(allow_remote_on_miss)),
                daemon=True,
                name=f"PreEarnCoverage-{job_id}",
            )
            self._coverage_thread = worker
            try:
                worker.start()
            except Exception as exc:
                worker_start_error = str(exc)
                self._coverage_thread = None
                warm["running"] = False
                warm["last_error"] = f"worker_start_failed: {worker_start_error}"
                warm["last_finished"] = _iso(_utc_now())
                warm["last_progress_at"] = warm["last_finished"]
                status["coverage_warmup"] = warm
                self._state["status"] = status
        finally:
            self._lock.release()

        if worker_start_error:
            return {
                "status": "worker_start_failed",
                "job_id": job_id,
                "error": worker_start_error,
                "coverage_warmup": self.get_coverage_warmup_status(),
            }
        return {
            "status": "accepted",
            "job_id": job_id,
            "total": len(targets),
            "candidate_total": int(candidate_total),
            "batch_size": bs,
            "coverage_warmup": self.get_coverage_warmup_status(),
        }

    def get_coverage_warmup_status(self) -> Dict[str, Any]:
        acquired = self._lock.acquire(timeout=0.2)
        if acquired:
            try:
                status = dict(self._state.get("status", {}) or {})
                warm = dict(status.get("coverage_warmup", {}) or {})
            finally:
                self._lock.release()
        else:
            fallback_state = self._state_read_fallback if isinstance(self._state_read_fallback, dict) else {}
            status = dict(fallback_state.get("status", {}) or {})
            warm = dict(status.get("coverage_warmup", {}) or {})
        if not warm:
            warm = self._default_coverage_warmup_status()
        return copy.deepcopy(warm)

    def _is_low_coverage_item(self, item: Dict[str, Any]) -> bool:
        if not isinstance(item, dict):
            return False
        score = _safe_int(item.get("coverage_score"), 0)
        label = str(item.get("coverage_label") or "").upper().strip()
        badges = {str(b or "").upper().strip() for b in (item.get("badges") or []) if str(b or "").strip()}
        low_coverage = bool(
            score < 45 or label == "LOW" or "LOW_COVERAGE" in badges
        )
        missing = self._analysis_enrichment_missing_fields(item)
        if not missing:
            return low_coverage

        # A completed warmup that confirmed the same fields are still absent
        # gets a field-specific cooldown. Manual per-symbol refresh bypasses
        # this through ``only_needs_coverage=False``; global rotation does not
        # hammer Yahoo indefinitely for genuinely unavailable coverage.
        enrichment = item.get("enrichment")
        retry_after = (
            enrichment.get("retry_after_utc")
            if isinstance(enrichment, dict)
            and isinstance(enrichment.get("retry_after_utc"), dict)
            else {}
        )
        now = _utc_now()
        due_missing = [
            field
            for field in missing
            if (
                _parse_iso(retry_after.get(field)) is None
                or _parse_iso(retry_after.get(field)) <= now
            )
        ]
        return bool(due_missing)

    def _analysis_enrichment_missing_fields(
        self,
        item: Dict[str, Any],
    ) -> List[str]:
        if not isinstance(item, dict):
            return []
        missing: List[str] = []

        beat_rate = _safe_float(item.get("beat_rate_historical"), None)
        beat_samples = max(0, _safe_int(item.get("beat_rate_samples"), 0))
        if (
            beat_rate is None
            or beat_samples < self._MIN_REPRESENTATIVE_BEAT_SAMPLES
        ):
            missing.append("beat_history")

        analyst_rec = str(item.get("analyst_recommendation") or "").strip().lower()
        analyst_missing = (
            _safe_float(item.get("analyst_target_upside_pct"), None) is None
            and _safe_float(item.get("analyst_rec_mean"), None) is None
            and max(0, _safe_int(item.get("analyst_count"), 0)) <= 0
            and analyst_rec in {"", "n/a", "none", "unknown"}
        )
        if analyst_missing:
            missing.append("analyst_consensus")

        if (
            _safe_float(item.get("range_proxy_pct"), None) is None
            and _safe_float(item.get("expected_move_pct"), None) is None
        ):
            missing.append("range_proxy")

        historical_samples = max(
            0,
            _safe_int(item.get("historical_sample_size"), 0),
        )
        if historical_samples < 4:
            missing.append("historical_move_history")

        if not item.get("earnings_time_utc"):
            missing.append("event_window")
        if not any(
            isinstance(row, dict) for row in (item.get("top_news") or [])
        ):
            missing.append("news_context")
        if (
            _safe_float(item.get("market_cap"), None) is None
            and _safe_float(item.get("avg_volume"), None) is None
        ):
            missing.append("liquidity_context")
        return list(dict.fromkeys(missing))

    def _mark_enrichment_attempt(self, item: Dict[str, Any]) -> None:
        if not isinstance(item, dict):
            return
        attempted_at = _utc_now()
        missing = self._analysis_enrichment_missing_fields(item)
        retry_after: Dict[str, str] = {}
        for field in missing:
            seconds = max(
                60,
                _safe_int(
                    self._ENRICHMENT_RETRY_SECONDS.get(field),
                    3600,
                ),
            )
            retry_after[field] = _iso(attempted_at + timedelta(seconds=seconds))
        item["enrichment"] = {
            "last_attempt_utc": _iso(attempted_at),
            "missing_fields": missing,
            "retry_after_utc": retry_after,
            "beat_unavailable_reason": item.get(
                "beat_rate_unavailable_reason"
            ),
        }

    def _carry_forward_enrichment_state(
        self,
        item: Dict[str, Any],
        previous_item: Optional[Dict[str, Any]],
    ) -> None:
        if (
            not isinstance(item, dict)
            or not isinstance(previous_item, dict)
            or not self._items_share_earnings_window(item, previous_item)
            or not isinstance(previous_item.get("enrichment"), dict)
        ):
            return
        current_missing = set(self._analysis_enrichment_missing_fields(item))
        previous = copy.deepcopy(previous_item["enrichment"])
        retry_after = previous.get("retry_after_utc")
        if not isinstance(retry_after, dict):
            return
        previous["missing_fields"] = [
            field
            for field in (previous.get("missing_fields") or [])
            if field in current_missing
        ]
        previous["retry_after_utc"] = {
            field: value
            for field, value in retry_after.items()
            if field in current_missing
        }
        if previous["retry_after_utc"]:
            item["enrichment"] = previous

    @staticmethod
    def _verified_float(value: Any, *, positive: bool = False) -> Optional[float]:
        parsed = _safe_float(value, None)
        if parsed is None or not math.isfinite(float(parsed)):
            return None
        if positive and float(parsed) <= 0.0:
            return None
        return float(parsed)

    def _same_event_enrichment_source(
        self,
        symbol: str,
        earnings_dt: Optional[datetime],
        previous_item: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """Return a persisted row only when it belongs to this earnings event.

        Enrichment is intentionally quarter-scoped. A provider moving the same
        report by a few days is tolerated, while an undated row or the next
        quarterly event cannot inherit analyst/history values accidentally.
        """
        if not isinstance(previous_item, dict) or earnings_dt is None:
            return None
        symbol_u = str(symbol or "").upper().strip()
        previous_symbol = str(previous_item.get("symbol") or "").upper().strip()
        previous_dt = _parse_iso(previous_item.get("earnings_time_utc"))
        if (
            not symbol_u
            or symbol_u != previous_symbol
            or previous_dt is None
        ):
            return None
        if abs((earnings_dt - previous_dt).total_seconds()) > 14 * 86400:
            return None
        return previous_item

    def _persisted_enrichment_is_fresh(
        self,
        previous_item: Optional[Dict[str, Any]],
        field: str,
        now: datetime,
    ) -> bool:
        if not isinstance(previous_item, dict):
            return False
        retry_after = previous_item.get("enrichment_refresh_after_utc")
        if isinstance(retry_after, dict):
            retry_dt = _parse_iso(retry_after.get(field))
            if retry_dt is not None and now < retry_dt:
                # A stale last-good value remains usable while a failed/poorer
                # provider attempt is cooling down. This prevents every regular
                # cycle (and every quick restart) from repeating the same call.
                return True
        timestamps = previous_item.get("enrichment_verified_at_utc")
        verified_at = None
        if isinstance(timestamps, dict):
            verified_at = _parse_iso(timestamps.get(field))
        if verified_at is None:
            # Compatibility for snapshots created before per-field provenance
            # existed. `updated_utc` is only a bootstrap timestamp; once carried,
            # the original value is written into the per-field map.
            verified_at = _parse_iso(previous_item.get("updated_utc"))
        if verified_at is None:
            return False
        ttl = max(
            60,
            _safe_int(
                self._VERIFIED_ENRICHMENT_TTL_SECONDS.get(field),
                3600,
            ),
        )
        age_seconds = (now - verified_at).total_seconds()
        return -300.0 <= age_seconds <= float(ttl)

    @classmethod
    def _beat_snapshot_from_item(
        cls,
        item: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(item, dict):
            return None
        rate = cls._verified_float(item.get("beat_rate_historical"))
        samples = max(0, _safe_int(item.get("beat_rate_samples"), 0))
        beats = max(0, _safe_int(item.get("beat_rate_beats"), 0))
        if (
            rate is None
            or not 0.0 <= rate <= 1.0
            or samples <= 0
            or beats > samples
        ):
            return None
        return {
            "rate": round(rate, 4),
            "beats": beats,
            "samples": samples,
            "source": item.get("beat_rate_source"),
            "unavailable_reason": None,
        }

    @classmethod
    def _historical_snapshot_from_item(
        cls,
        item: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(item, dict):
            return None
        median_move = cls._verified_float(
            item.get("historical_median_move_pct")
            if item.get("historical_median_move_pct") is not None
            else item.get("avg_move_historical")
        )
        samples = max(0, _safe_int(item.get("historical_sample_size"), 0))
        if median_move is None or median_move < 0.0 or samples <= 0:
            return None
        return {
            "median_move_pct": median_move,
            "sample_size": samples,
            "moves": [],
            "source": item.get("gap_risk_history_source"),
        }

    @classmethod
    def _verified_reactions(
        cls,
        value: Any,
    ) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for row in value if isinstance(value, list) else []:
            if not isinstance(row, dict):
                continue
            move = cls._verified_float(row.get("move_pct"))
            if move is None:
                continue
            clean = copy.deepcopy(row)
            clean["move_pct"] = round(move, 2)
            out.append(clean)
        return out

    @classmethod
    def _analyst_snapshot_from_item(
        cls,
        item: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(item, dict):
            return None
        snapshot = {
            "analyst_target_upside_pct": cls._verified_float(
                item.get("analyst_target_upside_pct")
            ),
            "analyst_recommendation": (
                str(item.get("analyst_recommendation") or "").strip().lower()
                or None
            ),
            "analyst_rec_mean": cls._verified_float(
                item.get("analyst_rec_mean")
            ),
            "analyst_count": max(0, _safe_int(item.get("analyst_count"), 0)),
        }
        if snapshot["analyst_recommendation"] in {
            None,
            "n/a",
            "none",
            "unknown",
        }:
            snapshot["analyst_recommendation"] = None
        if (
            snapshot["analyst_target_upside_pct"] is None
            and snapshot["analyst_rec_mean"] is None
            and snapshot["analyst_recommendation"] is None
            and snapshot["analyst_count"] <= 0
        ):
            return None
        return snapshot

    @staticmethod
    def _analyst_snapshot_quality(snapshot: Optional[Dict[str, Any]]) -> Tuple[int, int]:
        if not isinstance(snapshot, dict):
            return (-1, -1)
        meaningful = sum(
            1
            for key in (
                "analyst_target_upside_pct",
                "analyst_recommendation",
                "analyst_rec_mean",
            )
            if snapshot.get(key) is not None
        )
        return (
            max(0, _safe_int(snapshot.get("analyst_count"), 0)),
            meaningful,
        )

    def _enrichment_priority(self, item: Dict[str, Any]) -> int:
        score = _safe_int(item.get("coverage_score"), 0)
        if score < 45:
            return 0
        missing = set(self._analysis_enrichment_missing_fields(item))
        if (
            "beat_history" in missing
            and _safe_int(item.get("beat_rate_samples"), 0) <= 0
        ):
            return 1
        if "beat_history" in missing:
            return 2
        if "historical_move_history" in missing:
            return 3
        if "range_proxy" in missing:
            return 4
        if "analyst_consensus" in missing:
            return 5
        return 6

    def _resolve_coverage_targets(self, symbols: Optional[List[str]], only_needs_coverage: bool) -> List[str]:
        requested: List[str] = []
        if isinstance(symbols, list):
            requested = [str(s or "").upper().strip() for s in symbols if str(s or "").strip()]
        requested = [s for s in requested if _is_earnings_eligible_symbol(s)]
        requested = list(dict.fromkeys(requested))

        with self._lock:
            items = list(self._state.get("items", []) or [])

        if requested:
            if only_needs_coverage:
                low = {
                    str(item.get("symbol") or "").upper().strip()
                    for item in items
                    if self._is_low_coverage_item(item)
                }
                return [s for s in requested if s in low]
            return requested

        state_symbols = [
            str(item.get("symbol") or "").upper().strip()
            for item in items
            if isinstance(item, dict) and str(item.get("symbol") or "").strip()
        ]
        state_symbols = [s for s in state_symbols if _is_earnings_eligible_symbol(s)]
        state_symbols = list(dict.fromkeys(state_symbols))
        if not only_needs_coverage:
            return state_symbols

        low_items = [
            item
            for item in items
            if self._is_low_coverage_item(item)
            and _is_earnings_eligible_symbol(str(item.get("symbol") or "").upper().strip())
        ]
        watchlist = self._watchlist_symbol_set()
        low_items.sort(
            key=lambda item: (
                0 if str(item.get("symbol") or "").upper().strip() in watchlist else 1,
                self._enrichment_priority(item),
                _parse_iso(item.get("earnings_time_utc"))
                or datetime.max.replace(tzinfo=timezone.utc),
                _safe_int(item.get("coverage_score"), 0),
                str(item.get("symbol") or ""),
            )
        )
        return [str(item.get("symbol") or "").upper().strip() for item in low_items]

    def _run_coverage_warmup_worker(
        self,
        job_id: str,
        symbols: List[str],
        batch_size: int,
        allow_remote_on_miss: bool,
    ) -> None:
        cfg = self._get_config()
        with track_agent_run(
            "pre_earnings",
            external_call_budget=int(cfg.get("external_call_budget", 30)),
        ):
            self._run_coverage_warmup_worker_impl(
                job_id,
                symbols,
                batch_size,
                allow_remote_on_miss,
                cfg,
            )

    def _run_coverage_warmup_worker_impl(
        self,
        job_id: str,
        symbols: List[str],
        batch_size: int,
        allow_remote_on_miss: bool,
        cfg: Dict[str, Any],
    ) -> None:
        processed = 0
        success = 0
        failed = 0
        rescored = 0
        needs_full_rescore = False
        last_error = None
        total = len(symbols)
        batches = [symbols[i:i + batch_size] for i in range(0, total, batch_size)]

        for idx, batch in enumerate(batches, start=1):
            with self._lock:
                status = dict(self._state.get("status", {}) or {})
                warm = dict(status.get("coverage_warmup", {}) or self._default_coverage_warmup_status())
                if str(warm.get("job_id") or "") != str(job_id):
                    return
                warm["current_batch"] = idx
                warm["last_progress_at"] = _iso(_utc_now())
                status["coverage_warmup"] = warm
                self._state["status"] = status

            for symbol in batch:
                with self._lock:
                    status = dict(self._state.get("status", {}) or {})
                    warm = dict(status.get("coverage_warmup", {}) or self._default_coverage_warmup_status())
                    if str(warm.get("job_id") or "") != str(job_id):
                        return
                    warm["active_symbol"] = str(symbol or "").upper().strip()
                    warm["last_progress_at"] = _iso(_utc_now())
                    status["coverage_warmup"] = warm
                    self._state["status"] = status
                ok = False
                try:
                    ok = self._warmup_symbol_coverage(symbol=symbol, cfg=cfg, allow_remote_on_miss=allow_remote_on_miss)
                except Exception as e:
                    ok = False
                    last_error = str(e)

                try:
                    rescore = self._rescore_cached_symbol(symbol=symbol, cfg=cfg)
                    if str(rescore.get("status") or "").lower() == "ok":
                        rescored += 1
                    else:
                        needs_full_rescore = True
                        if not last_error:
                            last_error = str(rescore.get("status") or "targeted_rescore_failed")
                except Exception as e:
                    needs_full_rescore = True
                    logger.error(
                        "Pre-earnings targeted rescore failed for %s: %s",
                        symbol,
                        e,
                        exc_info=True,
                    )
                    if not last_error:
                        last_error = str(e)

                processed += 1
                if ok:
                    success += 1
                else:
                    failed += 1

                with self._lock:
                    status = dict(self._state.get("status", {}) or {})
                    warm = dict(status.get("coverage_warmup", {}) or self._default_coverage_warmup_status())
                    if str(warm.get("job_id") or "") != str(job_id):
                        return
                    warm["processed"] = int(processed)
                    warm["success"] = int(success)
                    warm["failed"] = int(failed)
                    warm["active_symbol"] = None
                    warm["last_symbol"] = str(symbol or "").upper().strip()
                    warm["last_progress_at"] = _iso(_utc_now())
                    if last_error:
                        warm["last_error"] = last_error
                    status["coverage_warmup"] = warm
                    self._state["status"] = status
                    # self._persist_locked()  # MOVED: Now persists at batch Level below

            # Persist at the end of each batch to reduce I/O pressure (Stage 3 Optimization)
            with self._lock:
                self._persist_locked()

            try:
                pass
            except Exception:
                pass
            time.sleep(0.15)

        with self._lock:
            status = dict(self._state.get("status", {}) or {})
            warm = dict(status.get("coverage_warmup", {}) or self._default_coverage_warmup_status())
            if str(warm.get("job_id") or "") == str(job_id):
                warm["running"] = False
                warm["processed"] = int(processed)
                warm["success"] = int(success)
                warm["failed"] = int(failed)
                warm["active_symbol"] = None
                warm["last_error"] = last_error
                warm["last_finished"] = _iso(_utc_now())
                warm["last_progress_at"] = warm["last_finished"]
                if failed == 0 or rescored:
                    warm["last_success"] = warm["last_finished"]
                warm["rescored"] = int(rescored)
                if rescored:
                    # The targeted items are already persisted.  Publishing the
                    # same timestamp lets the UI finish polling without waiting
                    # for an unrelated full-universe cycle.
                    status["last_success_time"] = warm["last_finished"]
                status["coverage_warmup"] = warm
                self._state["status"] = status
                self._persist_locked()
            pending_symbols = list(
                dict.fromkeys(getattr(self, "_coverage_pending_symbols", []) or [])
            )
            self._coverage_pending_symbols = []

        # Explicit symbol refreshes that arrived during a global warmup run next,
        # before rescoring, so the UI cannot report completion for a ticker that
        # was never enriched.
        if pending_symbols:
            chained = self.request_coverage_warmup(
                symbols=pending_symbols,
                batch_size=min(5, len(pending_symbols)),
                only_needs_coverage=False,
                allow_remote_on_miss=allow_remote_on_miss,
            )
            if str(chained.get("status") or "").lower() == "accepted":
                return

        # Existing rows are rescored and persisted atomically above.  A full
        # universe rebuild is only needed when the requested symbol was absent
        # (for example, a newly discovered calendar name).
        if needs_full_rescore:
            try:
                if self._is_enabled():
                    self.request_refresh()
            except Exception:
                pass

    def _warmup_symbol_coverage(self, symbol: str, cfg: Dict[str, Any], allow_remote_on_miss: bool) -> bool:
        sym = str(symbol or "").upper().strip()
        if not sym or not _is_earnings_eligible_symbol(sym):
            return False

        # News cache-first: remote only on cache miss.
        news_ok = False
        try:
            news_agent = get_news_tab_agent_if_ready()
            if news_agent is None:
                news_agent = get_news_tab_agent(self.config_manager.config)
            if news_agent is not None and hasattr(news_agent, "ensure_ticker_news_cached"):
                result = news_agent.ensure_ticker_news_cached(
                    sym,
                    force_refresh=False,
                    allow_remote=bool(allow_remote_on_miss),
                ) or {}
                news_ok = bool(_safe_int(result.get("count"), 0) > 0)
        except Exception:
            news_ok = False

        # Earnings cache-first: targeted refresh only when missing.
        hist = self._get_cached_earnings_history(sym, max_events=12)
        if not hist and allow_remote_on_miss:
            self._refresh_earnings_for_symbols(cfg, [sym])
            hist = self._get_cached_earnings_history(sym, max_events=12)
        earnings_ok = bool(hist)

        # Yahoo Finance data warmups populate the exact TTL caches read by the
        # scoring path. Calling provider methods and discarding their results
        # made the previous "warmup" report success without changing coverage.
        beat_stats = self._compute_historical_beat_stats(
            sym,
            allow_remote=bool(allow_remote_on_miss),
            force_refresh=bool(allow_remote_on_miss),
        )
        quote_ok = bool(self._get_cached_quote(sym))
        history_ok = False
        try:
            self._load_symbol_daily_bars(sym)
            history_stats = self._compute_historical_earnings_gap_stats(sym)
            history_ok = _safe_int(history_stats.get("sample_size"), 0) > 0
        except Exception:
            pass
        try:
            self._estimate_earnings_trend_score(sym)
            self._get_analyst_estimates_cached(sym)
        except Exception:
            pass

        beat_ok = _safe_int(beat_stats.get("samples"), 0) > 0
        # "Success" means the enrichment obtained the inputs that materially
        # close a low-coverage row, not merely that one cached quote happened
        # to exist. Calendar availability is reported separately.
        return bool(news_ok and history_ok and quote_ok and beat_ok)

    def _cached_news_rows_for_symbol(self, symbol: str) -> List[Dict[str, Any]]:
        """Read one symbol's already-warmed news without starting another job."""
        sym = str(symbol or "").upper().strip()
        if not sym:
            return []
        try:
            news_agent = get_news_tab_agent_if_ready()
            if news_agent is None:
                news_agent = get_news_tab_agent(self.config_manager.config)
            rows = news_agent.get_all_articles_flat(active_tickers=None) or []
            return list(self._build_news_index(rows, [sym]).get(sym, []) or [])
        except Exception:
            return []

    def _cached_earnings_rows_for_symbol(
        self,
        symbol: str,
        cfg: Dict[str, Any],
        current_item: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Read the canonical calendar cache, with the displayed event as fallback."""
        sym = str(symbol or "").upper().strip()
        rows: List[Dict[str, Any]] = []
        try:
            lookahead_days = max(1, int(cfg.get("lookahead_days", 7)))
            today = _utc_now().date()
            start = datetime.combine(today - timedelta(days=1), datetime.min.time())
            end = datetime.combine(
                today + timedelta(days=lookahead_days + 2),
                datetime.max.time(),
            )
            calendar = get_economic_calendar_agent(self.config_manager.config)
            cached = calendar.get_calendar_events_range(
                start,
                end,
                watchlist_tickers=[sym],
                allow_remote=False,
                include_all_reporters=False,
            ) or {}
            rows = [
                row
                for row in (cached.get("earnings", []) or [])
                if isinstance(row, dict)
                and str(row.get("symbol") or "").upper().strip() == sym
            ]
        except Exception:
            rows = []

        if rows:
            return rows
        event_utc = current_item.get("earnings_time_utc")
        if not event_utc:
            return []
        return [
            {
                "id": f"earn_{sym}_targeted_cache",
                "symbol": sym,
                "date": event_utc,
                "date_formatted": event_utc,
                "session": current_item.get("session"),
                "confirmed": bool(current_item.get("confirmed")),
                "category": "earnings",
                "source": "targeted_cache",
            }
        ]

    def _rescore_cached_symbol(self, symbol: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
        """Rebuild and persist one warmed row without scanning the universe."""
        sym = str(symbol or "").upper().strip()
        if not sym:
            return {"status": "invalid_symbol", "symbol": sym}

        with self._lock:
            current = next(
                (
                    copy.deepcopy(item)
                    for item in (self._state.get("items", []) or [])
                    if isinstance(item, dict)
                    and str(item.get("symbol") or "").upper().strip() == sym
                ),
                None,
            )
        if not isinstance(current, dict):
            return {"status": "not_found", "symbol": sym}

        news_rows = self._cached_news_rows_for_symbol(sym)
        if not news_rows:
            news_rows = [
                dict(row)
                for row in (current.get("top_news") or [])
                if isinstance(row, dict)
            ]
        earnings_rows = self._cached_earnings_rows_for_symbol(sym, cfg, current)
        if not earnings_rows:
            return {"status": "missing_event", "symbol": sym}

        state_rows = self._load_state_rows_map()
        state_row = dict(state_rows.get(sym, {}) or {})
        # The persisted pre-earnings row contains useful last-good values that
        # are not guaranteed to exist in the global state file.
        for key in (
            "market_cap",
            "sector",
            "coverage_score",
            "coverage_label",
            "coverage_contributors",
        ):
            if current.get(key) is not None and state_row.get(key) is None:
                state_row[key] = copy.deepcopy(current[key])

        feedback = getattr(self, "_feedback", {}) or {}
        weight_overrides = dict(feedback.get("weights") or {})
        calibration_model = dict(feedback.get("calibration_model") or {})
        calibration_metrics = dict((self._state.get("calibration") or {}))
        flags = dict(current.get("flags") or {})
        mismatch_info = {"reason": "persisted_mismatch"} if flags.get("calendar_news_mismatch") else None
        already_reported_info = None
        if flags.get("already_reported"):
            already_reported_info = {
                "headlines": list(flags.get("already_reported_evidence") or [])
            }

        rebuilt = self._build_item_for_symbol(
            symbol=sym,
            news_rows=news_rows,
            earnings_rows=earnings_rows,
            mismatch_info=mismatch_info,
            already_reported_info=already_reported_info,
            cfg=cfg,
            state_row=state_row,
            as_of_utc=_iso(_utc_now()),
            weight_overrides=weight_overrides or None,
            calibration_model=calibration_model or None,
            calibration_metrics=calibration_metrics or None,
            previous_item=current,
            refresh_enrichment=True,
        )
        self._carry_forward_ai_decision(rebuilt, current, cfg)
        self._carry_forward_pre_earnings_decision(rebuilt, current)
        self._mark_enrichment_attempt(rebuilt)

        with self._lock:
            items = list(self._state.get("items", []) or [])
            idx = next(
                (
                    i
                    for i, item in enumerate(items)
                    if isinstance(item, dict)
                    and str(item.get("symbol") or "").upper().strip() == sym
                ),
                None,
            )
            if idx is None:
                return {"status": "not_found", "symbol": sym}
            latest = items[idx]
            if not self._items_share_earnings_window(latest, rebuilt):
                return {"status": "stale_event", "symbol": sym}
            # Preserve an AI refresh that completed while the cached rebuild was
            # running, then publish the complete row in one locked swap.
            self._carry_forward_ai_decision(rebuilt, latest, cfg)
            self._carry_forward_pre_earnings_decision(rebuilt, latest)
            items[idx] = rebuilt
            self._state["items"] = items
            status = dict(self._state.get("status", {}) or {})
            status["last_targeted_rescore_symbol"] = sym
            status["last_targeted_rescore_time"] = _iso(_utc_now())
            status["last_success_time"] = status["last_targeted_rescore_time"]
            self._state["status"] = status
            self._record_concurrent_item_update_locked(sym, rebuilt, "targeted_rescore")
            self._persist_locked()

        return {
            "status": "ok",
            "symbol": sym,
            "coverage_score": rebuilt.get("coverage_score"),
            "beat_rate_historical": rebuilt.get("beat_rate_historical"),
        }

    def run_once(self) -> Dict[str, Any]:
        return self.run_cycle()

    def _calendar_universe_summary(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        """Return the exact cache-only discovery universe for the active window."""
        today = _utc_now().date()
        lookahead_days = max(1, _safe_int(cfg.get("lookahead_days"), 10))
        end_day = today + timedelta(days=lookahead_days)
        try:
            calendar = get_economic_calendar_agent_if_ready()
            if calendar is None:
                rows = []
            else:
                snapshot = calendar.get_calendar_events_range(
                    datetime.combine(today, datetime.min.time()),
                    datetime.combine(end_day, datetime.max.time()),
                    watchlist_tickers=None,
                    allow_remote=False,
                    include_all_reporters=True,
                ) or {}
                rows = [
                    row
                    for row in (snapshot.get("earnings") or [])
                    if isinstance(row, dict)
                ]
        except Exception as exc:
            logger.warning("Pre-Earnings calendar-universe summary unavailable: %s", exc)
            rows = []
        by_symbol: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            symbol = str(row.get("symbol") or "").upper().strip()
            if symbol:
                previous = by_symbol.get(symbol)
                if previous is None or self._event_source_priority(row) > self._event_source_priority(previous):
                    by_symbol[symbol] = row
        estimated = sum(
            1 for row in by_symbol.values()
            if self._event_source_priority(row) < 70
        )
        return {
            "window_start": today.isoformat(),
            "window_end": end_day.isoformat(),
            "reporter_count": len(by_symbol),
            "confirmed_or_provider_count": len(by_symbol) - estimated,
            "estimated_count": estimated,
            "source": "calendar_cache",
        }

    def get_state(self) -> Dict[str, Any]:
        # The persisted snapshot can be several MB. Keep enough latency budget
        # for its defensive deep copy and ranking work when a writer owns the
        # lock; waiting 150 ms here made the documented non-blocking API exceed
        # its 350 ms contract under ordinary CPU contention.
        acquired = self._lock.acquire(timeout=0.05)
        if acquired:
            try:
                payload = copy.deepcopy(self._state)
                self._state_read_fallback = copy.deepcopy(payload)
            finally:
                self._lock.release()
        else:
            # Serve the last atomically published deep snapshot while a writer
            # owns the lock. A shallow copy leaks nested mutations back into the
            # fallback and can let a pre-refresh row seed the API cache.
            payload = copy.deepcopy(self._state_read_fallback)
        cfg = self._get_config()
        status = payload.get("status", {}) if isinstance(payload, dict) else {}
        warm = dict(status.get("coverage_warmup", {}) or {})
        if not warm:
            warm = self._default_coverage_warmup_status()
        status["coverage_warmup"] = warm
        last_success = _full_cycle_schedule_anchor(payload)
        if self._is_enabled() and last_success:
            elapsed = max(0.0, (_utc_now() - last_success).total_seconds())
            status["next_run_in"] = max(0, int(cfg["refresh_seconds"] - elapsed))
        elif self._is_enabled():
            status["next_run_in"] = 0
        elif not self._is_enabled():
            status["next_run_in"] = None
        payload["status"] = status
        # Normalize legacy persisted snapshots so the additive premium contract
        # is available immediately after deploy, before the next full scan
        # completes. This is read-only; the on-disk snapshot is untouched.
        reported_today = payload.get("reported_today")
        if not isinstance(reported_today, list):
            reported_today = []
        # Persisted snapshots can outlive their earnings event (for example
        # after an overnight restart). Never expose those rows as active
        # candidates while a full provider refresh is still warming. Apply the
        # same session-aware truth gate used by the write path, then retain a
        # compact transparency record outside the active table.
        visible_items = [
            item for item in (payload.get("items") or []) if isinstance(item, dict)
        ]
        watchlist_symbols = {
            str(item.get("symbol") or "").upper().strip()
            for item in visible_items
            if item.get("is_watchlist")
        }
        visible_items, newly_reported = self._filter_reported_items(
            visible_items,
            cfg,
            _utc_now(),
            watchlist=watchlist_symbols,
        )
        if newly_reported:
            reported_by_key = {
                (
                    str(row.get("symbol") or "").upper().strip(),
                    str(row.get("earnings_time_utc") or ""),
                ): row
                for row in reported_today
                if isinstance(row, dict)
            }
            for row in newly_reported:
                key = (
                    str(row.get("symbol") or "").upper().strip(),
                    str(row.get("earnings_time_utc") or ""),
                )
                reported_by_key[key] = row
            reported_today = list(reported_by_key.values())
        payload["items"] = visible_items
        pulse = payload.get("pulse")
        if not isinstance(pulse, dict):
            pulse = None
        flags = dict(payload.get("flags") or {})
        flags["reported_today_count"] = len(reported_today)
        flags["pulse_used"] = pulse is not None
        payload["reported_today"] = reported_today
        payload["pulse"] = pulse
        payload["flags"] = flags
        for item in payload.get("items", []) or []:
            if isinstance(item, dict):
                news_rows = [row for row in (item.get("top_news") or []) if isinstance(row, dict)]
                clean_news = [
                    row for row in news_rows
                    if not _is_crypto_only_equity_news(row, item.get("symbol"))
                ]
                removed_crypto_rows = len(news_rows) - len(clean_news)
                if removed_crypto_rows:
                    item["top_news"] = clean_news
                    item["news_count"] = len(clean_news)
                    digest = dict(item.get("news_digest") or {})
                    digest["article_count"] = len(clean_news)
                    digest["evidence_count"] = min(
                        len(clean_news),
                        _safe_int(digest.get("evidence_count"), len(clean_news)),
                    )
                    digest["unique_sources"] = len({
                        str(row.get("publisher") or row.get("source") or "").lower().strip()
                        for row in clean_news
                        if str(row.get("publisher") or row.get("source") or "").strip()
                    })
                    item["news_digest"] = digest
                    warnings = list(item.get("news_quality_warnings") or [])
                    warnings.append(
                        f"Removed {removed_crypto_rows} crypto-token article(s) from equity evidence"
                    )
                    item["news_quality_warnings"] = list(dict.fromkeys(warnings))
                if not item.get("event_source_url"):
                    item["event_source_url"] = self._resolved_event_source_url(item)
                self._synchronize_direction_decision(item, cfg)
        position_contexts = self._attach_open_position_context(payload.get("items") or [], cfg)
        self._attach_daily_ranking(payload, cfg)
        self._attach_pulse_position_context(payload.get("pulse"), position_contexts)
        payload["calendar_universe"] = self._calendar_universe_summary(cfg)
        payload["disabled"] = not self._is_enabled()
        return payload

    def _lookup_frozen_evidence(
        self,
        symbol: str,
        event_iso: Optional[str],
    ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        """Read evidence for the exact event, including settled predictions."""
        event_dt = _parse_iso(event_iso)
        if event_dt is None:
            return None, None
        symbol = str(symbol or "").upper().strip()
        with self._feedback_guard():
            entries = list((self._feedback.get("open_predictions") or {}).values())
            entries.extend(self._feedback.get("outcomes") or [])
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                if str(entry.get("symbol") or "").upper().strip() != symbol:
                    continue
                if _parse_iso(entry.get("event_utc")) != event_dt:
                    continue
                first = entry.get("first_direction_evidence")
                top = entry.get("top_direction_evidence")
                if isinstance(first, dict) or isinstance(top, dict):
                    return (
                        copy.deepcopy(first) if isinstance(first, dict) else None,
                        copy.deepcopy(top) if isinstance(top, dict) else None,
                    )
        return None, None

    def get_verification_dossier(self, symbol: str, event_utc: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Serving path for the per-row verification dossier (I04).

        Builds the dossier from a deep copy of the stored item — the live state
        is never mutated by a read. Derived fields are recomputed on the copy
        with the same idempotent sync the serving table applies, so the dossier
        matches what the UI row shows.
        """
        from core.pre_earnings_evidence import build_verification_dossier, summarize_evidence

        sym = str(symbol or "").upper().strip()
        if not sym:
            return None
        requested_event = _parse_iso(event_utc) if event_utc else None
        if event_utc and requested_event is None:
            raise ValueError("Invalid earnings event timestamp")
        cfg = self._get_config()
        lock = getattr(self, "_lock", None)
        if lock is not None and not lock.acquire(timeout=0.25):
            raise TimeoutError("Pre-Earnings state is busy; retry shortly")
        try:
            raw = next(
                (
                    copy.deepcopy(item)
                    for item in (self._state.get("items") or [])
                    if isinstance(item, dict)
                    and str(item.get("symbol") or "").upper().strip() == sym
                    and (requested_event is None or _parse_iso(item.get("earnings_time_utc")) == requested_event)
                ),
                None,
            )
        finally:
            if lock is not None:
                lock.release()
        if raw is None:
            return None
        try:
            self._synchronize_direction_decision(raw, cfg)
            details = self._prediction_score_details(raw, cfg)
            raw["trader_score"] = details["score"]
            raw["trader_score_band"] = details["band"]
            raw["recommendation_eligible"] = bool(details["eligible"])
            raw["recommendation_blockers"] = details["blockers"]
        except Exception as exc:
            raw["recommendation_eligible"] = False
            raw["recommendation_blockers"] = ["verification_recompute_failed"]
            logger.warning("Pre-Earnings dossier recomputation failed for %s: %s", sym, exc)
        first_evidence, top_evidence = self._lookup_frozen_evidence(sym, raw.get("earnings_time_utc"))
        return {
            "symbol": sym,
            "earnings_time_utc": raw.get("earnings_time_utc"),
            "generated_at_utc": _iso(_utc_now()),
            "dossier": build_verification_dossier(raw, cfg),
            "first_direction_evidence": summarize_evidence(first_evidence),
            "top_direction_evidence": summarize_evidence(top_evidence),
        }

    @staticmethod
    def _attach_open_position_context(
        items: List[Dict[str, Any]],
        cfg: Dict[str, Any],
    ) -> Dict[str, Dict[str, Any]]:
        """Explain cross-strategy direction conflicts without hiding horizons."""
        try:
            from core.capital_manager import get_capital_manager, is_research_position

            positions = [
                dict(position)
                for position in (get_capital_manager().get_positions() or [])
                if isinstance(position, dict)
                and str(position.get("status") or "").upper().strip() == "OPEN"
                and not is_research_position(position)
            ]
        except Exception as exc:
            logger.warning("Pre-Earnings active-position context unavailable: %s", exc)
            positions = []

        by_symbol: Dict[str, List[Dict[str, Any]]] = {}
        for position in positions:
            symbol = str(position.get("ticker") or "").upper().strip()
            if symbol:
                by_symbol.setdefault(symbol, []).append(position)

        contexts: Dict[str, Dict[str, Any]] = {}
        current = _utc_now()
        window_hours = max(0.25, float(cfg.get("entry_window_hours", 2.0) or 2.0))
        for item in items or []:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol") or "").upper().strip()
            recommendation = str(item.get("direction_label") or "").upper().strip()
            window_open, window_reason, window_meta = is_pre_earnings_entry_window(
                item.get("earnings_time_utc"),
                item.get("session"),
                now=current,
                window_hours=window_hours,
            )
            active = by_symbol.get(symbol, [])
            active_directions = sorted({
                str(position.get("direction") or "").upper().strip()
                for position in active
                if str(position.get("direction") or "").strip()
            })
            opposing = [
                position
                for position in active
                if recommendation in {"LONG", "SHORT"}
                and str(position.get("direction") or "").upper().strip() != recommendation
            ]
            conflict = bool(opposing)
            if conflict and window_open:
                execution_state = "BLOCKED_OPPOSITE_OPEN_POSITION"
                conflict_type = "execution_conflict"
            elif conflict:
                execution_state = "RESEARCH_RANKING_FUTURE_EVENT"
                conflict_type = "cross_horizon"
            elif window_open:
                execution_state = "ENTRY_WINDOW_OPEN"
                conflict_type = "none"
            else:
                execution_state = "WAITING_FOR_ENTRY_WINDOW"
                conflict_type = "none"

            context = {
                "has_open_position": bool(active),
                "active_directions": active_directions,
                "position_ids": [
                    str(position.get("position_id") or "")
                    for position in active
                    if str(position.get("position_id") or "")
                ],
                "strategies": list(dict.fromkeys(
                    str(position.get("strategy_type") or "")
                    for position in active
                    if str(position.get("strategy_type") or "")
                )),
                "recommendation_direction": recommendation or None,
                "direction_conflict": conflict,
                "conflict_type": conflict_type,
                "execution_state": execution_state,
                "execution_eligible_now": bool(window_open and not conflict),
                "entry_window_open": bool(window_open),
                "entry_window_reason": window_reason,
                "entry_window": window_meta,
                "message": (
                    f"Active {','.join(active_directions)} position conflicts with the Pre-Earnings "
                    f"{recommendation} bias. This is a future-event research ranking; no opposite "
                    "order is eligible now, and Risk/Capital will require an explicit close/reversal."
                    if conflict and not window_open
                    else (
                        f"Active {','.join(active_directions)} position blocks the Pre-Earnings "
                        f"{recommendation} entry until an explicit close/reversal."
                        if conflict
                        else window_reason
                    )
                ),
            }
            item["active_position_context"] = context
            item["position_direction_conflict"] = conflict
            item["execution_state"] = execution_state
            item["execution_eligible_now"] = bool(window_open and not conflict)
            contexts[symbol] = context
        return contexts

    @staticmethod
    def _attach_pulse_position_context(
        pulse: Optional[Dict[str, Any]],
        contexts: Dict[str, Dict[str, Any]],
    ) -> None:
        if not isinstance(pulse, dict) or not contexts:
            return

        def annotate(rows: Any) -> None:
            for row in rows if isinstance(rows, list) else []:
                if not isinstance(row, dict):
                    continue
                context = contexts.get(str(row.get("symbol") or "").upper().strip())
                if context:
                    row["active_position_context"] = copy.deepcopy(context)
                    row["position_direction_conflict"] = bool(context.get("direction_conflict"))
                    row["execution_state"] = context.get("execution_state")

        for key in ("top_long", "top_short", "current_top_long", "current_top_short"):
            annotate(pulse.get(key))
        active_history = pulse.get("active_history")
        if isinstance(active_history, dict):
            annotate(active_history.get("top_long"))
            annotate(active_history.get("top_short"))

    def run_cycle(self) -> Dict[str, Any]:
        self.config_manager.reload_if_changed()
        cfg = self._get_config()
        now = _utc_now()
        now_iso = _iso(now)

        with self._lock:
            cycle_update_revision = int(
                getattr(self, "_concurrent_item_revision", 0)
            )
            status = dict(self._state.get("status", {}) or {})
            coverage_warm = dict(status.get("coverage_warmup", {}) or self._default_coverage_warmup_status())
            status["last_run_time"] = now_iso
            # A full rebuild takes minutes (cold caches after a restart). Tell
            # readers one is in flight so an old snapshot is shown as
            # "refreshing since ..." rather than as a dead, paused tab.
            status["cycle_running"] = True
            status["cycle_started_at"] = now_iso
            self._state["status"] = status

        if not self._is_enabled():
            with self._lock:
                self._state["disabled"] = True
                self._state["refresh_seconds"] = int(cfg["refresh_seconds"])
                self._state["status"]["next_run_in"] = None
                self._state["status"]["cycle_running"] = False
                self._persist_locked()
                return copy.deepcopy(self._state)

        try:
            # ── Early AI pass (2026-08): enrich AI verdicts on the persisted items
            #    BEFORE the slow per-ticker rebuild so the UI shows `ai_decision`
            #    promptly (the full rebuild can take 10+ min to reach _apply_ollama).
            #    Additive + non-fatal; both passes share one per-cycle call quota.
            ai_cycle_quota = {
                "remaining": max(
                    1,
                    _safe_int(cfg.get("ollama_max_tickers"), 25),
                ),
                "attempted": 0,
            }
            # A full rebuild can take several minutes. Publish missing canonical
            # decisions from the last good snapshot before that slow work so the
            # autonomous entry prefilter is never starved after startup/restart.
            with self._lock:
                persisted_decision_items = copy.deepcopy(self._state.get("items") or [])
            decision_limit = max(0, _safe_int(cfg.get("decision_max_tickers"), 25))
            bounded_decision_items = [
                item
                for item in persisted_decision_items
                if isinstance(item, dict) and item.get("symbol")
            ][:decision_limit]
            missing_decision_items = [
                item
                for item in bounded_decision_items
                if not isinstance(item.get("pre_earnings_decision"), dict)
            ]
            if missing_decision_items:
                # Do not spend AI calls re-evaluating canonical decisions that
                # already exist merely because another row in the bounded
                # startup window is missing one.
                self._attach_pre_earnings_decisions(cfg, items=missing_decision_items)
                decisions_by_symbol = {
                    str(item.get("symbol") or "").upper().strip(): item
                    for item in missing_decision_items
                    if isinstance(item, dict)
                    and isinstance(item.get("pre_earnings_decision"), dict)
                }
                with self._lock:
                    current_items = list(self._state.get("items") or [])
                    for current in current_items:
                        if not isinstance(current, dict):
                            continue
                        source = decisions_by_symbol.get(
                            str(current.get("symbol") or "").upper().strip()
                        )
                        if source is not None and self._items_share_earnings_window(current, source):
                            current["pre_earnings_decision"] = copy.deepcopy(
                                source["pre_earnings_decision"]
                            )
                    self._state["items"] = current_items
                    self._persist_locked()
            # General AI enrichment can consume the full per-cycle timeout
            # budget, so it runs only after the execution-critical canonical
            # decisions above have been published.
            self._early_ai_pass(
                cfg,
                cycle_quota=ai_cycle_quota,
                concurrent_since_revision=cycle_update_revision,
            )
            with self._lock:
                previous_items_by_symbol = {
                    str(item.get("symbol") or "").upper().strip(): copy.deepcopy(item)
                    for item in (self._state.get("items") or [])
                    if isinstance(item, dict)
                    and str(item.get("symbol") or "").strip()
                }
            watchlist_symbols = self._get_watchlist_symbols()
            # Sticky market-cap (Faza 3, 2026-07-07): the live server runs the whole
            # agent fleet concurrently, so yfinance .info and the FinnHub 60/min
            # window are both contended during this 500+ fetch bulk run and the
            # per-symbol market_cap fetch often returns None. Market cap is near-
            # constant over a refresh interval, so carry the last good value per
            # symbol forward and reuse it when the fresh fetch is throttled — same
            # pattern as the analyst-estimates "keep serving the last good value".
            self._prev_market_caps = self._load_prev_market_caps()
            earnings_items, earnings_age, earnings_available = self._load_earnings(cfg)
            earnings_index = self._build_earnings_index(earnings_items)
            earnings_symbols = list(earnings_index.keys())

            scope_limit = max(200, int(cfg.get("max_results", 200)) * 3)
            previous_visible = [
                str(item.get("symbol") or "").upper().strip()
                for item in (self._state.get("items") or [])
                if isinstance(item, dict) and str(item.get("symbol") or "").strip()
            ]
            nearest_earnings_symbols = sorted(
                earnings_symbols,
                key=lambda sym: (
                    _parse_iso(
                        (self._pick_primary_event(earnings_index.get(sym, []) or {}) or {}).get("date")
                        or (self._pick_primary_event(earnings_index.get(sym, []) or {}) or {}).get("date_formatted")
                    )
                    or datetime.max.replace(tzinfo=timezone.utc)
                ),
            )
            # User/watchlist and already-visible names must never be evicted by
            # thousands of far-future calendar reporters.
            news_symbols_scope = list(
                dict.fromkeys(
                    watchlist_symbols + previous_visible + nearest_earnings_symbols
                )
            )[:scope_limit]
            previous_priority = [
                str(item.get("symbol") or "").upper().strip()
                for item in sorted(
                    previous_items_by_symbol.values(),
                    key=lambda item: (
                        _safe_int(item.get("trader_score"), -1),
                        _safe_int(item.get("opportunity_score"), -1),
                        abs(_safe_int(item.get("direction_bias_score"), 0)),
                    ),
                    reverse=True,
                )
                if str(item.get("symbol") or "").strip()
            ]
            news_priority_symbols = list(
                dict.fromkeys(
                    previous_priority
                    + watchlist_symbols
                    + nearest_earnings_symbols[:30]
                )
            )
            news_items, news_age, news_available = self._load_news_items(
                cfg,
                news_symbols_scope,
                priority_symbols=news_priority_symbols,
            )
            news_index = self._build_news_index(news_items, news_symbols_scope or earnings_symbols)
            # Resolve the strongest/closest unverified events before expensive
            # enrichment. This includes both explicit date conflicts and old
            # cache rows that predate provenance metadata. The old implementation
            # spent all 30 Yahoo checks on an arbitrary conflict-map order, which
            # could leave every otherwise strong candidate unverified and yield an
            # empty slate even with valid reporters in the calendar.
            verification_needed = [
                symbol
                for symbol, rows in earnings_index.items()
                if (
                    any(str(row.get("event_status") or "").upper() == "DATE_CONFLICT" for row in rows)
                    or not any(
                        str(row.get("event_status") or "").upper() == "UPCOMING"
                        and int(row.get("source_priority") or 0) >= 70
                        and self._normalize_session(row.get("session") or row.get("when")) in {"BMO", "AMC"}
                        for row in rows
                    )
                )
            ]

            def _earliest_candidate_event(symbol: str) -> datetime:
                dates = [
                    _parse_iso(row.get("date") or row.get("date_formatted"))
                    for row in (earnings_index.get(symbol, []) or [])
                    if isinstance(row, dict)
                    and str(row.get("event_status") or "").upper() != "REPORTED"
                ]
                dates = [value for value in dates if value is not None]
                return min(dates) if dates else datetime.max.replace(tzinfo=timezone.utc)

            priority_position = {
                symbol: index
                for index, symbol in enumerate(
                    list(dict.fromkeys(previous_priority + watchlist_symbols))
                )
            }
            verification_needed.sort(
                key=lambda symbol: (
                    0 if symbol in priority_position else 1,
                    priority_position.get(symbol, 10**9),
                    _earliest_candidate_event(symbol),
                )
            )
            canonical_refresh_symbols = verification_needed[:30]
            if canonical_refresh_symbols and self._is_enabled():
                canonical_rows = self._refresh_earnings_for_symbols(
                    cfg,
                    canonical_refresh_symbols,
                    force_yfinance=True,
                )
                resolved_symbols = {
                    str(row.get("symbol") or "").upper().strip()
                    for row in canonical_rows
                    if isinstance(row, dict)
                    and "yfinance" in str(row.get("source") or "").lower()
                }
                if canonical_rows:
                    today_date = now.date()
                    earnings_items = [
                        row
                        for row in earnings_items
                        if not (
                            str((row or {}).get("symbol") or "").upper().strip() in resolved_symbols
                            and (_parse_iso((row or {}).get("date") or (row or {}).get("date_formatted")) is not None)
                            and _parse_iso((row or {}).get("date") or (row or {}).get("date_formatted")).date() >= today_date
                        )
                    ]
                    merged_by_id: Dict[str, Dict[str, Any]] = {}
                    for row in list(earnings_items) + list(canonical_rows):
                        if not isinstance(row, dict):
                            continue
                        row_id = str(
                            row.get("id")
                            or f"{row.get('symbol', '')}_{row.get('date') or row.get('date_formatted') or ''}"
                        )
                        previous = merged_by_id.get(row_id)
                        if previous is None or self._event_source_priority(row) >= self._event_source_priority(previous):
                            merged_by_id[row_id] = row
                    earnings_items = list(merged_by_id.values())
                    earnings_index = self._build_earnings_index(earnings_items)
                    earnings_symbols = list(earnings_index.keys())
            lookback_hours = min(168, max(24, int(cfg.get("lookback_hours", 336))))
            lookahead_days = int(cfg.get("lookahead_days", 7))
            mismatch = detect_calendar_mismatch_symbols(
                news_index,
                earnings_index,
                now=now,
                min_hits=2,
                recent_hours=lookback_hours,
                lookahead_days=lookahead_days,
            )
            calendar_refresh_attempted_symbols: List[str] = []
            calendar_refresh_results: Dict[str, Any] = {}

            # Cache-first fallback: if mismatch symbols are missing from cached calendar,
            # trigger a targeted calendar refresh for only those symbols.
            mismatch_symbols = [s for s in mismatch.keys() if s and s not in earnings_index]
            # Broaden beyond the watchlist: also (re)resolve earnings for the curated
            # large-cap universe (cached ~6h) so the radar shows the whole market's
            # upcoming reporters, not just tracked tickers. Cheap after the first cycle.
            universe_targets = [s for s in PRE_EARNINGS_UNIVERSE if s not in earnings_index]
            # Also resolve earnings for the user's WATCHLIST tickers (prioritised): the
            # calendar cache often lacks small/newer names (e.g. BMNR), so without this a
            # watched ticker reporting tomorrow never reaches the radar even though Home's
            # earnings widget shows it. Resolved via the yfinance fallback like the universe.
            watchlist_targets = [s for s in watchlist_symbols if s and s not in earnings_index]
            refresh_targets = list(dict.fromkeys(mismatch_symbols + watchlist_targets + universe_targets))
            if refresh_targets and self._is_enabled():
                calendar_refresh_attempted_symbols = list(dict.fromkeys(refresh_targets))
                refreshed_missing = self._refresh_earnings_for_symbols(cfg, refresh_targets)
                refreshed_index = self._build_earnings_index(refreshed_missing or [])
                for sym in calendar_refresh_attempted_symbols:
                    events_found = len(refreshed_index.get(sym, []) or [])
                    calendar_refresh_results[sym] = {
                        "status": "refreshed" if events_found > 0 else "missing_after_refresh",
                        "events_found": int(events_found),
                    }
                if refreshed_missing:
                    merged_by_id: Dict[str, Dict[str, Any]] = {}
                    for row in list(earnings_items) + list(refreshed_missing):
                        if not isinstance(row, dict):
                            continue
                        rid = str(row.get("id") or f"{row.get('symbol','')}_{row.get('date','')}")
                        merged_by_id[rid] = row
                    earnings_items = list(merged_by_id.values())
                    earnings_index = self._build_earnings_index(earnings_items)
                    earnings_symbols = list(earnings_index.keys())
                    mismatch = detect_calendar_mismatch_symbols(
                        news_index,
                        earnings_index,
                        now=now,
                        min_hits=2,
                        recent_hours=lookback_hours,
                        lookahead_days=lookahead_days,
                    )

            mismatch = self._prune_mismatch_with_cached_history(mismatch, now=now, cfg=cfg)
            # News-based "already reported" detection: flag tickers whose earnings has
            # already happened per the headlines even when the calendar date is a wrong
            # future value (the date-based _filter_stale_items and the future-only
            # mismatch detector both miss this). Reads the news — the reason news are
            # aggregated. Computed after the calendar refresh so it sees the freshest
            # earnings_index. Additive: does not alter mismatch or its tests.
            already_reported = detect_already_reported_symbols(
                news_index,
                earnings_index,
                now=now,
                min_hits=2,
                recent_hours=lookback_hours,
                event_window_days=lookahead_days,
            )
            mismatch_details: Dict[str, Dict[str, Any]] = {}
            for sym, info in (mismatch or {}).items():
                if not isinstance(info, dict):
                    continue
                mismatch_details[sym] = {
                    "hits": int(_safe_int(info.get("hits"), 0)),
                    "recent_headlines": list(info.get("recent_headlines") or []),
                    "lookahead_days": int(_safe_int(info.get("lookahead_days"), lookahead_days)),
                    "lookback_hours": int(_safe_int(info.get("lookback_hours"), lookback_hours)),
                    "latest_news_utc": info.get("latest_news_utc"),
                    "reason": info.get("reason"),
                }

            with self._feedback_guard():
                feedback_dirty = self._evaluate_matured_feedback_predictions(cfg)
                # History sector backfill (owner requirement, 2026-08-04): a small
                # batch of UNKNOWN-sector history rows is resolved from the
                # verified estimates source every cycle so the grouped track-record
                # cleans up within a few cycles instead of staying 90% UNKNOWN.
                try:
                    self._backfill_history_sectors(cfg)
                except Exception as e:
                    logger.warning(f"Pre-earnings sector backfill failed: {e}")
            # Score archived Pulse picks against the realized post-earnings move
            # (mirrors the per-item feedback loop; only reported events, 36h
            # maturation, reuses the cached 4h-OHLC gap-move computation).
            with self._pulse_store_lock:
                pulse_eval_dirty = self._evaluate_matured_pulse_picks_locked(cfg)
                if pulse_eval_dirty:
                    self._persist_pulse_store_locked()
            with self._feedback_guard():
                weights, retune_dirty = self._get_active_gap_weights(cfg)
                feedback_dirty = feedback_dirty or retune_dirty or pulse_eval_dirty
                calibration_model = self._build_group_calibration_model(cfg)
                live_calibration = self._compute_live_calibration_metrics(cfg, calibration_model)
                try:
                    feedback_dirty = self._refresh_learning_model() or feedback_dirty
                except Exception as e:
                    logger.warning(f"Pre-earnings learning model refresh failed: {e}")

            candidate_symbols = list(
                dict.fromkeys(
                    [s for s in earnings_symbols if s]
                    + [s for s in mismatch.keys() if s]
                )
            )
            candidate_symbols = self._preselect_candidate_symbols(
                candidate_symbols=candidate_symbols,
                earnings_index=earnings_index,
                news_index=news_index,
                mismatch=mismatch,
                now=now,
                max_results=int(cfg.get("max_results", 200)),
            )
            active_calendar_symbols = {
                str(symbol or "").upper().strip()
                for symbol, rows in earnings_index.items()
                if (
                    (event_dt := _parse_iso(
                        (primary := self._pick_primary_event(rows or [])).get("date")
                        or primary.get("date_formatted")
                    )) is not None
                    and now.date() <= event_dt.date() <= now.date() + timedelta(days=lookahead_days)
                )
                and str(symbol or "").strip()
            }
            active_calendar_reporter_count = len(active_calendar_symbols)

            state_rows = self._load_state_rows_map()
            items: List[Dict[str, Any]] = []
            calendar_validation_rejections: List[Dict[str, Any]] = []
            for sym in candidate_symbols:
                news_rows = list(news_index.get(sym, []) or [])
                earnings_rows = list(earnings_index.get(sym, []) or [])
                primary_event = self._pick_primary_event(earnings_rows)
                primary_event_dt = _parse_iso(
                    (primary_event or {}).get("date") or (primary_event or {}).get("date_formatted")
                )
                schedule_conflict = self._detect_schedule_conflict(
                    symbol=sym,
                    event_dt=primary_event_dt,
                    event=primary_event,
                    news_rows=news_rows,
                    cfg=cfg,
                )
                if schedule_conflict:
                    calendar_validation_rejections.append({
                        "symbol": sym,
                        "earnings_time_utc": _iso(primary_event_dt),
                        "session": self._normalize_session(
                            (primary_event or {}).get("when")
                            or (primary_event or {}).get("session")
                        ),
                        "reason": str(schedule_conflict.get("reason") or "SCHEDULE_CONFLICT"),
                        "details": copy.deepcopy(schedule_conflict),
                    })
                    logger.warning(
                        "Pre-earnings suppressed %s due to schedule conflict: %s",
                        sym,
                        schedule_conflict,
                    )
                    continue
                built_item = self._build_item_for_symbol(
                    symbol=sym,
                    news_rows=news_rows,
                    earnings_rows=earnings_rows,
                    mismatch_info=mismatch.get(sym),
                    already_reported_info=already_reported.get(sym),
                    cfg=cfg,
                    state_row=state_rows.get(sym, {}),
                    as_of_utc=now_iso,
                    weight_overrides=weights,
                    calibration_model=calibration_model,
                    calibration_metrics=live_calibration,
                    previous_item=previous_items_by_symbol.get(
                        str(sym).upper().strip()
                    ),
                )
                self._carry_forward_ai_decision(
                    built_item,
                    previous_items_by_symbol.get(str(sym).upper().strip()),
                    cfg,
                )
                self._carry_forward_enrichment_state(
                    built_item,
                    previous_items_by_symbol.get(str(sym).upper().strip()),
                )
                items.append(built_item)

            items = [it for it in items if isinstance(it, dict)]
            # Session-aware reported filter (Task PE-premium C1): date-only
            # calendar rows parse as midnight UTC, so the strict datetime
            # filter used to drop a same-day BMO right after midnight. Now a
            # same-day BMO survives until 14:30 UTC (US open, DST-safe) and a
            # same-day AMC until 22:00 UTC. Dropped items are surfaced as
            # `reported_today` transparency records in the payload.
            items, reported_today = self._filter_reported_items(
                items, cfg, now, watchlist={str(s or "").upper() for s in watchlist_symbols},
            )
            # At a bounded cycle frontier, admit the nearest events first.
            # Conviction ranking happens only after admitted rows are scored.
            items.sort(
                key=lambda it: (
                    _parse_iso(it.get("earnings_time_utc"))
                    or datetime.max.replace(tzinfo=timezone.utc),
                    -_safe_int(it.get("gap_risk"), 0),
                    -abs(direction_bias_score_from_item(it)),
                    str(it.get("symbol") or ""),
                ),
            )
            items = items[: max(1, int(cfg["max_results"]))]

            items = self._merge_concurrent_item_updates(
                items,
                cycle_update_revision,
                cfg,
            )
            # Compute a preliminary evidence-weighted score so the limited LLM
            # quota is spent on likely Top-10 candidates, not simply the names
            # with the largest historical gaps.
            self._refresh_prediction_scores(items, cfg, now=now)
            ollama_used = self._apply_ollama(
                items,
                cfg,
                cycle_quota=ai_cycle_quota,
            )
            self._refresh_prediction_scores(items, cfg, now=now)
            try:
                self._refresh_pre_event_features(items, cfg, now=now)
            except Exception as e:
                logger.warning(f"Pre-earnings pre-event feature refresh failed: {e}")
            with self._feedback_guard():
                registration_dirty = self._register_open_predictions(items, cfg)
            feedback_dirty = feedback_dirty or registration_dirty
            # Aggregate Pre-Earnings Pulse AI (Task PE-premium C3): full cycle
            # only — NOT in the early AI pass — cloud-only, TTL-cached, non-fatal.
            pulse = self.get_pulse(cfg, items)
            with self._feedback_guard():
                live_calibration = self._compute_live_calibration_metrics(cfg, calibration_model)
            payload = self._build_payload(
                cfg=cfg,
                items=items,
                news_age=news_age,
                earnings_age=earnings_age,
                news_available=news_available,
                earnings_available=earnings_available,
                ollama_used=ollama_used,
                calibration=live_calibration,
                mismatch_details=mismatch_details,
                calendar_refresh_attempted_symbols=calendar_refresh_attempted_symbols,
                calendar_refresh_results=calendar_refresh_results,
                reported_today=reported_today,
                calendar_validation_rejections=calendar_validation_rejections,
                pulse=pulse,
            )
            self._attach_pre_earnings_decisions(cfg, items=payload.get("items") or [])

            with self._lock:
                payload["items"] = self._merge_concurrent_item_updates_locked(
                    list(payload.get("items", []) or []),
                    cycle_update_revision,
                    cfg,
                )
                # Coverage enrichment runs on its own worker. Preserve the
                # worker's latest progress instead of restoring the snapshot
                # captured when this (potentially long) scoring cycle started.
                current_status = dict(self._state.get("status", {}) or {})
                current_coverage_warm = dict(
                    current_status.get("coverage_warmup")
                    or coverage_warm
                    or self._default_coverage_warmup_status()
                )
                self._state = payload
                # Freshness starts when the atomic snapshot is complete, not
                # when a potentially long provider/AI cycle began. Otherwise
                # a successful seven-minute scan is born seven minutes old.
                completed_iso = _iso(_utc_now())
                self._state["status"]["last_success_time"] = completed_iso
                # Only a whole-universe build may set this (see
                # _latest_pre_earnings_snapshot_time).
                self._state["status"]["last_full_cycle_time"] = completed_iso
                self._state["status"]["cycle_running"] = False
                self._state["status"]["cycle_started_at"] = now_iso
                self._state["status"]["last_error"] = None
                self._state["status"]["next_run_in"] = int(cfg["refresh_seconds"])
                self._state["status"]["coverage_warmup"] = current_coverage_warm
                for key in (
                    "last_targeted_rescore_symbol",
                    "last_targeted_rescore_time",
                ):
                    if current_status.get(key) is not None:
                        self._state["status"][key] = current_status[key]
                self._attach_daily_ranking(
                    self._state,
                    cfg,
                    universe_event_count=active_calendar_reporter_count,
                    universe_symbols=active_calendar_symbols,
                    scored_count=len(items),
                    news_articles_scanned=len(news_items),
                )
                self._attach_direction_track_records(cfg)
                self._persist_locked()
                if feedback_dirty:
                    self._persist_feedback_store()
                return copy.deepcopy(self._state)
        except Exception as e:
            logger.error("PreEarningsAgent cycle error: %s", e, exc_info=True)
            with self._lock:
                status = dict(self._state.get("status", {}) or {})
                status["last_run_time"] = now_iso
                status["last_error"] = str(e)
                status["next_run_in"] = int(cfg["refresh_seconds"])
                status["cycle_running"] = False
                self._state["status"] = status
                self._state["disabled"] = False
                self._persist_locked()
                return copy.deepcopy(self._state)

    def _loop(self) -> None:
        startup_cfg = self._get_config()
        startup_delay = max(0, int(startup_cfg.get("startup_delay_seconds", 20)))
        if startup_delay > 0:
            self._wake_event.wait(timeout=startup_delay)
            self._wake_event.clear()
            if self._stop_event.is_set():
                return

        # A failed due cycle must still observe the normal interval before a
        # retry. Otherwise an old last_success timestamp would cause a tight
        # provider loop. Explicit requests bypass this local retry boundary.
        retry_not_before = 0.0
        while not self._stop_event.is_set():
            self.config_manager.reload_if_changed()
            cfg = self._get_config()
            interval = max(30, int(cfg["refresh_seconds"]))

            if self._is_enabled():
                with self._lock:
                    explicit_refresh = bool(self._pending_refresh)
                scheduled_wait = self._seconds_until_scheduled_refresh(cfg)
                retry_wait = max(0.0, retry_not_before - time.monotonic())
                wait_sec = 0.0 if explicit_refresh else max(scheduled_wait, retry_wait)
                if wait_sec > 0:
                    self._wake_event.wait(timeout=wait_sec)
                    self._wake_event.clear()
                    if self._stop_event.is_set():
                        return
                    # Re-read persisted timestamps, configuration, and the
                    # explicit flag after every wake or scheduled timeout.
                    continue

                started = time.monotonic()
                with self._lock:
                    self._pending_refresh = False
                with track_agent_run(
                    "pre_earnings",
                    external_call_budget=int(cfg["external_call_budget"]),
                ):
                    self.run_cycle()
                try:
                    self._queue_watchlist_priority_warmup()
                except Exception:
                    logger.warning(
                        "Pre-Earnings watchlist priority warmup failed (non-fatal)",
                        exc_info=True,
                    )
                elapsed = max(0.0, time.monotonic() - started)
                retry_not_before = time.monotonic() + max(1.0, interval - elapsed)
            else:
                with self._lock:
                    status = dict(self._state.get("status", {}) or {})
                    status["next_run_in"] = None
                    self._state["status"] = status
                    self._state["disabled"] = True
                self._wake_event.wait(timeout=float(interval))
                self._wake_event.clear()

    def _get_watchlist_symbols(self) -> List[str]:
        symbols: List[str] = []
        try:
            wl = self.config_manager.watchlist
            if isinstance(wl, dict):
                for key in ("order", "yahoo", "stocks", "binance", "crypto", "cryptos"):
                    symbols.extend(wl.get(key, []) or [])
        except Exception:
            pass
        out: List[str] = []
        seen = set()
        for raw in symbols:
            sym = str(raw or "").upper().strip()
            if not sym or sym in seen:
                continue
            if not _is_earnings_eligible_symbol(sym):
                continue
            seen.add(sym)
            out.append(sym)
        return out

    def _watchlist_symbol_set(self) -> set:
        try:
            return set(self._get_watchlist_symbols())
        except Exception:
            return set()

    def _queue_watchlist_priority_warmup(self, limit: int = 5) -> Dict[str, Any]:
        """Enrich the owner's watchlist reporters first, nearest event first.

        A full cycle spends its provider budget on calendar verification, so a
        freshly built row has no EPS/price history, analyst consensus or
        liquidity context until a coverage warmup reaches it. Warmups used to
        be started only by the two toolbar buttons or by the optional
        continuous scanner, both of which walk the slate alphabetically inside
        an earnings day — a watched name reporting tomorrow could wait hours.
        Bounded: at most ``limit`` symbols, and each attempt puts the still
        missing fields on their normal retry cooldown.
        """
        watchlist = self._watchlist_symbol_set()
        if not watchlist:
            return {"status": "no_watchlist"}
        with self._lock:
            items = [
                copy.deepcopy(item)
                for item in (self._state.get("items") or [])
                if isinstance(item, dict)
                and str(item.get("symbol") or "").upper().strip() in watchlist
            ]
        due = [
            item for item in items
            if self._is_low_coverage_item(item)
            and "event_already_passed" not in self._event_gate_blockers(item)
        ]
        due.sort(
            key=lambda item: (
                _parse_iso(item.get("earnings_time_utc"))
                or datetime.max.replace(tzinfo=timezone.utc),
                str(item.get("symbol") or ""),
            )
        )
        symbols = [
            str(item.get("symbol") or "").upper().strip() for item in due
        ][: max(1, int(limit))]
        if not symbols:
            return {"status": "no_targets"}
        return self.request_coverage_warmup(
            symbols=symbols,
            batch_size=min(5, len(symbols)),
            only_needs_coverage=True,
            allow_remote_on_miss=True,
        )

    def _load_prev_market_caps(self) -> Dict[str, float]:
        """Load the last persisted market_cap per symbol from the pre-earnings output
        (Faza 3, 2026-07-07). Used as a sticky fallback when the fresh per-symbol fetch
        is throttled by the live server's concurrent agent fleet — market cap is near-
        # constant over a refresh interval, so reusing the last good value keeps the
        filter populated instead of wiping to null on every contended cycle.
        """
        out: Dict[str, float] = {}
        try:
            path = Path(PRE_EARNINGS_OUTPUT_PATH)
            if not path.exists():
                return out
            with path.open("r", encoding="utf-8") as f:
                payload = json.load(f)
            for it in (payload.get("items") or []):
                if not isinstance(it, dict):
                    continue
                sym = str(it.get("symbol") or "").upper().strip()
                mc = _safe_float(it.get("market_cap"), None)
                if sym and mc is not None:
                    out[sym] = mc
        except Exception:
            pass
        return out

    def _trigger_low_coverage_news_refresh(
        self,
        news_agent: Any,
        symbols: List[str],
        cfg: Dict[str, Any],
        *,
        force_refresh: bool = False,
    ) -> Dict[str, Any]:
        """Merge overlapping low-coverage requests into one draining worker.

        The news agent's global refresh singleflight intentionally collapses a
        second global run.  Using its per-ticker cache guard here avoids losing
        a targeted set behind an unrelated market refresh, while this local
        pending queue guarantees symbols arriving during a drain are processed
        by the same worker.
        """
        requested = [
            str(symbol or "").upper().strip()
            for symbol in (symbols or [])
            if _is_earnings_eligible_symbol(str(symbol or "").upper().strip())
        ]
        requested = list(dict.fromkeys(requested))
        if not requested:
            return {"status": "no_targets", "event": None}

        lock = getattr(self, "_low_coverage_news_lock", None)
        if lock is None:
            lock = self._low_coverage_news_lock = threading.RLock()
        idle = getattr(self, "_low_coverage_news_idle", None)
        if idle is None:
            idle = self._low_coverage_news_idle = threading.Event()
            idle.set()

        with lock:
            pending = list(getattr(self, "_low_coverage_news_pending", []) or [])
            self._low_coverage_news_pending = list(
                dict.fromkeys(pending + requested)
            )
            idle.clear()
            active = getattr(self, "_low_coverage_news_thread", None)
            if active is not None and (
                active.is_alive() or active.ident is None
            ):
                return {
                    "status": "queued",
                    "queued_symbols": list(self._low_coverage_news_pending),
                    "event": idle,
                }

        def _drain_impl() -> None:
            last_error = None
            while True:
                with lock:
                    pending_now = list(
                        getattr(self, "_low_coverage_news_pending", []) or []
                    )
                    if not pending_now:
                        self._low_coverage_news_thread = None
                        self._low_coverage_news_last_error = last_error
                        idle.set()
                        return
                    batch = pending_now[:5]
                    self._low_coverage_news_pending = pending_now[5:]
                for sym in batch:
                    try:
                        if hasattr(news_agent, "ensure_ticker_news_cached"):
                            news_agent.ensure_ticker_news_cached(
                                sym,
                                force_refresh=bool(force_refresh),
                                allow_remote=True,
                            )
                        elif hasattr(news_agent, "refresh_single_ticker_news"):
                            news_agent.refresh_single_ticker_news(sym)
                        else:
                            news_agent.refresh_all_news(
                                [sym],
                                include_market=False,
                            )
                    except Exception as exc:
                        last_error = str(exc)

        def _drain_entry() -> None:
            ctx = get_request_context()
            if ctx is not None and getattr(ctx, "external_budget", None) is not None:
                _drain_impl()
                return
            with track_agent_run(
                "pre_earnings",
                external_call_budget=int(cfg.get("external_call_budget", 30)),
            ):
                _drain_impl()

        caller_context = contextvars.copy_context()
        worker = threading.Thread(
            target=lambda: caller_context.run(_drain_entry),
            daemon=True,
            name="PreEarningsNewsRefresh",
        )
        with lock:
            self._low_coverage_news_last_error = None
            self._low_coverage_news_thread = worker
        worker.start()
        return {
            "status": "accepted",
            "queued_symbols": requested,
            "event": idle,
        }

    def _load_news_items(
        self,
        cfg: Dict[str, Any],
        symbols_scope: List[str],
        priority_symbols: Optional[List[str]] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[int], bool]:
        cache_ttl = int(cfg.get("cache_ttl_seconds", 600))
        lookback_hours = max(1, int(cfg["lookback_hours"]))
        cutoff = _utc_now() - timedelta(hours=lookback_hours)

        agent = get_news_tab_agent_if_ready()
        if agent is None:
            try:
                # Fallback to cached singleton bootstrap (loads backup/cache, no forced remote refresh).
                agent = get_news_tab_agent(self.config_manager.config)
            except Exception:
                return [], None, False

        def _read_cached_news() -> Tuple[List[Dict[str, Any]], Optional[int]]:
            age_local = None
            try:
                state = agent.get_news_tab_state(active_tickers=None) or {}
                last_update = _parse_iso(state.get("last_update"))
                age_local = int(max(0.0, (_utc_now() - last_update).total_seconds())) if last_update else None
            except Exception:
                age_local = None

            try:
                rows = agent.get_all_articles_flat(active_tickers=None) or []
            except Exception:
                rows = []

            filtered: List[Dict[str, Any]] = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                published = _parse_iso(row.get("published_at") or row.get("published_utc"))
                if published is None or published < cutoff:
                    continue
                filtered.append(row)
            return filtered, age_local

        out, age = _read_cached_news()
        stale = (age is None) or (age > cache_ttl)

        coverage = self._build_news_index(out, symbols_scope)
        missing_symbols = [
            str(s or "").upper().strip()
            for s in (symbols_scope or [])
            if str(s or "").strip() and not (coverage.get(str(s or "").upper().strip()) or [])
        ]

        # Cache-first policy with two complementary lanes:
        #   1. repeatedly deep-refresh the strongest/imminent candidates;
        #   2. rotate through the rest so a large calendar cannot starve.
        # A fresh *global* news timestamp is not proof that every ticker bucket
        # is fresh, so priority names are eligible after their own TTL even when
        # they already have one cached article.
        if self._is_enabled():
            now_ts = time.time()
            deep_ttl = max(300, _safe_int(cfg.get("news_deep_refresh_seconds"), 900))
            refresh_limit = max(
                1,
                min(25, _safe_int(cfg.get("news_deep_refresh_batch_size"), 10)),
            )
            priority_limit = max(
                1,
                min(refresh_limit, _safe_int(cfg.get("news_priority_refresh_size"), 6)),
            )
            deep_meta = getattr(self, "_deep_news_refresh_at", None)
            if not isinstance(deep_meta, dict):
                deep_meta = self._deep_news_refresh_at = {}

            priority_clean = list(
                dict.fromkeys(
                    str(s or "").upper().strip()
                    for s in (priority_symbols or [])
                    if str(s or "").strip()
                )
            )
            due_priority = [
                sym
                for sym in priority_clean
                if (now_ts - float(deep_meta.get(sym, 0.0) or 0.0)) >= deep_ttl
            ][:priority_limit]

            rotation_pool = list(
                dict.fromkeys(missing_symbols + list(symbols_scope or []))
            )
            due_rotation = [
                sym
                for sym in rotation_pool
                if sym not in due_priority
                and (
                    sym in missing_symbols
                    or stale
                    or (now_ts - float(deep_meta.get(sym, 0.0) or 0.0)) >= deep_ttl
                )
            ]
            rotation_slots = max(0, refresh_limit - len(due_priority))
            if due_rotation and rotation_slots:
                cursor = int(self._news_refresh_cursor % len(due_rotation))
                rotated = list(due_rotation[cursor:]) + list(due_rotation[:cursor])
                rotation_batch = rotated[:rotation_slots]
                self._news_refresh_cursor = (cursor + len(rotation_batch)) % len(due_rotation)
            else:
                rotation_batch = []
            refresh_symbols = list(dict.fromkeys(due_priority + rotation_batch))

            if refresh_symbols:
                for sym in refresh_symbols:
                    deep_meta[sym] = now_ts
                with self._lock:
                    status = dict(self._state.get("status", {}) or {})
                    status["news_refresh_cursor"] = int(self._news_refresh_cursor)
                    status["news_refresh_batch"] = list(refresh_symbols)
                    status["news_priority_refresh_batch"] = list(due_priority)
                    self._state["status"] = status

            # The worker is intentionally bounded and may finish after this
            # cycle. The next cycle consumes its persisted results; a 10-second
            # wait keeps the current snapshot responsive.
            if refresh_symbols:
                refresh_job = self._trigger_low_coverage_news_refresh(
                    agent,
                    refresh_symbols,
                    cfg,
                    force_refresh=True,
                )
                done = refresh_job.get("event")
                finished = bool(
                    done is not None
                    and hasattr(done, "wait")
                    and done.wait(timeout=10.0)
                )
                if not finished:
                    logger.info(
                        "Pre-earnings deep news refresh continues in background; serving current cache."
                    )
                elif getattr(self, "_low_coverage_news_last_error", None):
                    logger.warning(
                        "Pre-earnings news refresh skipped: %s",
                        self._low_coverage_news_last_error,
                    )
                out, age = _read_cached_news()

        return out, age, bool(out)

    def _load_earnings(
        self,
        cfg: Dict[str, Any],
    ) -> Tuple[List[Dict[str, Any]], Optional[int], bool]:
        cache_ttl = int(cfg["cache_ttl_seconds"])
        lookahead_days = max(1, int(cfg["lookahead_days"]))

        calendar = get_economic_calendar_agent(self.config_manager.config)
        today = _utc_now().date()
        start = datetime.combine(today - timedelta(days=1), datetime.min.time())
        end = datetime.combine(today + timedelta(days=lookahead_days), datetime.max.time())

        cached = calendar.get_calendar_events_range(
            start,
            end,
            watchlist_tickers=None,
            allow_remote=False,
        ) or {}
        earnings = [e for e in (cached.get("earnings", []) or []) if isinstance(e, dict)]
        cached_symbols = [str((row or {}).get("symbol") or "").upper().strip() for row in earnings if isinstance(row, dict)]
        age = self._earnings_cache_age_seconds(calendar, cached_symbols)
        stale = (age is None) or (age > cache_ttl)
        needs_initial_remote = not self._did_initial_remote_earnings_refresh

        if (
            (not earnings or stale or needs_initial_remote)
            and self._is_enabled()
            and self._reserve_external_call("economic_calendar")
        ):
            try:
                refreshed = calendar.get_calendar_events_range(
                    start,
                    end,
                    watchlist_tickers=None,
                    allow_remote=True,
                ) or {}
                earnings = [e for e in (refreshed.get("earnings", []) or []) if isinstance(e, dict)]
                refreshed_symbols = [str((row or {}).get("symbol") or "").upper().strip() for row in earnings if isinstance(row, dict)]
                age = self._earnings_cache_age_seconds(calendar, refreshed_symbols)
                if earnings:
                    self._did_initial_remote_earnings_refresh = True
            except Exception as e:
                logger.warning(f"Pre-earnings calendar refresh skipped: {e}")
        return earnings, age, bool(earnings)

    def _refresh_earnings_for_symbols(
        self,
        cfg: Dict[str, Any],
        symbols: List[str],
        force_yfinance: bool = False,
    ) -> List[Dict[str, Any]]:
        target = [str(s or "").upper().strip() for s in (symbols or []) if str(s or "").strip()]
        target = list(dict.fromkeys(target))[:150]
        if not target:
            return []
        try:
            lookahead_days = max(1, int(cfg["lookahead_days"]))
            calendar = get_economic_calendar_agent(self.config_manager.config)
            today = _utc_now().date()
            start = datetime.combine(today - timedelta(days=1), datetime.min.time())
            end = datetime.combine(today + timedelta(days=lookahead_days + 2), datetime.max.time())
            allow_calendar_remote = self._reserve_external_call(
                "economic_calendar"
            )
            refreshed = calendar.get_calendar_events_range(
                start,
                end,
                watchlist_tickers=target,
                allow_remote=allow_calendar_remote,
                include_all_reporters=not bool(force_yfinance),
            ) or {}
            rows = [e for e in (refreshed.get("earnings", []) or []) if isinstance(e, dict)]
            # The economic-calendar agent's native earnings source is sparse (no API key),
            # so resolve the rest via yfinance (cached ~6h). This is what actually lets the
            # broad large-cap universe surface upcoming reporters (e.g. JPM, banks).
            have = {str(r.get("symbol", "")).upper().strip() for r in rows}
            missing = [s for s in target if s not in have]
            yf_targets = target if force_yfinance else missing
            yf_rows = self._resolve_yf_earnings_rows(
                yf_targets[:30],
                lookahead_days,
                force_refresh=bool(force_yfinance),
            )
            if yf_rows:
                yf_rows = self._cross_check_yfinance_sessions(yf_rows, rows)
            if force_yfinance and yf_rows:
                canonical_symbols = {
                    str(row.get("symbol") or "").upper().strip()
                    for row in yf_rows
                    if isinstance(row, dict)
                }
                today = _utc_now().date()
                rows = [
                    row for row in rows
                    if (
                        str(row.get("symbol") or "").upper().strip() not in canonical_symbols
                        or (
                            (_parse_iso(row.get("date") or row.get("date_formatted")) is not None)
                            and _parse_iso(row.get("date") or row.get("date_formatted")).date() < today
                        )
                    )
                ]
                rows.extend(yf_rows)
                if hasattr(calendar, "replace_future_earnings_for_ticker"):
                    for symbol_u in canonical_symbols:
                        calendar.replace_future_earnings_for_ticker(
                            symbol_u,
                            [
                                row for row in yf_rows
                                if str(row.get("symbol") or "").upper().strip() == symbol_u
                            ],
                        )
            elif yf_rows:
                rows.extend(yf_rows)
            return rows
        except Exception as e:
            logger.warning(f"Pre-earnings targeted earnings refresh skipped: {e}")
            return []

    @classmethod
    def _cross_check_yfinance_sessions(
        cls,
        yfinance_rows: List[Dict[str, Any]],
        calendar_rows: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Attach BMO/AMC only when Yahoo's date agrees with calendar data.

        Yahoo's calendar is useful for the day but does not consistently expose
        the release session. A same-symbol, same-day calendar match supplies the
        session without inventing it. The composite provenance also tells the
        canonicalizer that two provider paths agreed on the date.
        """
        sessions: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for raw in calendar_rows or []:
            if not isinstance(raw, dict):
                continue
            symbol = str(raw.get("symbol") or "").upper().strip()
            dt = _parse_iso(raw.get("date") or raw.get("date_formatted"))
            session = cls._normalize_session(raw.get("session") or raw.get("when"))
            if not symbol or dt is None or session not in {"BMO", "AMC"}:
                continue
            key = (symbol, dt.date().isoformat())
            previous = sessions.get(key)
            if previous is None or cls._event_source_priority(raw) >= cls._event_source_priority(previous):
                sessions[key] = raw

        checked: List[Dict[str, Any]] = []
        for raw in yfinance_rows or []:
            if not isinstance(raw, dict):
                continue
            row = dict(raw)
            symbol = str(row.get("symbol") or "").upper().strip()
            dt = _parse_iso(row.get("date") or row.get("date_formatted"))
            match = sessions.get((symbol, dt.date().isoformat())) if symbol and dt else None
            if match is not None:
                session = cls._normalize_session(match.get("session") or match.get("when"))
                row["session"] = session
                row["when"] = session.lower()
                row["session_confidence"] = "cross_provider"
                row["source"] = "yfinance+calendar"
                row["source_url"] = row.get("source_url") or match.get("source_url")
                row["confirmed"] = True
            checked.append(row)
        return checked

    def _resolve_yf_earnings_rows(
        self,
        symbols: List[str],
        lookahead_days: int,
        force_refresh: bool = False,
    ) -> List[Dict[str, Any]]:
        """Resolve next earnings date per symbol via yfinance (cached ~6h); return
        calendar-style rows for those reporting within the lookahead window."""
        import time as _t
        rows: List[Dict[str, Any]] = []
        if not hasattr(self, "_yf_earn_cache"):
            self._yf_earn_cache: Dict[str, Tuple[Any, float]] = {}
        try:
            import yfinance as yf
        except Exception:
            return rows
        now = _utc_now()
        start_d = (now - timedelta(days=1)).date()
        end_d = (now + timedelta(days=int(lookahead_days) + 2)).date()
        for sym in symbols:
            try:
                cached_entry = self._yf_earn_cache.get(sym)
                if not force_refresh and cached_entry is not None and (_t.time() - cached_entry[1]) < 21600:
                    val = cached_entry[0]
                    if isinstance(val, (tuple, list)) and len(val) >= 3:
                        edate, session, session_confidence = val[0], val[1], val[2]
                    else:
                        edate, session, session_confidence = val, "UNKNOWN", "unknown"
                else:
                    if not self._reserve_external_call("yfinance_calendar"):
                        continue
                    edate = None
                    session = "UNKNOWN"
                    session_confidence = "unknown"
                    clean_sym = StockDataClient._clean_symbol(sym)
                    ticker_obj = yf.Ticker(clean_sym)
                    df = None
                    try:
                        df = ticker_obj.get_earnings_dates(limit=8)
                        if df is not None and len(df):
                            for idx, _raw in df.iterrows():
                                try:
                                    local_dt = idx.to_pydatetime()
                                except Exception:
                                    continue
                                dt_utc = local_dt if local_dt.tzinfo else local_dt.replace(tzinfo=timezone.utc)
                                dt_utc = dt_utc.astimezone(timezone.utc)
                                if dt_utc.date() >= start_d:
                                    edate = dt_utc.date()
                                    if local_dt.hour != 0:
                                        session = "AMC" if local_dt.hour >= 12 else "BMO"
                                        session_confidence = "provider"
                                    break
                    except Exception:
                        df = None

                    if edate is None:
                        try:
                            cal = ticker_obj.calendar
                            ed = cal.get("Earnings Date") if isinstance(cal, dict) else None
                            if isinstance(ed, (list, tuple)) and ed:
                                edate = ed[0]
                            elif ed:
                                edate = ed
                        except Exception:
                            edate = None

                    if edate is not None and session == "UNKNOWN":
                        try:
                            past_sessions: List[str] = []
                            if df is not None and len(df):
                                for idx, _raw in df.iterrows():
                                    try:
                                        pdt = idx.to_pydatetime()
                                        if pdt.hour != 0:
                                            past_sessions.append("AMC" if pdt.hour >= 12 else "BMO")
                                    except Exception:
                                        continue
                            if len(past_sessions) >= 3:
                                amc_count = past_sessions.count("AMC")
                                bmo_count = past_sessions.count("BMO")
                                # Inferred from the company's past reporting
                                # habit, NOT stated by the provider for this
                                # event: label it so. It was stamped "provider"
                                # (and confirmed=True, date_confidence=high),
                                # which passed the verified-session gate.
                                if amc_count / len(past_sessions) >= 0.75:
                                    session = "AMC"
                                    session_confidence = "inferred"
                                elif bmo_count / len(past_sessions) >= 0.75:
                                    session = "BMO"
                                    session_confidence = "inferred"
                        except Exception:
                            pass

                    # Do not turn a transient empty/rate-limit response into a
                    # six-hour negative cache. Preserve only verified dates.
                    if edate is not None:
                        self._yf_earn_cache[sym] = ((edate, session, session_confidence), _t.time())
                if isinstance(edate, datetime):
                    edate = edate.date()
                if edate is None or not (start_d <= edate <= end_d):
                    continue
                iso = datetime(edate.year, edate.month, edate.day, 0, 0, tzinfo=timezone.utc).isoformat()
                rows.append({
                    "id": f"earn_{sym}_{edate.strftime('%Y%m%d')}",
                    "symbol": sym,
                    "date": iso,
                    "date_formatted": edate.isoformat(),
                    "session": session,
                    "when": session.lower(),
                    "category": "earnings",
                    "source": "yfinance",
                    "source_url": f"https://finance.yahoo.com/quote/{sym}/analysis/",
                    "confirmed": session_confidence in {"provider", "cross_provider"},
                    "verified_at": _iso(now),
                    "date_confidence": "high" if session_confidence == "provider" else "medium",
                    "session_confidence": session_confidence,
                })
            except Exception:
                continue
        return rows

    def _earnings_cache_age_seconds(self, calendar: Any, symbols: List[str]) -> Optional[int]:
        now_ts = time.time()
        try:
            lock = getattr(calendar, "_cache_lock", None)
            if lock is not None:
                with lock:
                    events_cache = getattr(calendar, "_events_cache", {}) or {}
                    meta = dict(events_cache.get("earnings_meta", {}) or {})
                    cache_path = Path(getattr(calendar, "_cache_path", "calendar_events_cache.json"))
            else:
                events_cache = getattr(calendar, "_events_cache", {}) or {}
                meta = dict(events_cache.get("earnings_meta", {}) or {})
                cache_path = Path(getattr(calendar, "_cache_path", "calendar_events_cache.json"))

            vals = []
            for sym in symbols:
                ts = _safe_float(meta.get(sym.upper()), None)
                if ts:
                    vals.append(ts)
            if vals:
                return int(max(0.0, now_ts - max(vals)))
            if cache_path.exists():
                return int(max(0.0, now_ts - cache_path.stat().st_mtime))
        except Exception:
            pass
        return None

    def _build_news_index(self, news_items: List[Dict[str, Any]], symbols_scope: List[str]) -> Dict[str, List[Dict[str, Any]]]:
        symbols = list(dict.fromkeys([str(s or "").upper().strip() for s in symbols_scope if str(s or "").strip()]))
        symbol_set = set(symbols)
        symbol_token_re = re.compile(r"\b[A-Z]{1,6}(?:\.[A-Z])?\b")
        by_symbol: Dict[str, List[Dict[str, Any]]] = {s: [] for s in symbols}

        company_names: Dict[str, str] = {}
        try:
            news_agent = get_news_tab_agent_if_ready()
            raw_names = getattr(news_agent, "_company_names_cache", {}) if news_agent else {}
            company_names = {
                str(key or "").upper().strip(): str(value or "").strip()
                for key, value in (raw_names or {}).items()
                if str(key or "").strip() and str(value or "").strip()
            }
        except Exception:
            company_names = {}

        def _relevance(row: Dict[str, Any], symbol: str) -> float:
            text = _headline_short_blob(row)
            # Bare US symbols can collide with foreign listings (Jabil JBL vs
            # Jubilee JSE:JBL). The exchange prefix is part of issuer identity.
            foreign = re.search(rf"\b(?:JSE|LSE|AIM|ASX|TSX|TSXV|BSE|NSE|HKEX)\s*:\s*{re.escape(symbol)}\b", text, re.I)
            domestic = re.search(rf"\b(?:NYSE|NASDAQ|AMEX|NYSEARCA)\s*:\s*{re.escape(symbol)}\b", text, re.I)
            name_words = re.findall(r"[a-z0-9]+", company_names.get(symbol, "").lower())
            named_issuer = bool(name_words and len(name_words[0]) >= 4 and re.search(rf"\b{re.escape(name_words[0])}\b", text, re.I))
            if foreign and not domestic and not named_issuer:
                return 0.0
            if _has_strict_symbol_mention(text, symbol):
                return 1.0
            explicit = _explicit_tickers(text)
            if explicit and symbol not in explicit:
                return 0.0
            company_name = company_names.get(symbol, "")
            if company_name:
                normalized_text = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
                normalized_name = re.sub(r"[^a-z0-9]+", " ", company_name.lower()).strip()
                first_word = normalized_name.split()[0] if normalized_name else ""
                if normalized_name and normalized_name in normalized_text:
                    return 1.0
                if len(first_word) >= 4 and re.search(rf"\b{re.escape(first_word)}\b", normalized_text):
                    return 0.9
            # A provider ticker bucket is not issuer identification (symbols
            # collide across exchanges). Unverified rows cannot become evidence.
            return 0.0

        def _append(symbol: str, row: Dict[str, Any], relevance: float) -> None:
            if relevance <= 0.0:
                return
            enriched = dict(row)
            enriched["_pe_relevance"] = round(float(relevance), 3)
            enriched["_pe_publisher"] = _news_publisher(enriched)
            by_symbol.setdefault(symbol, []).append(enriched)

        for row in news_items:
            if not isinstance(row, dict):
                continue
            # Pre-Earnings is an equity-event model. A crypto article may use
            # the same short token as a stock (BB = BlackBerry and BounceBit),
            # so crypto-classified rows are never stock earnings evidence.
            if _is_crypto_only_equity_news(row):
                continue
            direct_symbols = self._extract_news_symbols(row)
            if direct_symbols:
                for symbol in direct_symbols:
                    _append(symbol, row, _relevance(row, symbol))
                continue

            text = _headline_blob(row)
            if not text:
                continue
            # Fast pre-filter on uppercased tokens, then CONFIRM each candidate with a
            # strict, case-sensitive mention ($SYM, (SYM), or uppercase SYM). Without this,
            # common-word tickers (HELP, ON, ALL, KEY, CAR) match the lowercase English word:
            # "help raise my niece" was being tagged as $HELP news, drowning the real signal.
            candidates = set(symbol_token_re.findall(str(text).upper())) & symbol_set
            if not candidates:
                continue
            for symbol in candidates:
                if _has_strict_symbol_mention(text, symbol):
                    _append(symbol, row, 1.0)

        for symbol, rows in by_symbol.items():
            rows.sort(
                key=lambda r: _parse_iso(r.get("published_at") or r.get("published_utc"))
                or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )
        return by_symbol

    def _prune_mismatch_with_cached_history(
        self,
        mismatch: Dict[str, Dict[str, Any]],
        now: datetime,
        cfg: Dict[str, Any],
    ) -> Dict[str, Dict[str, Any]]:
        if not isinstance(mismatch, dict) or not mismatch:
            return {}
        keep: Dict[str, Dict[str, Any]] = {}
        past_grace_days = max(1, _safe_int(cfg.get("mismatch_past_grace_days"), 3))
        lookahead_days = max(1, _safe_int(cfg.get("lookahead_days"), 10))
        lo = now - timedelta(days=past_grace_days)
        hi = now + timedelta(days=lookahead_days + 2)

        for symbol, info in mismatch.items():
            sym = str(symbol or "").upper().strip()
            if not sym:
                continue
            has_recent_cached_event = False
            for row in self._get_cached_earnings_history(sym, max_events=16):
                dt = _parse_iso(row.get("date") or row.get("date_formatted"))
                if dt is None:
                    continue
                if lo <= dt <= hi:
                    has_recent_cached_event = True
                    break
            if not has_recent_cached_event:
                keep[sym] = info
        return keep

    @staticmethod
    def _extract_news_symbols(row: Dict[str, Any]) -> List[str]:
        return _extract_news_row_symbols(row)

    @staticmethod
    def _event_source_priority(row: Dict[str, Any]) -> int:
        source = str((row or {}).get("source") or "").strip().lower()
        url = str((row or {}).get("source_url") or "").strip().lower()
        if any(token in source for token in ("investor relations", "official_ir", "company_ir")):
            return 100
        if "sec.gov" in url or source in {"sec", "sec_filing"}:
            return 95
        if "yfinance" in source:
            if "calendar" in source:
                return 90
            return 80
        if "finnhub" in source:
            return 70
        if "nasdaq" in source:
            # Nasdaq/Zacks publishes a broad estimated slate derived from
            # historical reporting patterns. It is excellent for discovery,
            # but remains below verified provider/issuer dates for gating.
            return 50
        if bool((row or {}).get("confirmed")):
            return 60
        return 20

    @classmethod
    def _canonicalize_symbol_events(
        cls,
        symbol: str,
        rows: List[Dict[str, Any]],
        *,
        now: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """Normalize provenance and expose conflicting same-quarter dates.

        We deliberately do not guess between two low-confidence future dates.
        Both remain auditable, while the selected row is blocked from trader
        recommendations through ``event_status=DATE_CONFLICT``.
        """
        current = now or _utc_now()
        by_day: Dict[str, Dict[str, Any]] = {}
        undated: List[Dict[str, Any]] = []
        for raw in rows or []:
            if not isinstance(raw, dict):
                continue
            row = dict(raw)
            row["symbol"] = str(symbol or row.get("symbol") or "").upper().strip()
            dt = _parse_iso(row.get("date") or row.get("date_formatted"))
            session = cls._normalize_session(row.get("when") or row.get("session"))
            row["session"] = session
            row["when"] = session.lower() if session in {"BMO", "AMC"} else "unknown"
            row["source"] = str(row.get("source") or "calendar_cache_unknown")
            row["source_priority"] = cls._event_source_priority(row)
            row["session_confidence"] = str(
                row.get("session_confidence")
                or ("provider" if session in {"BMO", "AMC"} else "unknown")
            )
            has_actuals = (
                _safe_float(row.get("eps_actual"), None) is not None
                or _safe_float(row.get("epsActual"), None) is not None
                or _safe_float(row.get("revenue_actual"), None) is not None
                or _safe_float(row.get("revenueActual"), None) is not None
            )
            if has_actuals or (dt is not None and dt.date() < current.date()):
                row["event_status"] = "REPORTED"
            else:
                row["event_status"] = "UPCOMING"
            row["date_confidence"] = (
                "high" if row["source_priority"] >= 90
                else ("medium" if row["source_priority"] >= 70 else "low")
            )
            if dt is None:
                row["event_status"] = "UNVERIFIED"
                undated.append(row)
                continue
            day_key = dt.date().isoformat()
            previous = by_day.get(day_key)
            if previous is None or (
                row["source_priority"], bool(row.get("confirmed")), len(row)
            ) > (
                previous.get("source_priority", 0), bool(previous.get("confirmed")), len(previous)
            ):
                by_day[day_key] = row

        dated = list(by_day.values())
        active = [
            row for row in dated
            if row.get("event_status") == "UPCOMING"
            and (_parse_iso(row.get("date") or row.get("date_formatted")) is not None)
            and (_parse_iso(row.get("date") or row.get("date_formatted")).date() <= current.date() + timedelta(days=45))
        ]
        active_days = sorted({
            _parse_iso(row.get("date") or row.get("date_formatted")).date().isoformat()
            for row in active
        })
        if len(active_days) > 1:
            ranked = sorted(
                active,
                key=lambda row: (row.get("source_priority", 0), bool(row.get("confirmed"))),
                reverse=True,
            )
            best_priority = int(ranked[0].get("source_priority", 0))
            second_priority = int(ranked[1].get("source_priority", 0))
            authoritative = ranked[0] if best_priority >= 90 and best_priority - second_priority >= 15 else None
            for row in active:
                if authoritative is not None and row is not authoritative:
                    row["event_status"] = "SUPERSEDED"
                elif authoritative is None:
                    row["event_status"] = "DATE_CONFLICT"
                row["conflicting_dates"] = active_days

        dated.sort(
            key=lambda row: _parse_iso(row.get("date") or row.get("date_formatted"))
            or datetime.max.replace(tzinfo=timezone.utc)
        )
        return dated + undated

    def _build_earnings_index(self, earnings_items: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
        out: Dict[str, List[Dict[str, Any]]] = {}
        for row in earnings_items:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol") or "").upper().strip()
            if not symbol:
                continue
            if not _is_earnings_eligible_symbol(symbol):
                continue
            out.setdefault(symbol, []).append(row)
        for symbol, rows in list(out.items()):
            out[symbol] = self._canonicalize_symbol_events(symbol, rows)
        return out

    @staticmethod
    def _mentions_symbol(text: str, symbol: str) -> bool:
        return _has_strict_symbol_mention(text, symbol)

    def _preselect_candidate_symbols(
        self,
        candidate_symbols: List[str],
        earnings_index: Dict[str, List[Dict[str, Any]]],
        news_index: Dict[str, List[Dict[str, Any]]],
        mismatch: Dict[str, Dict[str, Any]],
        now: datetime,
        max_results: int,
    ) -> List[str]:
        clean = [str(s or "").upper().strip() for s in candidate_symbols if str(s or "").strip()]
        if not clean:
            return []
        limit = max(300, max(1, int(max_results)) * 2)
        def _rank(sym: str) -> Tuple[int, float, float, float, str]:
            event_rows = earnings_index.get(sym, []) or []
            event = self._pick_primary_event(event_rows, now=now)
            event_dt = _parse_iso((event or {}).get("date") or (event or {}).get("date_formatted"))
            session = self._normalize_session((event or {}).get("when") or (event or {}).get("session"))
            # Session-aware proximity (2026-08-03): calendar rows are date-only
            # (midnight UTC), so a raw |event_dt - now| made a TODAY AMC ticker
            # (reporting ~20:00 UTC) look ~18h in the past at 18:00 UTC and rank
            # BELOW a tomorrow reporter — evicting it (PLTR was #639/3181) at the
            # 400-candidate cap before it was ever built. Mirror
            # `_earnings_reported_status`: today-not-yet-reported => imminent.
            delta_days = PreEarningsAgent._effective_event_delta_days(event_dt, session, now)
            is_reported = bool(
                event_dt is not None
                and PreEarningsAgent._earnings_reported_status(event_dt, session, now)
            )
            news_score = min(15, len(news_index.get(sym, []) or [])) / 15.0
            mismatch_score = 0.8 if sym in mismatch else 0.0
            # Coverage is staged strictly from the nearest unreported event to
            # the farthest. News/mismatch evidence only breaks proximity ties.
            return (
                1 if is_reported or delta_days is None else 0,
                float(delta_days) if delta_days is not None else float("inf"),
                -mismatch_score,
                -news_score,
                sym,
            )

        ranked = sorted(clean, key=_rank)
        return ranked[:limit]

    @staticmethod
    def _eps_pair(event: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
        row = event if isinstance(event, dict) else {}
        estimate = _safe_float(row.get("epsEstimated"), None)
        if estimate is None:
            estimate = _safe_float(row.get("eps_estimated"), None)
        if estimate is None:
            estimate = _safe_float(row.get("EPS Estimate"), None)
        actual = _safe_float(row.get("epsActual"), None)
        if actual is None:
            actual = _safe_float(row.get("eps_actual"), None)
        if actual is None:
            actual = _safe_float(row.get("Reported EPS"), None)
        if estimate is not None and not math.isfinite(float(estimate)):
            estimate = None
        if actual is not None and not math.isfinite(float(actual)):
            actual = None
        return estimate, actual

    def _compute_historical_beat_stats(
        self,
        symbol: str,
        *,
        max_events: int = 8,
        allow_remote: bool = False,
        force_refresh: bool = False,
    ) -> Dict[str, Any]:
        """Return real EPS beat statistics with a cache-first Yahoo fallback."""
        events = self._get_cached_earnings_history(symbol, max_events=max_events * 3)
        valid_pairs: List[Tuple[float, float]] = []
        for event in events:
            estimate, actual = self._eps_pair(event)
            if estimate is not None and actual is not None:
                valid_pairs.append((estimate, actual))
            if len(valid_pairs) >= max_events:
                break
        source = "economic_calendar"

        # A sparse calendar row (for example one completed quarter) must not
        # masquerade as a representative 100% beat rate. Prefer the verified
        # eight-quarter Yahoo sample whenever the cache has fewer observations.
        yahoo_rows = self._get_yfinance_earnings_history(
            symbol,
            max_events=max_events,
            allow_remote=allow_remote,
            force_refresh=force_refresh,
        )
        yahoo_pairs: List[Tuple[float, float]] = []
        for event in yahoo_rows:
            estimate, actual = self._eps_pair(event)
            if estimate is not None and actual is not None:
                yahoo_pairs.append((estimate, actual))
            if len(yahoo_pairs) >= max_events:
                break
        if len(yahoo_pairs) > len(valid_pairs):
            valid_pairs = yahoo_pairs
            source = "yfinance"

        beats = sum(1 for estimate, actual in valid_pairs if actual > estimate)
        samples = len(valid_pairs)
        return {
            "rate": round(beats / samples, 4) if samples else None,
            "beats": int(beats),
            "samples": int(samples),
            "source": source if samples else None,
            "unavailable_reason": (
                None
                if samples
                else "No completed reports with both estimated and reported EPS."
            ),
        }

    def _compute_historical_beat_rate(self, symbol: str) -> Optional[float]:
        """Compatibility wrapper; never fabricates a neutral beat rate."""
        return self._compute_historical_beat_stats(symbol).get("rate")

    def _get_analyst_estimates_cached(self, symbol: str) -> Optional[dict]:
        """Analyst estimates with a 6h TTL that REUSES the last good value when a fresh
        fetch is throttled/empty. yfinance's quoteSummary endpoint is rate-limited hard,
        so a boot-time burst of concurrent .info calls often returns empty — without this,
        every ticker would show N/A on those cycles."""
        sym = str(symbol or "").upper().strip()
        if not sym:
            return None
        cache = getattr(self, "_analyst_ttl_cache", None)
        if cache is None:
            cache = self._analyst_ttl_cache = {}
        now = time.time()
        hit = cache.get(sym)
        if hit and (now - hit[0]) < self._ANALYST_TTL_SECONDS:
            return hit[1]
        if self._external_budget_exhausted():
            return hit[1] if hit is not None else None
        if not self._reserve_external_call("yfinance_estimates"):
            return hit[1] if hit is not None else None
        fresh = None
        try:
            fresh = StockDataClient.get_estimates(sym)
        except Exception:
            fresh = None
        # Keep any useful verified field. Requiring a price target caused the
        # same Yahoo info payload to be fetched up to three times per symbol
        # when only market-cap/sector/EPS fields were available.
        useful_keys = (
            "targetMeanPrice",
            "marketCap",
            "sector",
            "estimatedEPS",
            "recommendationMean",
            "numberAnalysts",
        )
        if isinstance(fresh, dict) and any(
            fresh.get(key) is not None and str(fresh.get(key)).strip() != ""
            for key in useful_keys
        ):
            cache[sym] = (now, fresh)
            return fresh
        # Throttled/empty: keep serving the last good value rather than wiping to N/A.
        if hit is not None:
            return hit[1]
        return fresh

    def _resolve_sector(self, symbol_u: str, state_row: Dict[str, Any]) -> Optional[str]:
        """Sector resolution chain: state_row → fundamentalSnapshot → the 6h
        analyst-estimates cache (same yfinance .info payload — no extra
        request). Missing anywhere → None (today's behavior; sector_key folds
        to UNKNOWN). Never raises."""
        try:
            sec = str(
                (state_row or {}).get("sector")
                or (state_row or {}).get("fundamentalSnapshot", {}).get("sector")
                or ""
            ).strip()
            if sec:
                return sec
            est = self._get_analyst_estimates_cached(symbol_u)
            if isinstance(est, dict):
                sec = str(est.get("sector") or "").strip()
                if sec:
                    return sec
        except Exception:
            pass
        return None

    # ── Peer read-through grouping (owner requirement, 2026-11) ─────────────
    # Group each name with the peers it actually trades with so yesterday's
    # settled peer outcomes can (a) feed the AI prompt and (b) force a
    # re-analysis when they post-date the saved verdict. Grouping key chain:
    # curated theme override -> industry -> sector. Honest by construction:
    # every rung that is UNKNOWN/blank falls through, and a fully unresolved
    # name groups with NOTHING (None), never with a synthetic "UNKNOWN" pool.

    @staticmethod
    def _normalize_peer_token(value: Any) -> Optional[str]:
        """UPPER_SNAKE normalization for grouping keys; UNKNOWN/blank -> None."""
        token = re.sub(r"[^A-Z0-9]+", "_", str(value or "").upper().strip()).strip("_")
        if token in {"", "UNKNOWN", "N_A", "NA", "NONE"}:
            return None
        return token

    def _peer_theme_map(self, cfg: Optional[Dict[str, Any]]) -> Dict[str, str]:
        """Curated defaults merged with the operator's
        `pre_earnings.peer_theme_overrides` (symbol -> theme), which win on
        conflict. Theme values are normalized so "BTC Miners" == "BTC_MINERS"."""
        merged = dict(PEER_THEME_OVERRIDES)
        raw = (cfg or {}).get("peer_theme_overrides")
        if isinstance(raw, dict):
            for sym, theme in raw.items():
                s = str(sym or "").upper().strip()
                t = self._normalize_peer_token(theme)
                if s and t:
                    merged[s] = t
        return merged

    def _resolve_industry(
        self, symbol_u: str, state_row: Optional[Dict[str, Any]] = None
    ) -> Optional[str]:
        """Industry resolution chain: state_row -> fundamentalSnapshot -> the
        6h analyst-estimates cache (the same yfinance .info payload
        `_resolve_sector` already fetches — a TTL hit, no extra request).
        Missing anywhere -> None. Never raises."""
        try:
            ind = str(
                (state_row or {}).get("industry")
                or (state_row or {}).get("fundamentalSnapshot", {}).get("industry")
                or ""
            ).strip()
            if ind:
                return ind
            est = self._get_analyst_estimates_cached(symbol_u)
            if isinstance(est, dict):
                ind = str(est.get("industry") or "").strip()
                if ind:
                    return ind
        except Exception:
            pass
        return None

    def _peer_key_for_symbol(
        self,
        symbol: str,
        *,
        sector: Any = None,
        industry: Any = None,
        theme_map: Optional[Dict[str, str]] = None,
    ) -> Optional[str]:
        """Grouping key: curated theme override -> industry -> sector; None
        when every rung is UNKNOWN/blank (the name groups with nothing)."""
        sym = str(symbol or "").upper().strip()
        if not sym:
            return None
        theme = (theme_map or {}).get(sym)
        if theme:
            return theme
        ind = industry if industry is not None else self._resolve_industry(sym, None)
        token = self._normalize_peer_token(ind)
        if token:
            return token
        return self._normalize_peer_token(sector)

    def _peer_label_for_symbol(
        self,
        symbol: str,
        *,
        sector: Any = None,
        industry: Any = None,
        theme_map: Optional[Dict[str, str]] = None,
    ) -> Optional[str]:
        """Human label chain matching `_peer_key_for_symbol`: theme label ->
        industry (as reported, already title case) -> sector; None when the
        name resolves to no group."""
        sym = str(symbol or "").upper().strip()
        if not sym:
            return None
        theme = (theme_map or {}).get(sym)
        if theme:
            return PEER_THEME_LABELS.get(theme) or theme.replace("_", " ").title()
        ind = industry if industry is not None else self._resolve_industry(sym, None)
        if self._normalize_peer_token(ind):
            return str(ind).strip()
        if self._normalize_peer_token(sector):
            return str(sector).strip()
        return None

    def peer_key(
        self,
        symbol: str,
        *,
        sector: Any = None,
        industry: Any = None,
        cfg: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        return self._peer_key_for_symbol(
            symbol,
            sector=sector,
            industry=industry,
            theme_map=self._peer_theme_map(cfg),
        )

    def peer_key_label(
        self,
        symbol: str,
        *,
        sector: Any = None,
        industry: Any = None,
        cfg: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        return self._peer_label_for_symbol(
            symbol,
            sector=sector,
            industry=industry,
            theme_map=self._peer_theme_map(cfg),
        )

    def _peer_key_for_outcome(
        self, outcome: Dict[str, Any], theme_map: Dict[str, str]
    ) -> Optional[str]:
        """Peer key for a settled outcome row. Fast path: the key persisted at
        settlement. Legacy rows re-derive theme -> industry -> sector, where
        industry is read ONLY from the in-memory estimates cache — a fresh
        provider call per legacy outcome symbol would burn the per-cycle budget
        on a grouping join; the persisted sector still groups correctly when
        industry is cold."""
        if not isinstance(outcome, dict):
            return None
        persisted = self._normalize_peer_token(outcome.get("peer_key"))
        if persisted:
            return persisted
        sym = str(outcome.get("symbol") or "").upper().strip()
        if not sym:
            return None
        theme = (theme_map or {}).get(sym)
        if theme:
            return theme
        industry = str(outcome.get("industry") or "").strip()
        if not industry:
            cache = getattr(self, "_analyst_ttl_cache", None) or {}
            hit = cache.get(sym)
            est = hit[1] if isinstance(hit, tuple) and len(hit) == 2 else None
            if isinstance(est, dict):
                industry = str(est.get("industry") or "").strip()
        token = self._normalize_peer_token(industry)
        if token:
            return token
        return self._normalize_peer_token(outcome.get("sector"))

    def _build_peer_readthrough(
        self,
        symbol_u: str,
        *,
        sector: Any = None,
        industry: Any = None,
        cfg: Optional[Dict[str, Any]] = None,
        now: Optional[datetime] = None,
    ) -> Tuple[Optional[str], Optional[str], Optional[Dict[str, Any]]]:
        """(peer_key, peer_key_label, peer_readthrough) for one radar item.

        Selects two evidence layers from the same peer group, both inside the
        last `peer_readthrough_hours` (default 96h), newest first, capped at
        `peer_readthrough_max_peers` in total:

        1. Settled outcomes (evaluation landed in the window) — the mature
           layer.
        2. EARLY day-1 reactions (owner requirement, 2026-08-04): open
           predictions whose event already happened but which settle only
           ~36h later. For clustered earnings weeks (HUT/CIFR report today,
           MARA/CLSK/RIOT tomorrow) the settled layer would arrive AFTER the
           cluster already reported — useless. The early layer computes the
           real gap move from OHLC bars right now via
           `_compute_event_gap_move_pct` (1h cache, external-budget
           respecting), so the read-through fires while the cluster is still
           tradeable. Early peers are marked `early: true` and carry
           `evaluation_utc: None` (not final).

        The verdict: bearish when the positive share (rounded to whole
        percents) is <=34%, bullish at >=67%, else mixed — 1/3 positive reads
        bearish and 2/3 bullish, matching the owner's intuition.
        `peer_readthrough` is None when there is no fresh peer evidence at all
        (honest null, never an empty-object fake)."""
        cfg = cfg or {}
        theme_map = self._peer_theme_map(cfg)
        key = self._peer_key_for_symbol(
            symbol_u, sector=sector, industry=industry, theme_map=theme_map
        )
        label = self._peer_label_for_symbol(
            symbol_u, sector=sector, industry=industry, theme_map=theme_map
        )
        if key is None:
            return key, label, None
        hours = max(24, min(336, _safe_int(cfg.get("peer_readthrough_hours"), 96)))
        max_peers = max(1, min(20, _safe_int(cfg.get("peer_readthrough_max_peers"), 5)))
        now_dt = now or _utc_now()
        cutoff = now_dt - timedelta(hours=hours)
        feedback = getattr(self, "_feedback", {}) or {}
        candidates: List[Tuple[datetime, Dict[str, Any], float]] = []
        settled_symdays = set()
        for o in feedback.get("outcomes", []) or []:
            if not isinstance(o, dict):
                continue
            osym = str(o.get("symbol") or "").upper().strip()
            oevent_dt = _parse_iso(o.get("event_utc"))
            if osym and oevent_dt is not None:
                settled_symdays.add((osym, oevent_dt.date().isoformat()))
            if not osym or osym == symbol_u:
                continue  # never read through to yourself
            eval_dt = _parse_iso(o.get("evaluation_utc"))
            if eval_dt is None or eval_dt < cutoff or eval_dt > now_dt + timedelta(days=1):
                continue
            move = _safe_float(o.get("realized_signed_move_pct"), None)
            if move is None or not math.isfinite(float(move)):
                continue
            if self._peer_key_for_outcome(o, theme_map) != key:
                continue
            candidates.append((eval_dt, o, float(move)))

        # ── Early day-1 layer: peers that already reported but have not
        # settled yet. Dedup on (symbol, event day): calendar refreshes can
        # register the same report under both a T00 placeholder and a refined
        # T12/BMO key (seen live: CIFR 2026-08-04 twice) — the read-through
        # must count the report once. Prefer the BMO/AMC-resolved entry, then
        # the newest event_utc.
        early_by_symday: Dict[Tuple[str, str], Tuple[datetime, str, Dict[str, Any]]] = {}
        open_preds = feedback.get("open_predictions") or {}
        if isinstance(open_preds, dict):
            pred_iter = open_preds.values()
        else:
            pred_iter = open_preds
        for pred in pred_iter:
            if not isinstance(pred, dict):
                continue
            psym = str(pred.get("symbol") or "").upper().strip()
            if not psym or psym == symbol_u:
                continue
            event_dt = _parse_iso(pred.get("event_utc"))
            if event_dt is None or event_dt > now_dt or event_dt < cutoff:
                continue
            if self._peer_key_for_symbol(
                psym, sector=pred.get("sector"), industry=None, theme_map=theme_map
            ) != key:
                continue
            day = event_dt.date().isoformat()
            sess = self._normalize_session(pred.get("session"))
            prev = early_by_symday.get((psym, day))
            if prev is not None:
                prev_sess = prev[1]
                prev_resolved = prev_sess in ("BMO", "AMC")
                cur_resolved = sess in ("BMO", "AMC")
                if prev_resolved and not cur_resolved:
                    continue
                if prev_resolved == cur_resolved and prev[0] >= event_dt:
                    continue
            early_by_symday[(psym, day)] = (event_dt, sess, pred)

        early_rows: List[Tuple[datetime, str, Dict[str, Any], float]] = []
        early_sorted = sorted(
            early_by_symday.items(), key=lambda kv: kv[1][0], reverse=True
        )
        for (psym, day), (event_dt, sess, pred) in early_sorted:
            if (psym, day) in settled_symdays:
                continue  # already matured into the settled layer
            if len(early_rows) >= max_peers:
                break  # cap price lookups per item; OHLC cache is 1h anyway
            move = self._compute_event_gap_move_pct(psym, event_dt, sess)
            if move is None or not math.isfinite(float(move)):
                # AMC reporter whose next-open bar does not exist yet, or the
                # external budget is exhausted — honest skip, no fake zero.
                continue
            early_rows.append((event_dt, psym, pred, float(move)))

        combined: List[Tuple[datetime, Dict[str, Any]]] = []
        for eval_dt, o, move in candidates:
            combined.append(
                (
                    eval_dt,
                    {
                        "symbol": str(o.get("symbol") or "").upper().strip(),
                        "event_utc": o.get("event_utc"),
                        "evaluation_utc": _iso(eval_dt),
                        "signed_move_pct": round(move, 4),
                        "ai_verdict": (
                            str(o.get("predicted_ai_verdict") or "").strip() or None
                        ),
                        "early": False,
                    },
                )
            )
        for event_dt, psym, pred, move in early_rows:
            combined.append(
                (
                    event_dt,
                    {
                        "symbol": psym,
                        "event_utc": _iso(event_dt),
                        "evaluation_utc": None,
                        "signed_move_pct": round(move, 4),
                        "ai_verdict": (
                            str(pred.get("predicted_ai_verdict") or "").strip()
                            or None
                        ),
                        "early": True,
                    },
                )
            )
        if not combined:
            return key, label, None
        combined.sort(key=lambda row: row[0], reverse=True)
        combined = combined[:max_peers]
        peers = [row[1] for row in combined]
        count = len(peers)
        moves = [p["signed_move_pct"] for p in peers]
        avg_move = sum(moves) / count
        pct_positive = sum(1 for m in moves if m > 0) / count
        pct_pct = round(pct_positive, 2)  # whole-percent share for the verdict bands
        if count >= 2 and pct_pct <= 0.34:
            verdict = "bearish"
        elif count >= 2 and pct_pct >= 0.67:
            verdict = "bullish"
        else:
            verdict = "mixed"
        readthrough = {
            "peers": peers,
            "count": count,
            "avg_signed_move_pct": round(avg_move, 4),
            "pct_positive": round(pct_positive, 4),
            "verdict": verdict,
            # Staleness anchor for the re-analysis rule: the freshest peer
            # evidence timestamp (settled evaluation OR early report time). A
            # verdict generated before this did not see the evidence.
            "latest_evaluation_utc": _iso(combined[0][0]),
            "window_hours": hours,
            "early_count": sum(1 for p in peers if p.get("early")),
        }
        return key, label, readthrough

    def _settlement_peer_key(
        self, pred: Dict[str, Any], symbol: str, cfg: Dict[str, Any]
    ) -> Tuple[str, Optional[str]]:
        """(peer_key, peer_key_label) persisted onto a settled outcome.

        Reuses the prediction's stored values. Legacy predictions (registered
        before peer keys existed, or resolved to UNKNOWN) re-derive through the
        same theme -> industry -> sector chain; industry comes from the 6h
        estimates cache, which reserves against the normal per-cycle budget, so
        a settlement burst cannot stampede the provider. Honest UNKNOWN/None
        when nothing resolves."""
        key = self._normalize_peer_token((pred or {}).get("peer_key"))
        label = str((pred or {}).get("peer_key_label") or "").strip() or None
        if key:
            return key, label
        sector = (pred or {}).get("sector")
        theme_map = self._peer_theme_map(cfg)
        resolved = self._peer_key_for_symbol(symbol, sector=sector, theme_map=theme_map)
        if resolved is None:
            return "UNKNOWN", None
        resolved_label = self._peer_label_for_symbol(symbol, sector=sector, theme_map=theme_map)
        return resolved, resolved_label

    # ── History sector resolution (owner requirement, 2026-08-04) ─────────
    # The track-record history showed 183/200 rows as UNKNOWN: old predictions
    # were registered before enrichment persisted a sector. Resolution chain,
    # cheapest first: (1) yfinance estimates payload already carries `sector`;
    # (2) AI classification from the cached ticker news headlines (only when
    # news exists — no news honestly stays UNKNOWN); (3) nothing resolved ->
    # UNKNOWN with a manual refresh button in the UI.

    # GICS vocabulary mapped onto the yfinance sector names this app already
    # displays, so AI answers land in the same taxonomy as verified data.
    _SECTOR_AI_CANON = {
        "energy": "Energy",
        "materials": "Basic Materials",
        "basic materials": "Basic Materials",
        "industrials": "Industrials",
        "consumer discretionary": "Consumer Cyclical",
        "consumer cyclical": "Consumer Cyclical",
        "consumer staples": "Consumer Defensive",
        "consumer defensive": "Consumer Defensive",
        "health care": "Healthcare",
        "healthcare": "Healthcare",
        "financials": "Financial Services",
        "financial services": "Financial Services",
        "information technology": "Technology",
        "technology": "Technology",
        "communication services": "Communication Services",
        "utilities": "Utilities",
        "real estate": "Real Estate",
    }

    def _resolve_sector_from_estimates(self, symbol: str) -> Optional[str]:
        """Verified sector from the yfinance estimates payload (6h TTL cache,
        external-budget respecting). None when unavailable/blank/UNKNOWN."""
        est = self._get_analyst_estimates_cached(symbol)
        if not isinstance(est, dict):
            return None
        sector = str(est.get("sector") or "").strip()
        if not sector or sector.upper() == "UNKNOWN":
            return None
        return sector

    def _classify_sector_from_news(
        self, symbol: str, cfg: Optional[Dict[str, Any]] = None
    ) -> Optional[str]:
        """AI sector classification from cached ticker news headlines.

        Owner rule: for UNKNOWN names the AI must search the news and assign a
        sector; when there are NO news articles the name honestly stays
        UNKNOWN (the UI then offers a manual sector-refresh button). The model
        answers with one GICS sector which is mapped onto the yfinance sector
        vocabulary; anything unparseable/unsure -> None (never a guess)."""
        cfg = cfg or {}
        if not bool(cfg.get("sector_ai_enabled", True)):
            return None
        sym = str(symbol or "").upper().strip()
        if not sym:
            return None
        articles: List[Any] = []
        try:
            news_agent = get_news_tab_agent_if_ready()
            if news_agent is None and getattr(self, "config_manager", None) is not None:
                news_agent = get_news_tab_agent(self.config_manager.config)
            if news_agent is not None and hasattr(news_agent, "get_ticker_news"):
                articles = list(news_agent.get_ticker_news(sym, max_items=8) or [])
        except Exception:
            articles = []
        headlines: List[str] = []
        for a in articles:
            if isinstance(a, dict):
                title = str(a.get("title") or "").strip()
                summary = str(a.get("summary") or "").strip()
            else:
                title = str(getattr(a, "title", "") or "").strip()
                summary = str(getattr(a, "summary", "") or "").strip()
            line = title or summary
            if line:
                headlines.append(line[:220])
            if len(headlines) >= 5:
                break
        if not headlines:
            return None  # honest: no news -> stays UNKNOWN, refresh button in UI
        model = str(cfg.get("sector_ai_model") or cfg.get("ollama_model") or "").strip() or None
        timeout_sec = max(5, min(60, _safe_int(cfg.get("sector_ai_timeout_sec"), 20)))
        system = (
            "You classify a US-listed company into exactly one GICS sector "
            "from its news headlines. Reply with ONLY one of: Energy, Basic "
            "Materials, Industrials, Consumer Cyclical, Consumer Defensive, "
            "Healthcare, Financial Services, Technology, Communication "
            "Services, Utilities, Real Estate. If the headlines do not make "
            "the company's business clear, reply UNKNOWN. No explanations."
        )
        prompt = f"Ticker: {sym}\nHeadlines:\n- " + "\n- ".join(headlines)
        try:
            result = ollama.generate(
                prompt,
                model=model,
                system=system,
                timeout_override=timeout_sec,
            ) or {}
            text = str(result.get("response") or "").strip()
        except Exception:
            return None
        if not text:
            return None
        first_line = text.splitlines()[0].strip().strip('"').rstrip(".").strip()
        return self._SECTOR_AI_CANON.get(first_line.lower())

    def resolve_history_sector(
        self,
        symbol: str,
        cfg: Optional[Dict[str, Any]] = None,
        *,
        allow_ai: bool = True,
    ) -> Tuple[Optional[str], str]:
        """(sector, source) for a history row symbol. source is one of
        "estimates" | "ai_news" | "none". Nothing is persisted here — the
        caller decides (resolution may be a probe)."""
        sym = str(symbol or "").upper().strip()
        if not sym:
            return None, "none"
        sector = self._resolve_sector_from_estimates(sym)
        if sector:
            return sector, "estimates"
        if allow_ai:
            sector = self._classify_sector_from_news(sym, cfg)
            if sector:
                return sector, "ai_news"
        return None, "none"

    def _persist_sector_resolution(
        self,
        symbol: str,
        sector: str,
        cfg: Optional[Dict[str, Any]] = None,
    ) -> int:
        with self._feedback_guard():
            return self._persist_sector_resolution_locked(symbol, sector, cfg)

    def _persist_sector_resolution_locked(
        self,
        symbol: str,
        sector: str,
        cfg: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Write a resolved sector back onto every UNKNOWN history row for the
        symbol (settled outcomes + open predictions) and re-derive the peer
        grouping with the new information. Returns touched row count."""
        sym = str(symbol or "").upper().strip()
        sector = str(sector or "").strip()
        if not sym or not sector:
            return 0
        cfg = cfg or {}
        theme_map = self._peer_theme_map(cfg)
        touched = 0
        feedback = getattr(self, "_feedback", {}) or {}
        for o in feedback.get("outcomes", []) or []:
            if not isinstance(o, dict):
                continue
            if str(o.get("symbol") or "").upper().strip() != sym:
                continue
            cur = str(o.get("sector") or "").strip()
            if cur and cur.upper() != "UNKNOWN":
                continue
            o["sector"] = sector
            if self._normalize_peer_token(o.get("peer_key")) in (None, "UNKNOWN"):
                pk = self._peer_key_for_symbol(sym, sector=sector, theme_map=theme_map)
                o["peer_key"] = pk if pk is not None else "UNKNOWN"
                o["peer_key_label"] = (
                    self._peer_label_for_symbol(sym, sector=sector, theme_map=theme_map)
                    if pk is not None
                    else None
                )
            touched += 1
        open_preds = feedback.get("open_predictions") or {}
        pred_iter = open_preds.values() if isinstance(open_preds, dict) else open_preds
        for pred in pred_iter:
            if not isinstance(pred, dict):
                continue
            if str(pred.get("symbol") or "").upper().strip() != sym:
                continue
            cur = str(pred.get("sector") or "").strip()
            if cur and cur.upper() != "UNKNOWN":
                continue
            pred["sector"] = sector
            if self._normalize_peer_token(pred.get("peer_key")) in (None, "UNKNOWN"):
                pk = self._peer_key_for_symbol(sym, sector=sector, theme_map=theme_map)
                pred["peer_key"] = pk
                pred["peer_key_label"] = (
                    self._peer_label_for_symbol(sym, sector=sector, theme_map=theme_map)
                    if pk is not None
                    else None
                )
            touched += 1
        if touched:
            self._persist_feedback_store()
        return touched

    def _backfill_history_sectors(self, cfg: Dict[str, Any]) -> int:
        """Proactive per-cycle sector backfill over the recent history window.

        Resolves a small batch of UNKNOWN-sector symbols per cycle (default 8)
        using ONLY the verified estimates source (no AI here — the AI rung is
        reserved for the explicit UI refresh so a cycle never burns LLM budget
        on 183 legacy rows at once). Newest rows first so the visible history
        cleans up within a few cycles."""
        limit = max(0, min(50, _safe_int(cfg.get("sector_backfill_per_cycle"), 8)))
        if limit <= 0:
            return 0
        feedback = getattr(self, "_feedback", {}) or {}
        outcomes = feedback.get("outcomes", []) or []

        # Newest first (evaluation desc), distinct symbols, UNKNOWN sector only.
        def _eval_key(o: Dict[str, Any]) -> str:
            return str(o.get("evaluation_utc") or o.get("event_utc") or "")

        seen: set = set()
        candidates: List[str] = []
        for o in sorted(
            [r for r in outcomes if isinstance(r, dict)],
            key=_eval_key,
            reverse=True,
        )[:300]:
            cur = str(o.get("sector") or "").strip()
            if cur and cur.upper() != "UNKNOWN":
                continue
            sym = str(o.get("symbol") or "").upper().strip()
            if not sym or sym in seen:
                continue
            seen.add(sym)
            candidates.append(sym)
            if len(candidates) >= limit:
                break
        resolved = 0
        for sym in candidates:
            sector, _src = self.resolve_history_sector(sym, cfg, allow_ai=False)
            if sector:
                if self._persist_sector_resolution(sym, sector, cfg):
                    resolved += 1
        return resolved

    def _enrich_with_verified_data(self, symbol: str) -> dict:
        eps_est_curr = None
        rev_est = None
        analyst_count = 0
        # Real analyst consensus from yfinance info (free, reliable, per-ticker)
        analyst_target_upside_pct = None
        analyst_recommendation = None
        analyst_rec_mean = None
        try:
            est_data = self._get_analyst_estimates_cached(symbol)
            if est_data:
                eps_est_curr = est_data.get("estimatedEPS")
                rev_est = est_data.get("estimatedRevenue")
                analyst_count = (
                    est_data.get("numberAnalysts")
                    or est_data.get("analystCount")
                    or est_data.get("numberOfAnalysts")
                    or None
                )
                _target = _safe_float(est_data.get("targetMeanPrice"), None)
                _px = _safe_float(est_data.get("currentPrice"), None)
                _tlow = _safe_float(est_data.get("targetLowPrice"), None)
                _thigh = _safe_float(est_data.get("targetHighPrice"), None)
                # Show the REAL upside, even when large: clinical biotechs (e.g. HELP/Cybin,
                # $6.76 price vs $42 mean target on 8 analysts) genuinely trade far below
                # targets. Only reject true feed artifacts: sub-$1 prices (division noise) or
                # an internally inconsistent target band (mean outside [low, high]).
                if _target and _target > 0 and _px and _px >= 1.0:
                    _consistent = True
                    if _tlow is not None and _thigh is not None and _tlow > 0 and _thigh > 0:
                        _consistent = (_tlow * 0.5) <= _target <= (_thigh * 1.5)
                    if _consistent:
                        analyst_target_upside_pct = round((_target - _px) / _px * 100.0, 2)
                _rk = est_data.get("recommendationKey")
                if isinstance(_rk, str) and _rk.strip() and _rk.lower() != "none":
                    analyst_recommendation = _rk.strip().lower()
                analyst_rec_mean = _safe_float(est_data.get("recommendationMean"), None)
        except Exception:
            pass

        # No real options-flow feed (needs a paid provider) — leave unknown rather than
        # faking a neutral 1.0 that renders as a misleading per-ticker "50".
        calls_vs_puts = None

        return {
            "eps_estimate_current_quarter": eps_est_curr,
            "eps_estimate_revisions_30d": None,  # no analyst-revision feed (paid) — keep honest, excluded from bias
            "revenue_estimate_current": rev_est,
            "analyst_count": analyst_count,
            # Real analyst consensus — drives the analyst bias component AND the UI column
            "analyst_target_upside_pct": analyst_target_upside_pct,
            "analyst_recommendation": analyst_recommendation,
            "analyst_rec_mean": round(analyst_rec_mean, 2) if analyst_rec_mean is not None else None,
            "implied_move_pct": None,
            "iv_rank": None,
            "calls_vs_puts_ratio": calls_vs_puts,
            "net_insider_sentiment": None  # no reliable free insider parse — honest null, not a fake NEUTRAL
        }

    def _fetch_etoro_context(self, symbol: str) -> Optional[Dict[str, Any]]:
        sym = str(symbol or "").upper().strip()
        if not sym:
            return None
        try:
            from core.etoro_client import get_etoro_client
            client = get_etoro_client()
            if not client.is_available():
                return None
            meta = client.search_instrument(sym)
            if not meta:
                return None
            iid = meta.get("instrument_id")
            rates = client.get_live_rates([iid]) if iid else {}
            rate_info = rates.get(iid, {}) if iid else {}
            feed = client.get_feed_for_instrument(iid, limit=2) if iid else []
            headlines = [p.get("text") for p in feed if p.get("text")]

            return {
                "instrument_id": iid,
                "holders": meta.get("holders"),
                "popularity_rank": meta.get("popularity_rank"),
                "buy_pct": meta.get("buy_pct"),
                "sell_pct": meta.get("sell_pct"),
                "pe_ratio": meta.get("pe_ratio"),
                "esg_score": meta.get("esg_score"),
                "bid": rate_info.get("bid"),
                "ask": rate_info.get("ask"),
                "spread": rate_info.get("spread"),
                "social_headlines": headlines[:2],
            }
        except Exception as exc:
            logger.debug("eToro context fetch failed for %s: %s", sym, exc)
            return None

    def _build_item_for_symbol(
        self,
        symbol: str,
        news_rows: List[Dict[str, Any]],
        earnings_rows: List[Dict[str, Any]],
        mismatch_info: Optional[Dict[str, Any]],
        cfg: Dict[str, Any],
        state_row: Dict[str, Any],
        as_of_utc: Optional[str],
        weight_overrides: Optional[Dict[str, float]] = None,
        calibration_model: Optional[Dict[str, Any]] = None,
        calibration_metrics: Optional[Dict[str, Any]] = None,
        already_reported_info: Optional[Dict[str, Any]] = None,
        previous_item: Optional[Dict[str, Any]] = None,
        refresh_enrichment: bool = False,
    ) -> Dict[str, Any]:
        symbol_u = str(symbol or "").upper().strip()
        event = self._pick_primary_event(earnings_rows)

        earnings_dt = _parse_iso((event or {}).get("date") or (event or {}).get("date_formatted"))
        session = self._normalize_session((event or {}).get("when") or (event or {}).get("session"))
        event_status = str((event or {}).get("event_status") or ("UPCOMING" if earnings_dt else "UNVERIFIED")).upper()
        event_source = str((event or {}).get("source") or "calendar_cache_unknown")
        date_confidence = str((event or {}).get("date_confidence") or "low").lower()
        confirmed = bool((event or {}).get("confirmed")) or (
            _safe_float((event or {}).get("eps_actual"), None) is not None
            or _safe_float((event or {}).get("revenue_actual"), None) is not None
        )

        enrichment_now = _parse_iso(as_of_utc) or _utc_now()
        enrichment_now_iso = _iso(enrichment_now)
        previous_enrichment = self._same_event_enrichment_source(
            symbol_u,
            earnings_dt,
            previous_item,
        )
        verified_at = (
            copy.deepcopy(previous_enrichment.get("enrichment_verified_at_utc"))
            if isinstance(previous_enrichment, dict)
            and isinstance(previous_enrichment.get("enrichment_verified_at_utc"), dict)
            else {}
        )
        refresh_after = (
            copy.deepcopy(previous_enrichment.get("enrichment_refresh_after_utc"))
            if isinstance(previous_enrichment, dict)
            and isinstance(previous_enrichment.get("enrichment_refresh_after_utc"), dict)
            else {}
        )
        value_sources: Dict[str, str] = {}
        legacy_verified_at = (
            _iso(_parse_iso(previous_enrichment.get("updated_utc")))
            if isinstance(previous_enrichment, dict)
            else None
        )

        def _mark_persisted(field: str, *, retry: bool = False) -> None:
            value_sources[field] = "persisted_same_event"
            if not _parse_iso(verified_at.get(field)) and legacy_verified_at:
                verified_at[field] = legacy_verified_at
            if retry:
                retry_seconds = max(
                    60,
                    _safe_int(
                        self._ENRICHMENT_RETRY_SECONDS.get(
                            field,
                            self._VERIFIED_ENRICHMENT_TTL_SECONDS.get(
                                field,
                                3600,
                            ),
                        ),
                        3600,
                    ),
                )
                refresh_after[field] = _iso(
                    enrichment_now + timedelta(seconds=retry_seconds)
                )

        def _mark_fresh(field: str) -> None:
            value_sources[field] = "fresh"
            if enrichment_now_iso:
                verified_at[field] = enrichment_now_iso
            refresh_after.pop(field, None)

        fresh_news_rows = [
            dict(row) for row in (news_rows or []) if isinstance(row, dict)
        ]
        previous_news = (
            [
                dict(row)
                for row in (previous_enrichment.get("top_news") or [])
                if isinstance(row, dict)
            ]
            if isinstance(previous_enrichment, dict)
            else []
        )
        # Revalidate persisted evidence too: an old provider bucket may refer
        # to another issuer with the same symbol.
        previous_news = self._build_news_index(
            [dict(row, symbol=symbol_u) for row in previous_news], [symbol_u]
        ).get(symbol_u, [])
        news_bundle = self._prepare_news_evidence(
            fresh_news_rows,
            previous_news,
            now=enrichment_now,
            lookback_hours=max(24, _safe_int(cfg.get("lookback_hours"), 336)),
            evidence_limit=max(4, _safe_int(cfg.get("news_evidence_limit"), 12)),
        )
        effective_news_rows = list(news_bundle.get("rows") or [])
        evidence_rows = list(news_bundle.get("evidence_rows") or [])
        if fresh_news_rows:
            _mark_fresh("news_context")
        elif effective_news_rows:
            _mark_persisted("news_context", retry=True)
        news_rows = effective_news_rows
        top_news = [self._compact_news_row(row) for row in evidence_rows]
        news_count = int(news_bundle.get("relevant_articles") or 0)
        news_digest = {
            "article_count": news_count,
            "scanned_articles": int(news_bundle.get("article_count") or 0),
            "evidence_count": len(top_news),
            "unique_sources": int(news_bundle.get("unique_sources") or 0),
            "material_articles": int(news_bundle.get("material_articles") or 0),
            "fresh_articles": int(news_bundle.get("fresh_articles") or 0),
            "weighted_articles": float(news_bundle.get("weighted_articles") or 0.0),
            "newest_utc": news_bundle.get("newest_utc"),
            "oldest_utc": news_bundle.get("oldest_utc"),
            "fingerprint": news_bundle.get("fingerprint"),
            "lookback_hours": max(24, _safe_int(cfg.get("lookback_hours"), 336)),
        }

        market_cap = self._verified_float(
            state_row.get("market_cap")
            or state_row.get("marketCap")
            or state_row.get("fundamentalSnapshot", {}).get("market_cap"),
            positive=True,
        )
        avg_volume = self._verified_float(
            state_row.get("avg_volume")
            or state_row.get("volume")
            or state_row.get("fundamentalSnapshot", {}).get("avg_volume"),
            positive=True,
        )
        fresh_market_cap = market_cap is not None
        fresh_avg_volume = avg_volume is not None
        sector = str(
            state_row.get("sector")
            or state_row.get("fundamentalSnapshot", {}).get("sector")
            or ""
        ).strip()
        previous_sector = (
            str(previous_enrichment.get("sector") or "").strip()
            if isinstance(previous_enrichment, dict)
            else ""
        )
        if (
            sector.upper() in {"", "UNKNOWN", "N/A", "NONE"}
            and previous_sector.upper() not in {"", "UNKNOWN", "N/A", "NONE"}
        ):
            sector = previous_sector
        if sector.upper() in {"", "UNKNOWN", "N/A", "NONE"}:
            sector = self._resolve_sector(symbol_u, state_row)

        # Peer read-through (owner requirement, 2026-11): group this name with
        # its traded-as-one theme/industry/sector peers and surface how those
        # peers actually moved after THEIR reports in the last ~96h. Feeds the
        # AI prompt AND the staleness rule that re-analyzes a cached verdict
        # when fresh peer outcomes post-date it. Industry resolves from the
        # same estimates payload `_resolve_sector` just used (TTL hit).
        industry = self._resolve_industry(symbol_u, state_row)
        peer_key_value, peer_key_label_value, peer_readthrough = self._build_peer_readthrough(
            symbol_u,
            sector=sector,
            industry=industry,
            cfg=cfg,
        )

        previous_market_cap = (
            self._verified_float(
                previous_enrichment.get("market_cap"),
                positive=True,
            )
            if isinstance(previous_enrichment, dict)
            else None
        )
        previous_avg_volume = (
            self._verified_float(
                previous_enrichment.get("avg_volume"),
                positive=True,
            )
            if isinstance(previous_enrichment, dict)
            else None
        )
        reuse_liquidity = (
            not refresh_enrichment
            and (previous_market_cap is not None or previous_avg_volume is not None)
            and self._persisted_enrichment_is_fresh(
                previous_enrichment,
                "liquidity_context",
                enrichment_now,
            )
        )
        if reuse_liquidity:
            if market_cap is None:
                market_cap = previous_market_cap
            if avg_volume is None:
                avg_volume = previous_avg_volume
            _mark_persisted("liquidity_context")

        previous_range = None
        if isinstance(previous_enrichment, dict):
            previous_range = self._verified_float(
                previous_enrichment.get("expected_move_pct")
                if previous_enrichment.get("expected_move_pct") is not None
                else previous_enrichment.get("range_proxy_pct")
            )
            if previous_range is not None and previous_range < 0.0:
                previous_range = None
        reuse_range = (
            not refresh_enrichment
            and previous_range is not None
            and self._persisted_enrichment_is_fresh(
                previous_enrichment,
                "range_proxy",
                enrichment_now,
            )
        )
        expected_move_pct = previous_range if reuse_range else None
        if reuse_range:
            _mark_persisted("range_proxy")

        quote: Dict[str, Any] = {}
        if (
            market_cap is None
            or avg_volume is None
            or expected_move_pct is None
        ):
            quote = self._get_cached_quote(symbol_u)
        if market_cap is None:
            market_cap = self._verified_float(
                (quote or {}).get("marketCap"),
                positive=True,
            )
            fresh_market_cap = market_cap is not None
        # Pre-Earnings market-cap filter (Faza 3, 2026-07-07): yfinance chart
        # quote has no marketCap, so fall back to the analyst-estimates cache
        # (same .info payload get_estimates already fetched — marketCap added)
        # then to StockDataClient.get_market_cap (yfinance .info + FinnHub
        # profile2 fallback, TTL-cached 6h).
        if market_cap is None:
            try:
                est = self._get_analyst_estimates_cached(symbol_u)
                if isinstance(est, dict):
                    market_cap = self._verified_float(
                        est.get("marketCap"),
                        positive=True,
                    )
                    fresh_market_cap = market_cap is not None
            except Exception:
                pass
        if (
            market_cap is None
            and not self._external_budget_exhausted()
            and self._reserve_external_call("yfinance_market_cap")
        ):
            try:
                market_cap = self._verified_float(
                    StockDataClient.get_market_cap(symbol_u),
                    positive=True,
                )
                fresh_market_cap = market_cap is not None
            except Exception:
                pass
        if market_cap is None:
            market_cap = previous_market_cap
        if avg_volume is None:
            avg_volume = self._verified_float(
                (quote or {}).get("volume"),
                positive=True,
            )
            fresh_avg_volume = avg_volume is not None
        if avg_volume is None:
            avg_volume = previous_avg_volume
        if market_cap is not None or avg_volume is not None:
            if fresh_market_cap or fresh_avg_volume:
                _mark_fresh("liquidity_context")
            elif value_sources.get("liquidity_context") != "persisted_same_event":
                _mark_persisted(
                    "liquidity_context",
                    retry=not reuse_liquidity,
                )

        previous_hist_stats = self._historical_snapshot_from_item(
            previous_enrichment
        )
        reuse_history = (
            not refresh_enrichment
            and previous_hist_stats is not None
            and self._persisted_enrichment_is_fresh(
                previous_enrichment,
                "historical_move_history",
                enrichment_now,
            )
        )
        if reuse_history:
            hist_stats = previous_hist_stats
            _mark_persisted("historical_move_history")
        else:
            fresh_hist_stats = self._compute_historical_earnings_gap_stats(
                symbol_u
            )
            fresh_median = self._verified_float(
                fresh_hist_stats.get("median_move_pct")
            )
            fresh_samples = max(
                0,
                _safe_int(fresh_hist_stats.get("sample_size"), 0),
            )
            fresh_valid = (
                fresh_median is not None
                and fresh_median >= 0.0
                and fresh_samples > 0
            )
            previous_samples = (
                _safe_int(previous_hist_stats.get("sample_size"), 0)
                if previous_hist_stats
                else 0
            )
            if (
                previous_hist_stats is not None
                and (not fresh_valid or fresh_samples < previous_samples)
            ):
                hist_stats = previous_hist_stats
                _mark_persisted(
                    "historical_move_history",
                    retry=True,
                )
            else:
                hist_stats = fresh_hist_stats
                if fresh_valid:
                    _mark_fresh("historical_move_history")

        historical_median_move_pct = self._verified_float(
            hist_stats.get("median_move_pct")
        )
        historical_samples = max(
            0,
            _safe_int(hist_stats.get("sample_size"), 0),
        )
        if expected_move_pct is None:
            fresh_expected_move = self._verified_float(
                self._estimate_expected_move_pct(symbol_u)
            )
            if fresh_expected_move is not None and fresh_expected_move >= 0.0:
                expected_move_pct = fresh_expected_move
                _mark_fresh("range_proxy")
            elif previous_range is not None:
                expected_move_pct = previous_range
                _mark_persisted("range_proxy", retry=True)

        historical_score = _score_historical_median_move(historical_median_move_pct)
        news_intensity_score = _score_news_intensity(news_count)
        rule_severity_score, severity_label = self._score_catalyst_severity(news_rows)
        liquidity_score = self._score_liquidity_risk(market_cap, avg_volume)
        implied_score = _score_implied_move(expected_move_pct)

        raw_gap_risk, contributors = compute_gap_risk_score(
            historical_move_score=historical_score,
            news_intensity_score=news_intensity_score,
            catalyst_severity_score=rule_severity_score,
            liquidity_risk_score=liquidity_score,
            implied_move_score=implied_score,
            weight_overrides=weight_overrides,
        )
        gap_risk = raw_gap_risk

        sector_key = str(sector or "UNKNOWN").upper()
        cap_bucket = _cap_bucket(market_cap)
        group_factor, group_samples = self._resolve_calibration_factor(calibration_model, sector_key, cap_bucket)
        # Kept before the factor is applied: the factor must be fitted against
        # uncalibrated scores, or it chases its own previous correction.
        gap_risk_raw = gap_risk
        if group_factor != 1.0:
            gap_risk = int(round(_clamp(gap_risk * group_factor, 0.0, 100.0)))
            contributors.append(
                {
                    "name": "sector_cap_calibration",
                    "value": round(group_factor, 4),
                    "weight": 0.0,
                    "note": f"Sector/Cap calibration factor ({sector_key}/{cap_bucket}, n={group_samples})",
                }
            )

        if mismatch_info:
            gap_risk = min(100, gap_risk + 12)
            contributors.append(
                {
                    "name": "calendar_mismatch_boost",
                    "value": 1.0,
                    "weight": 0.0,
                    "note": "News suggests earnings but calendar entry missing in +/-2d window",
                }
            )

        news_sentiment_raw = self._score_news_sentiment(news_rows)
        analyst_proxy = self._score_analyst_revision_proxy(news_rows)
        news_sentiment = _clamp((0.8 * news_sentiment_raw) + (0.2 * analyst_proxy), -1.0, 1.0)
        catalyst_flags = self._score_catalyst_direction_flags(news_rows)
        previous_earnings_trend = (
            self._verified_float(
                previous_enrichment.get("earnings_trend_score")
            )
            if isinstance(previous_enrichment, dict)
            else None
        )
        reuse_earnings_trend = (
            not refresh_enrichment
            and previous_earnings_trend is not None
            and self._persisted_enrichment_is_fresh(
                previous_enrichment,
                "earnings_trend",
                enrichment_now,
            )
        )
        if reuse_earnings_trend:
            earnings_trend = previous_earnings_trend
            _mark_persisted("earnings_trend")
        else:
            earnings_trend = self._verified_float(
                self._estimate_earnings_trend_score(symbol_u)
            )
            if earnings_trend is not None:
                _mark_fresh("earnings_trend")
            elif previous_earnings_trend is not None:
                earnings_trend = previous_earnings_trend
                _mark_persisted("earnings_trend", retry=True)

        # Real post-earnings history: what the stock actually did after the last 3 reports,
        # plus a small directional tilt the bias factors in (per user request).
        previous_reactions = self._verified_reactions(
            previous_enrichment.get("recent_reactions")
            if isinstance(previous_enrichment, dict)
            else None
        )
        reuse_reactions = (
            not refresh_enrichment
            and bool(previous_reactions)
            and self._persisted_enrichment_is_fresh(
                previous_enrichment,
                "recent_reactions",
                enrichment_now,
            )
        )
        if reuse_reactions:
            recent_reactions = previous_reactions
            _mark_persisted("recent_reactions")
        else:
            fresh_reactions = self._verified_reactions(
                self._compute_recent_earnings_reactions(symbol_u, n=3)
            )
            if (
                previous_reactions
                and len(fresh_reactions) < len(previous_reactions)
            ):
                recent_reactions = previous_reactions
                _mark_persisted("recent_reactions", retry=True)
            else:
                recent_reactions = fresh_reactions
                if recent_reactions:
                    _mark_fresh("recent_reactions")

        previous_analyst = self._analyst_snapshot_from_item(
            previous_enrichment
        )
        reuse_analyst = (
            not refresh_enrichment
            and previous_analyst is not None
            and self._persisted_enrichment_is_fresh(
                previous_enrichment,
                "analyst_consensus",
                enrichment_now,
            )
        )
        if reuse_analyst:
            enriched_data = dict(previous_analyst)
            _mark_persisted("analyst_consensus")
        else:
            enriched_data = self._enrich_with_verified_data(symbol_u)
            fresh_analyst = self._analyst_snapshot_from_item(enriched_data)
            if (
                previous_analyst is not None
                and self._analyst_snapshot_quality(fresh_analyst)
                < self._analyst_snapshot_quality(previous_analyst)
            ):
                enriched_data.update(previous_analyst)
                _mark_persisted("analyst_consensus", retry=True)
            elif fresh_analyst is not None:
                enriched_data.update(fresh_analyst)
                _mark_fresh("analyst_consensus")

        previous_beat_stats = self._beat_snapshot_from_item(
            previous_enrichment
        )
        reuse_beat = (
            not refresh_enrichment
            and previous_beat_stats is not None
            and self._persisted_enrichment_is_fresh(
                previous_enrichment,
                "beat_history",
                enrichment_now,
            )
        )
        if reuse_beat:
            beat_stats = previous_beat_stats
            _mark_persisted("beat_history")
        else:
            fresh_beat_stats = self._compute_historical_beat_stats(symbol_u)
            fresh_beat_snapshot = self._beat_snapshot_from_item(
                {
                    "beat_rate_historical": fresh_beat_stats.get("rate"),
                    "beat_rate_beats": fresh_beat_stats.get("beats"),
                    "beat_rate_samples": fresh_beat_stats.get("samples"),
                    "beat_rate_source": fresh_beat_stats.get("source"),
                }
            )
            if (
                previous_beat_stats is not None
                and (
                    fresh_beat_snapshot is None
                    or _safe_int(fresh_beat_snapshot.get("samples"), 0)
                    < _safe_int(previous_beat_stats.get("samples"), 0)
                )
            ):
                beat_stats = previous_beat_stats
                _mark_persisted("beat_history", retry=True)
            else:
                beat_stats = (
                    fresh_beat_snapshot
                    if fresh_beat_snapshot is not None
                    else fresh_beat_stats
                )
                if fresh_beat_snapshot is not None:
                    _mark_fresh("beat_history")

        enriched_data["beat_rate_historical"] = beat_stats.get("rate")
        # Sample count travels with the rate so compute_direction_bias_score can
        # damp a thin beat-rate sample instead of treating 1/1 as 100% (P0-2).
        enriched_data["beat_rate_samples"] = beat_stats.get("samples")
        enriched_data["recent_reaction_tilt"] = self._compute_recent_reaction_tilt(recent_reactions)

        # eToro retail context feeds direction component c6 AND rides on the
        # item for the UI card / AI prompt — fetch once, before scoring.
        etoro_ctx = self._fetch_etoro_context(symbol_u)
        db_dict = compute_direction_bias_score(
            symbol_u,
            enriched_data,
            news_sentiment,
            retail=etoro_ctx,
            retail_weight=_safe_float(cfg.get("retail_positioning_weight"), 0.10) or 0.0,
            retail_sign=_safe_float(cfg.get("retail_positioning_sign"), 1.0),
            retail_support_rank_ceiling=_safe_int(cfg.get("retail_support_rank_ceiling"), 20000),
        )
        direction_bias = db_dict["bias_score"]
        
        # §50 DATA CONFIDENCE is computed AFTER coverage_score below (the evolved
        # multi-input bands take coverage as a downgrade-only modifier). The
        # local data_confidence / data_guard_msg / is_insufficient_data are set
        # there and preserve the V7 historical_samples==0 -> is_insufficient_data
        # semantics used by the NEWS_ONLY_CALL guard and gap_risk_unavailable flag.
        coverage_score, coverage_label, coverage_contributors = self._compute_analysis_coverage(
            event_present=earnings_dt is not None,
            news_count=news_count,
            news_quality_score=(
                0.55 * min(1.0, float(news_bundle.get("weighted_articles") or 0.0) / 6.0)
                + 0.25 * min(1.0, float(news_bundle.get("unique_sources") or 0.0) / 3.0)
                + 0.20 * min(1.0, float(news_bundle.get("fresh_articles") or 0.0) / 3.0)
            ),
            historical_samples=historical_samples,
            implied_available=implied_score is not None,
            liquidity_available=(market_cap is not None) or (avg_volume is not None),
            earnings_trend_available=earnings_trend is not None,
        )
        # §50 DATA CONFIDENCE — delegated to the canonical multi-input function
        # (coverage is a downgrade-only modifier; is_insufficient_data preserves
        # the V7 historical_samples==0 semantics used by the NEWS_ONLY_CALL guard
        # and the gap_risk_unavailable flag).
        data_confidence, data_guard_msg, is_insufficient_data = compute_data_confidence(
            historical_samples=historical_samples,
            coverage_score=coverage_score,
            implied_available=implied_score is not None,
            news_count=news_count,
        )
        # §50 PRE-EARNINGS SCORE — evolves _compute_opportunity_score into the
        # canonical score (visible components; score is NOT probability). The
        # back-compat `opportunity_score` local is set equal to the canonical score.
        _pe_score_result = compute_pre_earnings_score(
            gap_risk=gap_risk,
            direction_bias=direction_bias,
            coverage_score=coverage_score,
            coverage_contributors=coverage_contributors,
            historical_samples=historical_samples,
            news_count=news_count,
            implied_available=implied_score is not None,
        )
        opportunity_score = _pe_score_result.score

        confidence = self._estimate_confidence_intervals(
            gap_risk=gap_risk,
            direction_bias=direction_bias,
            historical_samples=historical_samples,
            news_count=news_count,
            implied_available=implied_score is not None,
            earnings_trend_available=earnings_trend is not None,
            group_samples=group_samples,
            global_samples=_safe_int((calibration_metrics or {}).get("sample_size"), 0),
        )
        # The direction band's score assumed a fully supported bias. When the
        # share cap applied, most direction inputs were missing: report the
        # lower support instead of the full-input figure.
        if (
            isinstance(db_dict, dict)
            and db_dict.get("input_share_cap_applied")
            and isinstance(confidence.get("direction_bias"), dict)
        ):
            _evidence_factor = _clamp(_safe_float(db_dict.get("evidence_factor"), 1.0) or 0.0, 0.0, 1.0)
            _band = confidence["direction_bias"]
            _band["score"] = round(float(_band.get("score") or 0.0) * _evidence_factor, 4)
            _band["input_share_cap_applied"] = True
            _band["evidence_factor"] = round(_evidence_factor, 4)
            _band["input_coverage_pct"] = round(
                100.0
                * (_safe_float(db_dict.get("available_weight"), 0.0) or 0.0)
                / max(1e-9, _safe_float(db_dict.get("nominal_weight"), 0.0) or 0.0),
                1,
            )
        if is_insufficient_data and isinstance(confidence.get("gap_risk"), dict):
            # GapRisk is published as unavailable for this row; a band around
            # the internal placeholder would leak a number we refuse to show.
            confidence["gap_risk"]["low"] = None
            confidence["gap_risk"]["high"] = None
            confidence["gap_risk"]["unavailable"] = True

        why = self._build_why_bullets(
            symbol=symbol_u,
            gap_risk=gap_risk,
            direction_bias=direction_bias,
            news_count=news_count,
            severity_label=severity_label,
            mismatch=bool(mismatch_info),
            event=event,
            contributors=contributors,
            # Honesty: with no verified historical quarters GapRisk is N/A —
            # the why-bullets must say so instead of printing the internal
            # placeholder int as if it were a real score (audit P0-4).
            gap_risk_unavailable_reason=(data_guard_msg if is_insufficient_data else None),
            direction_components=(
                db_dict.get("components") if isinstance(db_dict, dict) else None
            ),
        )
        what_to_watch = self._build_what_to_watch(
            direction_bias=direction_bias,
            severity_label=severity_label,
            mismatch=bool(mismatch_info),
            event=event,
        )
        bias_reasons = self._build_bias_reasons(direction_bias, news_sentiment, catalyst_flags, analyst_proxy, earnings_trend)

        # NEWS_ONLY_CALL guard (audit P0-2): when GapRisk is unavailable AND
        # fewer than 2 non-news direction components exist, the only real
        # evidence is headline sentiment — a LONG/SHORT label would manufacture
        # conviction the data does not support. Clamp the label to NEUTRAL (the
        # score stays computed for transparency) and badge the row so the UI
        # can be honest about the thin basis. `_synchronize_direction_decision`
        # re-applies the same guard after any AI blend.
        _db_components = db_dict.get("components", {}) if isinstance(db_dict, dict) else {}
        _non_news_components = sum(
            1
            for _key in ("historical", "analyst_revision", "options", "post_earnings_history")
            if _db_components.get(_key) is not None
        )
        _news_only_call = bool(is_insufficient_data and _non_news_components < 2)

        badges = []
        if gap_risk >= 70 and not is_insufficient_data:
            badges.append("HIGH_GAP_RISK")
        if direction_bias <= -40 and not _news_only_call:
            badges.append("SHORT_BIAS")
        if direction_bias >= 40 and not _news_only_call:
            badges.append("LONG_BIAS")
        if _news_only_call:
            badges.append("NEWS_ONLY_CALL")
        if mismatch_info:
            badges.append("CALENDAR_MISMATCH")
        if event_status == "DATE_CONFLICT":
            badges.append("DATE_CONFLICT")
        elif event_status == "UNVERIFIED" or date_confidence == "low":
            badges.append("EVENT_UNVERIFIED")
        if already_reported_info:
            badges.append("ALREADY_REPORTED")
        if not news_rows:
            badges.append("LOW_NEWS_CONTEXT")
        if coverage_label == "LOW":
            badges.append("LOW_COVERAGE")

        # Bands are the documented 0-39 / 40-69 / 70-100 (docs/PRE_EARNINGS_SYSTEM.md
        # and the frontend both use 40 as the med edge); the backend used 45, so a
        # GapRisk of 40-44 rendered "med" in the UI but "low" in the payload.
        gap_risk_level = "high" if gap_risk >= 70 else ("med" if gap_risk >= 40 else "low")
        direction_label = "NEUTRAL" if _news_only_call else direction_label_from_score(direction_bias)
        direction_bias_norm = round(_clamp(float(direction_bias) / 100.0, -1.0, 1.0), 4)
        _direction_computable = (news_count > 0 or abs(direction_bias) > 5)
        confidence_gap = confidence.get("gap_risk", {}) if isinstance(confidence, dict) else {}
        confidence_bias = confidence.get("direction_bias", {}) if isinstance(confidence, dict) else {}
        confidence_score = int(round(_clamp(100.0 * ((float(_safe_float(confidence_gap.get("score"), 0.0) or 0.0) + float(_safe_float(confidence_bias.get("score"), 0.0) or 0.0)) / 2.0), 0.0, 100.0)))
        item_flags = {
            "missing_earnings_date": earnings_dt is None,
            "calendar_news_mismatch": bool(mismatch_info),
            "calendar_date_conflict": event_status == "DATE_CONFLICT",
            "event_unverified": (
                event_status in {"UNVERIFIED", "DATE_CONFLICT"}
                or date_confidence not in {"high", "medium"}
            ),
            "already_reported": bool(already_reported_info),
            "already_reported_evidence": (
                list(already_reported_info.get("headlines", []) or [])[:3]
                if already_reported_info else []
            ),
            "low_news_context": not news_rows,
            "low_coverage": coverage_label == "LOW",
        }

        # Build components dict that frontend expects
        _components = dict(db_dict.get("components", {}))
        if expected_move_pct is not None:
            _components["range_proxy_move"] = round(expected_move_pct, 4)
            # NOTE: `options_implied_move` is intentionally NOT populated here.
            # It used to alias this same recent-range proxy under a name that
            # claims paid options-implied volatility the system does not have
            # (audit P1-5). The key now appears only when a real options feed
            # supplies it; the honest proxy stays under `range_proxy_move`.

        return {
            "symbol": symbol_u,
            "is_watchlist": symbol_u in self._watchlist_symbol_set(),
            "earnings_time_utc": _iso(earnings_dt),
            "session": session,
            "confirmed": bool(confirmed),
            "event_status": event_status,
            "event_source": event_source,
            "event_source_url": (event or {}).get("source_url") or self._resolved_event_source_url({
                "symbol": symbol_u,
                "event_source": event_source,
            }),
            "event_verified_at": (event or {}).get("verified_at"),
            "event_date_confidence": date_confidence,
            "event_session_confidence": (event or {}).get("session_confidence") or "unknown",
            "event_conflicting_dates": list((event or {}).get("conflicting_dates") or []),
            "gap_risk": int(_clamp(gap_risk, 0, 100)) if not is_insufficient_data else None,
            "gap_risk_raw": int(_clamp(gap_risk_raw, 0, 100)) if not is_insufficient_data else None,
            "gap_risk_level": gap_risk_level if not is_insufficient_data else "NONE",
            "gap_risk_numeric": int(_clamp(gap_risk, 0, 100)) if not is_insufficient_data else None,
            # Explicit unavailable flag + reason so the frontend can surface
            # "insufficient history" instead of a silent null. Win-rate-neutral:
            # this only adds a display flag; gap_risk stays null when no data.
            "gap_risk_unavailable": bool(is_insufficient_data),
            "gap_risk_unavailable_reason": (
                data_guard_msg if is_insufficient_data else None
            ),
            "gap_risk_history_source": hist_stats.get("source"),
            "direction_bias": direction_bias_norm if (not is_insufficient_data or _direction_computable) else None,
            "direction_bias_score": int(round(_clamp(direction_bias, -100, 100))) if (not is_insufficient_data or _direction_computable) else None,
            "structured_direction_bias_score": int(round(_clamp(direction_bias, -100, 100))) if (not is_insufficient_data or _direction_computable) else None,
            "final_direction_bias_score": int(round(_clamp(direction_bias, -100, 100))) if (not is_insufficient_data or _direction_computable) else None,
            "direction_label": direction_label if (not is_insufficient_data or _direction_computable) else "INSUFFICIENT_DATA",
            # Additive honesty marker (audit P0-2): True when the directional
            # call rests on news sentiment alone (GapRisk unavailable + fewer
            # than 2 non-news components). The label is clamped to NEUTRAL and
            # the row carries the NEWS_ONLY_CALL badge.
            "news_only_call": bool(_news_only_call),
            "data_confidence": data_confidence,
            "data_message": data_guard_msg,
            "components": _components,
            "direction_components": _components,
            # Real analyst consensus (yfinance) — shown in the Analyst column, drives bias c2
            "analyst_target_upside_pct": enriched_data.get("analyst_target_upside_pct"),
            "analyst_recommendation": enriched_data.get("analyst_recommendation"),
            "analyst_rec_mean": enriched_data.get("analyst_rec_mean"),
            "analyst_count": enriched_data.get("analyst_count"),
            "analyst_consensus_source": value_sources.get("analyst_consensus"),
            "beat_rate_historical": beat_stats.get("rate"),
            "beat_rate_beats": int(_safe_int(beat_stats.get("beats"), 0)),
            "beat_rate_samples": int(_safe_int(beat_stats.get("samples"), 0)),
            "beat_rate_source": beat_stats.get("source"),
            "beat_rate_unavailable_reason": beat_stats.get("unavailable_reason"),
            # Real post-earnings history + our directional track record on this name
            "recent_reactions": recent_reactions,
            "recent_reactions_source": value_sources.get("recent_reactions"),
            "symbol_track_record": self._get_track_record_cached().get("per_symbol", {}).get(symbol_u),
            "track_record_overall": self._get_track_record_cached().get("overall"),
            "range_proxy_pct": round(expected_move_pct, 4) if expected_move_pct is not None else None,
            "expected_move_pct": round(expected_move_pct, 4) if expected_move_pct is not None else None,
            "range_proxy_source": value_sources.get("range_proxy"),
            "historical_median_move_pct": (
                round(historical_median_move_pct, 4) if historical_median_move_pct is not None else None
            ),
            "avg_move_historical": (
                round(historical_median_move_pct, 4) if historical_median_move_pct is not None else None
            ),
            "historical_sample_size": historical_samples,
            "sector": sector,
            # Peer read-through grouping + fresh settled peer evidence (owner
            # requirement, 2026-11). peer_key/peer_key_label are null when the
            # name resolves to no group; peer_readthrough is null when no peer
            # outcome settled inside the window — honest, never an empty fake.
            "peer_key": peer_key_value,
            "peer_key_label": peer_key_label_value,
            "peer_readthrough": peer_readthrough,
            "market_cap": round(market_cap, 2) if market_cap is not None else None,
            "avg_volume": round(avg_volume, 2) if avg_volume is not None else None,
            "market_cap_bucket": cap_bucket,
            "earnings_trend_score": (
                round(earnings_trend, 4)
                if earnings_trend is not None
                else None
            ),
            "coverage_score": int(_clamp(coverage_score, 0, 100)),
            "coverage_label": coverage_label,
            "coverage_contributors": coverage_contributors,
            "opportunity_score": int(_clamp(_pe_score_result.score, 0, 100)),
            "pre_earnings_score": int(_clamp(_pe_score_result.score, 0, 100)),
            "pre_earnings_score_label": _pe_score_result.label,
            "pre_earnings_score_components": _pe_score_result.components,
            "pre_earnings_is_not_probability": True,
            "badges": badges,
            "why": why[:5],
            "what_to_watch": what_to_watch[:5],
            "bias_reasons": bias_reasons[:5],
            "news_count": news_count,
            "news_digest": news_digest,
            "top_news": top_news,
            "contributors": contributors,
            "confidence": confidence,
            # One coherent definition (audit P2-8): confidence_score is always
            # the 0-100 average of the two interval scores above. It used to be
            # forced to 0 for insufficient-data rows while the served
            # `confidence` object still carried scores ~0.4 — two disagreeing
            # answers on the same row (live VECO: 0 vs 0.38-0.43). Thin data
            # already widens the intervals (lowering the scores); the N/A state
            # stays signaled by gap_risk_unavailable / data_confidence=NONE.
            "confidence_score": confidence_score,
            "confidence_interval": {
                "gap_risk": confidence_gap if isinstance(confidence_gap, dict) else {},
                "direction_bias": confidence_bias if isinstance(confidence_bias, dict) else {},
            },
            "flags": item_flags,
            "enrichment_verified_at_utc": verified_at,
            "enrichment_refresh_after_utc": refresh_after,
            "enrichment_value_sources": value_sources,
            "updated_utc": as_of_utc,
            "ollama": None,
            "etoro_context": etoro_ctx,
        }

    def _pick_primary_event(
        self,
        rows: List[Dict[str, Any]],
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        if not rows:
            return {}
        reference_time = now or _utc_now()
        if reference_time.tzinfo is None:
            reference_time = reference_time.replace(tzinfo=timezone.utc)
        else:
            reference_time = reference_time.astimezone(timezone.utc)
        upcoming = []
        for row in rows:
            dt = _parse_iso(row.get("date") or row.get("date_formatted"))
            status = str(row.get("event_status") or "UPCOMING").upper()
            if status in {"REPORTED", "SUPERSEDED"}:
                continue
            if dt and dt >= (reference_time - timedelta(days=1)):
                status_rank = 2 if status == "UPCOMING" else (1 if status == "DATE_CONFLICT" else 0)
                upcoming.append((status_rank, self._event_source_priority(row), dt, row))
        if upcoming:
            upcoming.sort(key=lambda pair: (-pair[0], -pair[1], pair[2]))
            return dict(upcoming[0][3])
        return {}

    def _detect_schedule_conflict(
        self,
        symbol: str,
        event_dt: Optional[datetime],
        event: Optional[Dict[str, Any]],
        news_rows: List[Dict[str, Any]],
        cfg: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        if event_dt is None:
            return None
        if event_dt < (_utc_now() - timedelta(days=1)):
            return None
        if _safe_float((event or {}).get("eps_actual"), None) is not None:
            return None
        if _safe_float((event or {}).get("revenue_actual"), None) is not None:
            return None
        max_news_rows = max(0, _safe_int(cfg.get("schedule_conflict_max_news_rows"), 0))
        if len(news_rows) > max_news_rows:
            return None

        min_spacing_days = max(21, _safe_int(cfg.get("schedule_conflict_min_spacing_days"), 45))
        history = self._get_cached_earnings_history(symbol, max_events=16)
        nearest_prior: Optional[datetime] = None
        for row in history:
            dt = _parse_iso((row or {}).get("date") or (row or {}).get("date_formatted"))
            if dt is None or dt >= event_dt:
                continue
            # Only count rows that have confirmed reported actuals; future
            # calendar entries that happen to sit earlier than the candidate
            # date have no actuals yet and must not be treated as a "prior
            # earnings event" — doing so caused false RECENT_PRIOR_EARNINGS_CONFLICT
            # suppressions for symbols whose calendar had several scheduled
            # dates within a short window.
            has_eps = _safe_float((row or {}).get("eps_actual"), None) is not None
            has_rev = _safe_float((row or {}).get("revenue_actual"), None) is not None
            if not has_eps and not has_rev:
                continue
            if nearest_prior is None or dt > nearest_prior:
                nearest_prior = dt
        if nearest_prior is None:
            return None

        delta_days = (event_dt.date() - nearest_prior.date()).days
        if delta_days <= 0 or delta_days >= min_spacing_days:
            return None
        return {
            "reason": "RECENT_PRIOR_EARNINGS_CONFLICT",
            "previous_earnings_time_utc": _iso(nearest_prior),
            "candidate_earnings_time_utc": _iso(event_dt),
            "delta_days": int(delta_days),
            "news_rows": int(len(news_rows)),
            "min_spacing_days": int(min_spacing_days),
        }

    def _filter_schedule_conflict_items(
        self,
        items: List[Dict[str, Any]],
        cfg: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        filtered: List[Dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol") or "").upper().strip()
            if not symbol:
                continue
            event_dt = _parse_iso(item.get("earnings_time_utc"))
            news_rows = list(item.get("top_news") or [])
            conflict = self._detect_schedule_conflict(
                symbol=symbol,
                event_dt=event_dt,
                event=item,
                news_rows=news_rows,
                cfg=cfg,
            )
            if conflict:
                logger.warning(
                    "Pre-earnings filtered persisted item %s due to schedule conflict: %s",
                    symbol,
                    conflict,
                )
                continue
            filtered.append(item)
        return filtered

    @staticmethod
    def _normalize_session(value: Any) -> str:
        raw = str(value or "").strip().upper()
        if raw in {"BMO", "PRE-MARKET", "PREMARKET", "BEFORE MARKET OPEN", "BEFORE OPEN", "BMO "}:
            return "BMO"
        if raw in {"AMC", "AFTER HOURS", "AFTER-HOURS", "AFTER MARKET CLOSE", "AFTER CLOSE"}:
            return "AMC"
        return "UNKNOWN"

    @staticmethod
    def _news_evidence_id(row: Dict[str, Any]) -> str:
        explicit = str(row.get("id") or "").strip()
        if explicit:
            return explicit[:160]
        url = str(row.get("url") or "").strip().lower().split("#", 1)[0]
        if url:
            return hashlib.sha256(url.encode("utf-8", errors="ignore")).hexdigest()[:20]
        title = re.sub(r"[^a-z0-9]+", " ", str(row.get("title") or "").lower()).strip()
        source = str(row.get("source") or "").strip().lower()
        published = str(row.get("published_at") or row.get("published_utc") or "")[:10]
        seed = f"{title}|{source}|{published}"
        return hashlib.sha256(seed.encode("utf-8", errors="ignore")).hexdigest()[:20]

    @staticmethod
    def _news_materiality_score(
        row: Dict[str, Any],
        *,
        now: Optional[datetime] = None,
    ) -> float:
        """Rank evidence for the AI prompt; this is not directional sentiment."""
        text = _norm_text(_headline_blob(row))
        material_terms = {
            "earnings": 1.2,
            "guidance": 1.6,
            "revenue": 1.0,
            "eps": 1.1,
            "estimate": 0.8,
            "forecast": 1.1,
            "margin": 0.9,
            "demand": 0.8,
            "analyst": 0.6,
            "price target": 0.8,
            "upgrade": 1.1,
            "downgrade": 1.1,
            "offering": 1.8,
            "dilution": 1.8,
            "investigation": 1.7,
            "lawsuit": 1.4,
            "fda": 1.8,
            "contract": 1.2,
            "partnership": 1.0,
            "acquisition": 1.4,
            "merger": 1.4,
            "buyback": 1.1,
            "layoff": 1.1,
            "ceo": 0.9,
        }
        term_score = sum(weight for token, weight in material_terms.items() if token in text)
        published = _parse_iso(row.get("published_at") or row.get("published_utc"))
        current = now or _utc_now()
        freshness = 0.0
        if published is not None:
            age_hours = max(0.0, (current - published).total_seconds() / 3600.0)
            if age_hours <= 6:
                freshness = 2.0
            elif age_hours <= 24:
                freshness = 1.5
            elif age_hours <= 72:
                freshness = 1.0
            elif age_hours <= 168:
                freshness = 0.5
        if str(row.get("summary") or row.get("snippet") or "").strip():
            freshness += 0.2
        return round(float(term_score + freshness), 4)

    @classmethod
    def _prepare_news_evidence(
        cls,
        fresh_rows: List[Dict[str, Any]],
        previous_rows: List[Dict[str, Any]],
        *,
        now: Optional[datetime] = None,
        lookback_hours: int = 336,
        evidence_limit: int = 12,
    ) -> Dict[str, Any]:
        """Deduplicate, expire and diversify the news evidence used by scoring/AI."""
        current = now or _utc_now()
        cutoff = current - timedelta(hours=max(1, int(lookback_hours)))
        limit = max(1, min(20, int(evidence_limit)))
        deduped: Dict[str, Dict[str, Any]] = {}

        def _story_key(title: str, url: str) -> str:
            normalized = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
            # Exact normalized titles remove duplicated URLs/syndication while
            # preserving materially different updates in a developing story.
            if normalized:
                return "story:" + normalized
            return "url:" + url.split("#", 1)[0]

        for is_fresh, source_rows in ((True, fresh_rows or []), (False, previous_rows or [])):
            for raw in source_rows:
                if not isinstance(raw, dict):
                    continue
                row = dict(raw)
                title = str(row.get("title") or "").strip()
                if not title:
                    continue
                published = _parse_iso(row.get("published_at") or row.get("published_utc"))
                # Persisted evidence without a timestamp cannot be proven to be
                # inside the active lookback and must not live forever.
                if published is None and not is_fresh:
                    continue
                if published is not None and published < cutoff:
                    continue
                title_key = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
                url_key = str(row.get("url") or "").strip().lower().split("#", 1)[0]
                key = _story_key(title, url_key) if title_key else url_key
                if not key:
                    continue
                relevance = float(_clamp(_safe_float(row.get("_pe_relevance"), 1.0) or 0.0, 0.0, 1.0))
                row["_pe_relevance"] = round(relevance, 3)
                row["_pe_publisher"] = str(row.get("_pe_publisher") or _news_publisher(row))
                row["_pe_materiality"] = round(
                    cls._news_materiality_score(row, now=current) * (0.25 + 0.75 * relevance),
                    4,
                )
                existing = deduped.get(key)
                if existing is None:
                    deduped[key] = row
                    continue
                existing_dt = _parse_iso(existing.get("published_at") or existing.get("published_utc"))
                existing_summary = str(existing.get("summary") or existing.get("snippet") or "")
                row_summary = str(row.get("summary") or row.get("snippet") or "")
                if (
                    (published is not None and (existing_dt is None or published > existing_dt))
                    or len(row_summary) > len(existing_summary)
                ):
                    deduped[key] = row

        rows = list(deduped.values())
        floor_dt = datetime.min.replace(tzinfo=timezone.utc)
        rows.sort(
            key=lambda row: _parse_iso(row.get("published_at") or row.get("published_utc")) or floor_dt,
            reverse=True,
        )

        selected: List[Dict[str, Any]] = []
        selected_ids = set()
        source_counts: Dict[str, int] = {}

        def _select(row: Dict[str, Any], *, enforce_source_cap: bool) -> None:
            evidence_id = cls._news_evidence_id(row)
            if evidence_id in selected_ids or len(selected) >= limit:
                return
            source = str(row.get("_pe_publisher") or _news_publisher(row)).strip().lower() or "unknown"
            if enforce_source_cap and source_counts.get(source, 0) >= 3:
                return
            selected.append(row)
            selected_ids.add(evidence_id)
            source_counts[source] = source_counts.get(source, 0) + 1

        # Always preserve the newest two facts, then use materiality + freshness
        # with a per-source cap so one syndicated feed cannot crowd out every
        # independent source.
        relevant_rows = [
            row for row in rows
            if float(_safe_float(row.get("_pe_relevance"), 1.0) or 0.0) >= 0.65
        ]
        for row in relevant_rows[:2]:
            _select(row, enforce_source_cap=False)
        ranked_material = sorted(
            rows,
            key=lambda row: (
                float(_safe_float(row.get("_pe_materiality"), 0.0) or 0.0),
                _parse_iso(row.get("published_at") or row.get("published_utc")) or floor_dt,
            ),
            reverse=True,
        )
        for row in ranked_material:
            _select(row, enforce_source_cap=True)
        if len(selected) < limit:
            for row in rows:
                _select(row, enforce_source_cap=False)

        selected_fingerprint_rows = sorted(
            (
                cls._news_evidence_id(row),
                str(row.get("title") or "").strip().lower(),
                str(row.get("published_at") or row.get("published_utc") or ""),
            )
            for row in selected
        )
        fingerprint = hashlib.sha256(
            json.dumps(
                selected_fingerprint_rows,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8", errors="ignore")
        ).hexdigest()[:24] if selected_fingerprint_rows else None
        dated = [
            _parse_iso(row.get("published_at") or row.get("published_utc"))
            for row in rows
        ]
        dated = [value for value in dated if value is not None]
        trusted_rows = relevant_rows
        sources = {
            str(row.get("_pe_publisher") or _news_publisher(row)).strip().lower()
            for row in trusted_rows
            if str(row.get("_pe_publisher") or _news_publisher(row)).strip()
        }
        trusted_dated = [
            _parse_iso(row.get("published_at") or row.get("published_utc"))
            for row in trusted_rows
        ]
        trusted_dated = [value for value in trusted_dated if value is not None]
        fresh_cutoff = current - timedelta(hours=72)
        weighted_articles = 0.0
        for row in trusted_rows:
            published = _parse_iso(row.get("published_at") or row.get("published_utc"))
            if published is None:
                continue
            age_hours = max(0.0, (current - published).total_seconds() / 3600.0)
            weighted_articles += math.exp(-age_hours / 72.0)
        return {
            "rows": rows,
            "evidence_rows": selected,
            "article_count": len(rows),
            "relevant_articles": len(trusted_rows),
            "fresh_articles": sum(1 for value in trusted_dated if value >= fresh_cutoff),
            "weighted_articles": round(weighted_articles, 4),
            "unique_sources": len(sources),
            "material_articles": sum(
                1
                for row in rows
                if float(_safe_float(row.get("_pe_materiality"), 0.0) or 0.0) >= 2.5
            ),
            "newest_utc": _iso(max(trusted_dated)) if trusted_dated else None,
            "oldest_utc": _iso(min(dated)) if dated else None,
            "fingerprint": fingerprint,
        }

    @classmethod
    def _compact_news_row(cls, row: Dict[str, Any]) -> Dict[str, Any]:
        summary = str(row.get("summary") or row.get("snippet") or "").strip()
        summary = re.sub(r"<[^>]+>", " ", summary)
        summary = re.sub(r"\s+", " ", summary).strip()
        return {
            "id": cls._news_evidence_id(row),
            "published_utc": row.get("published_at") or row.get("published_utc"),
            "source": row.get("source"),
            "publisher": row.get("_pe_publisher") or _news_publisher(row),
            "title": row.get("title"),
            "url": row.get("url"),
            "summary": summary[:320] or None,
            "is_crypto": bool(row.get("is_crypto")),
            "materiality_score": round(
                float(_safe_float(row.get("_pe_materiality"), 0.0) or 0.0),
                3,
            ),
            "relevance_score": round(
                float(_safe_float(row.get("_pe_relevance"), 1.0) or 0.0),
                3,
            ),
        }

    def _score_catalyst_severity(self, news_rows: List[Dict[str, Any]]) -> Tuple[float, str]:
        best = 0.3
        label = "EARNINGS_VOLATILITY"
        for row in news_rows:
            text = _norm_text(_headline_blob(row))
            for token, sev in NEGATIVE_SEVERITY_TERMS.items():
                if token in text and sev > best:
                    best = sev
                    label = "NEGATIVE_CATALYST"
            for token, sev in POSITIVE_SEVERITY_TERMS.items():
                if token in text and sev > best:
                    best = sev
                    label = "POSITIVE_CATALYST"
        return float(_clamp(best, 0.0, 1.0)), label

    @staticmethod
    def _score_liquidity_risk(market_cap: Optional[float], avg_volume: Optional[float]) -> float:
        cap = _safe_float(market_cap, None)
        vol = _safe_float(avg_volume, None)
        if cap is None or vol is None:
            return 0.4
        if cap < 500_000_000 and vol < 2_000_000:
            return 1.0
        if cap < 2_000_000_000 and vol < 5_000_000:
            return 0.7
        if cap < 10_000_000_000 and vol < 10_000_000:
            return 0.5
        return 0.2

    def _score_news_sentiment(self, news_rows: List[Dict[str, Any]]) -> float:
        if not news_rows:
            return 0.0
        score = 0.0
        hits = 0
        for row in news_rows[:20]:
            text = _norm_text(_headline_blob(row))
            local = 0.0
            for term, weight in POSITIVE_SENTIMENT_TERMS.items():
                if term in text:
                    local += weight
                    hits += 1
            for term, weight in NEGATIVE_SENTIMENT_TERMS.items():
                if term in text:
                    local += weight
                    hits += 1
            score += local
        if hits <= 0:
            return 0.0
        return float(_clamp(score / max(3.0, hits), -1.0, 1.0))

    def _score_analyst_revision_proxy(self, news_rows: List[Dict[str, Any]]) -> float:
        if not news_rows:
            return 0.0
        score = 0.0
        for row in news_rows[:20]:
            text = _norm_text(_headline_blob(row))
            if any(term in text for term in ANALYST_POSITIVE_TERMS):
                score += 0.35
            if any(term in text for term in ANALYST_NEGATIVE_TERMS):
                score -= 0.35
        return float(_clamp(score, -1.0, 1.0))

    @staticmethod
    def _coverage_news_score(news_count: int) -> float:
        n = max(0, int(news_count))
        if n >= 6:
            return 1.0
        if n >= 3:
            return 0.7
        if n >= 1:
            return 0.4
        return 0.0

    @staticmethod
    def _coverage_history_score(historical_samples: int) -> float:
        n = max(0, int(historical_samples))
        # Four quarterly reports are a complete trailing-year sample and match
        # the one-year daily-price window used by the real gap calculation.
        if n >= 4:
            return 1.0
        if n >= 3:
            return 0.8
        if n >= 1:
            return 0.5
        return 0.0

    def _compute_analysis_coverage(
        self,
        *,
        event_present: bool,
        news_count: int,
        news_quality_score: Optional[float] = None,
        historical_samples: int,
        implied_available: bool,
        liquidity_available: bool,
        earnings_trend_available: bool,
    ) -> Tuple[int, str, List[Dict[str, Any]]]:
        components: List[Tuple[str, float, float, str]] = [
            ("event_window", 1.0 if event_present else 0.0, 0.20, "Earnings event exists in active lookahead window"),
            (
                "news_context",
                float(_clamp(
                    self._coverage_news_score(news_count) if news_quality_score is None else news_quality_score,
                    0.0,
                    1.0,
                )),
                0.30,
                "Relevant, recent, publisher-diverse news evidence",
            ),
            ("historical_context", self._coverage_history_score(historical_samples), 0.20, "Historical earnings move sample depth"),
            ("range_proxy_context", 1.0 if implied_available else 0.0, 0.15, "Recent intraday range proxy present in local cache"),
            ("liquidity_context", 1.0 if liquidity_available else 0.0, 0.10, "Liquidity/fundamental proxies present in cache"),
            ("earnings_trend_context", 1.0 if earnings_trend_available else 0.0, 0.05, "EPS/revenue growth trend context available"),
        ]
        weighted = sum(value * weight for _name, value, weight, _note in components)
        score = int(round(100.0 * _clamp(weighted, 0.0, 1.0)))
        if score >= 70:
            label = "HIGH"
        elif score >= 45:
            label = "MEDIUM"
        else:
            label = "LOW"
        contributors = [
            {
                "name": name,
                "value": round(float(value), 4),
                "weight": round(float(weight), 4),
                "note": note,
            }
            for name, value, weight, note in components
        ]
        return score, label, contributors

    @staticmethod
    def _compute_opportunity_score(gap_risk: int, direction_bias: int, coverage_score: int) -> int:
        signal_strength = (0.65 * float(_safe_int(gap_risk, 0))) + (0.35 * abs(float(_safe_int(direction_bias, 0))))
        weighted = signal_strength * (float(_safe_int(coverage_score, 0)) / 100.0)
        return int(round(_clamp(weighted, 0.0, 100.0)))

    @staticmethod
    def _item_news_counts(item: Dict[str, Any]) -> Tuple[int, int]:
        """Return article/source counts, including compatibility with legacy snapshots."""
        digest = item.get("news_digest") if isinstance(item.get("news_digest"), dict) else {}
        rows = [row for row in (item.get("top_news") or []) if isinstance(row, dict)]

        raw_article_count = digest.get("article_count")
        if raw_article_count is None:
            raw_article_count = item.get("news_count")
        if raw_article_count is None:
            raw_article_count = len(rows)

        raw_source_count = digest.get("unique_sources")
        if raw_source_count is None:
            raw_source_count = len(
                {
                    str(row.get("source") or "").strip().casefold()
                    for row in rows
                    if str(row.get("source") or "").strip()
                }
            )
        return max(0, _safe_int(raw_article_count, 0)), max(0, _safe_int(raw_source_count, 0))

    def _event_gate_blockers(
        self,
        item: Dict[str, Any],
        *,
        now: Optional[datetime] = None,
        require_verified_provenance: bool = False,
    ) -> List[str]:
        """Single calendar/event truth gate shared by every recommendation surface."""
        current = now or _utc_now()
        blockers: List[str] = []
        flags = item.get("flags") if isinstance(item.get("flags"), dict) else {}
        badges = {str(value) for value in (item.get("badges") or [])}
        if flags.get("already_reported") or "ALREADY_REPORTED" in badges:
            blockers.append("already_reported")
        if flags.get("calendar_date_conflict") or "DATE_CONFLICT" in badges:
            blockers.append("calendar_date_conflict")

        status = str(item.get("event_status") or "").strip().upper()
        if status in {"REPORTED", "SUPERSEDED"}:
            blockers.append("event_already_passed")
        elif status in {"DATE_CONFLICT", "UNVERIFIED"}:
            blockers.append("event_unverified")
        confidence = str(item.get("event_date_confidence") or "").strip().lower()
        if confidence == "low":
            blockers.append("event_date_low_confidence")

        event_dt = _parse_iso(item.get("earnings_time_utc"))
        session = self._normalize_session(item.get("session"))
        if event_dt is None:
            blockers.append("missing_event_time")
        elif self._earnings_reported_status(event_dt, session, current):
            blockers.append("event_already_passed")
        # Built rows always carry session confidence. Legacy/test rows do not,
        # so only an explicit unknown is blocked for backward compatibility.
        session_confidence = str(item.get("event_session_confidence") or "").strip().lower()
        if session == "UNKNOWN" and session_confidence:
            blockers.append("event_session_unverified")
        if require_verified_provenance:
            if status != "UPCOMING":
                blockers.append("verified_event_required")
            if not str(item.get("event_source") or "").strip():
                blockers.append("event_source_required")
            if not self._resolved_event_source_url(item):
                blockers.append("event_source_url_required")
            if _parse_iso(item.get("event_verified_at")) is None:
                blockers.append("event_verification_timestamp_required")
            if confidence not in {"medium", "high"}:
                blockers.append("verified_event_date_confidence_required")
            if session_confidence not in {"provider", "cross_provider"}:
                blockers.append("verified_event_session_confidence_required")
            if session == "UNKNOWN":
                blockers.append("verified_event_session_required")
        return list(dict.fromkeys(blockers))

    @staticmethod
    def _resolved_event_source_url(item: Dict[str, Any]) -> Optional[str]:
        explicit = str((item or {}).get("event_source_url") or "").strip()
        if explicit:
            parsed = urlparse(explicit)
            if parsed.scheme in {"http", "https"} and parsed.netloc:
                return explicit
            return None
        symbol = str((item or {}).get("symbol") or "").upper().strip()
        source = str((item or {}).get("event_source") or "").lower().strip()
        if not symbol or not re.fullmatch(r"[A-Z]{1,6}(?:\.[A-Z])?", symbol):
            return None
        if "yfinance" in source or "yahoo" in source:
            return f"https://finance.yahoo.com/quote/{symbol}/analysis/"
        if "finnhub" in source:
            return "https://finnhub.io/"
        return None

    def _prediction_score_details(
        self,
        item: Dict[str, Any],
        cfg: Dict[str, Any],
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Evidence-weighted ranking for trade triage, never a win probability."""
        current = now or _utc_now()
        require_verified_event = bool(cfg.get("top_prediction_require_verified_event", True))
        blockers: List[str] = self._event_gate_blockers(
            item,
            now=current,
            require_verified_provenance=require_verified_event,
        )
        flags = item.get("flags") if isinstance(item.get("flags"), dict) else {}
        event_dt = _parse_iso(item.get("earnings_time_utc"))
        session = self._normalize_session(item.get("session"))
        # Fail closed during startup/schema migration: a legacy cached row can
        # have a future timestamp but no canonical provider reconciliation.
        # It remains visible for audit, but cannot become a trading call until
        # the current calendar pipeline has marked it UPCOMING.
        direction = str(item.get("direction_label") or "").strip().upper()
        if direction not in {"LONG", "SHORT"}:
            blockers.append("no_directional_edge")
        bias = abs(float(_safe_float(item.get("direction_bias_score"), 0.0) or 0.0))
        min_bias = max(0, _safe_int(cfg.get("top_prediction_min_abs_bias"), 20))
        if bias < min_bias:
            blockers.append("direction_below_threshold")

        coverage = float(_safe_float(item.get("coverage_score"), 0.0) or 0.0)
        min_coverage = max(0, _safe_int(cfg.get("top_prediction_min_coverage"), 45))
        if coverage < min_coverage:
            blockers.append("coverage_below_threshold")

        gap_risk = _safe_float(item.get("gap_risk_numeric"), None)
        if gap_risk is None:
            gap_risk = _safe_float(item.get("gap_risk"), None)
        if gap_risk is None:
            blockers.append("gap_risk_unavailable")

        confidence = float(_safe_float(item.get("confidence_score"), 0.0) or 0.0)
        confidence = float(_clamp(confidence, 0.0, 100.0))
        opportunity = float(_safe_float(item.get("opportunity_score"), 0.0) or 0.0)

        ai = item.get("ai_decision") if isinstance(item.get("ai_decision"), dict) else None
        ai_used = bool(ai and ai.get("used"))
        news_update_pending = bool(
            ai_used
            and bool(cfg.get("news_change_reanalysis", True))
            and self._ai_verdict_stale_by_news_evidence(item, ai)
        )
        ai_confidence = float(
            _clamp(_safe_float((ai or {}).get("confidence"), 0.0) or 0.0, 0.0, 1.0)
        )
        ai_verdict = str((ai or {}).get("verdict") or "NEUTRAL").strip().upper()
        if not _ai_decision_qualifies_pre_earnings(ai):
            blockers.append("ai_rejected_event_eligibility")
        if bool(cfg.get("top_prediction_require_ai", True)) and not ai_used:
            blockers.append("ai_review_required")
        if ai_used and ai_verdict == "NEUTRAL":
            blockers.append("ai_no_directional_edge")
        if not ai_used:
            ai_component = 30.0
        elif news_update_pending:
            ai_component = 20.0
        elif ai_verdict == direction:
            ai_component = 60.0 + (40.0 * ai_confidence)
        elif ai_verdict == "NEUTRAL":
            ai_component = 45.0
        else:
            ai_component = 5.0

        track = item.get("symbol_track_record") if isinstance(item.get("symbol_track_record"), dict) else {}
        track_total = max(0, _safe_int(track.get("total"), 0))
        track_hits = max(0, min(track_total, _safe_int(track.get("hits"), 0)))
        # Beta(2,2) shrinkage prevents a 1/1 ticker history from outranking a
        # deep sample with a sustainable edge.
        track_component = 100.0 * ((track_hits + 2.0) / (track_total + 4.0))

        article_count, source_count = self._item_news_counts(item)
        digest = item.get("news_digest") if isinstance(item.get("news_digest"), dict) else {}
        weighted_articles = float(_safe_float(digest.get("weighted_articles"), article_count) or 0.0)
        fresh_articles = max(0, _safe_int(digest.get("fresh_articles"), 0))
        min_articles = max(0, _safe_int(cfg.get("top_prediction_min_news_articles"), 3))
        min_sources = max(0, _safe_int(cfg.get("top_prediction_min_news_sources"), 1))
        if article_count < min_articles:
            blockers.append("insufficient_relevant_news")
        if source_count < min_sources:
            blockers.append("insufficient_news_sources")
        if ai_used and (ai or {}).get("news_articles_read") is not None:
            if _safe_int((ai or {}).get("news_articles_read"), 0) < min_articles:
                blockers.append("ai_read_too_few_articles")
        if ai_used and (ai or {}).get("news_sources_read") is not None:
            if _safe_int((ai or {}).get("news_sources_read"), 0) < min_sources:
                blockers.append("ai_read_too_few_sources")
        newest_news = _parse_iso(digest.get("newest_utc"))
        if newest_news is None:
            row_dates = [
                _parse_iso(row.get("published_utc") or row.get("published_at"))
                for row in (item.get("top_news") or [])
                if isinstance(row, dict)
            ]
            row_dates = [value for value in row_dates if value is not None]
            newest_news = max(row_dates) if row_dates else None
            if row_dates and "fresh_articles" not in digest:
                fresh_articles = sum(
                    1 for value in row_dates
                    if (current - value).total_seconds() <= 72 * 3600
                )
        max_news_age = max(1, _safe_int(cfg.get("top_prediction_max_news_age_hours"), 96))
        if newest_news is None or (current - newest_news).total_seconds() > max_news_age * 3600:
            blockers.append("stale_news_context")
        if news_update_pending:
            blockers.append("ai_news_refresh_pending")
        news_component = (
            50.0 * min(1.0, weighted_articles / 6.0)
            + 30.0 * min(1.0, source_count / 3.0)
            + 20.0 * min(1.0, fresh_articles / 3.0)
        )

        delta_days = self._effective_event_delta_days(event_dt, session, current)
        if delta_days is None:
            urgency_component = 0.0
        elif delta_days <= 1.0:
            urgency_component = 100.0
        else:
            urgency_component = max(20.0, 100.0 - (8.0 * min(delta_days, 10.0)))

        breakdown = {
            "direction": round(bias, 2),
            "coverage": round(float(_clamp(coverage, 0.0, 100.0)), 2),
            "confidence": round(confidence, 2),
            "gap_opportunity": round(float(_clamp(gap_risk or 0.0, 0.0, 100.0)), 2),
            "ai_alignment": round(ai_component, 2),
            "ticker_track_record": round(track_component, 2),
            "news_breadth": round(news_component, 2),
            "event_urgency": round(urgency_component, 2),
        }
        weighted = (
            0.22 * breakdown["direction"]
            + 0.18 * breakdown["coverage"]
            + 0.14 * breakdown["confidence"]
            + 0.12 * breakdown["gap_opportunity"]
            + 0.14 * breakdown["ai_alignment"]
            + 0.08 * breakdown["ticker_track_record"]
            + 0.07 * breakdown["news_breadth"]
            + 0.05 * breakdown["event_urgency"]
        )
        penalties = 0.0
        if str(item.get("decision_alignment") or "").upper() == "CONFLICT":
            penalties += 12.0
        if article_count == 0:
            penalties += 8.0
        if flags.get("calendar_news_mismatch"):
            penalties += 8.0
        if news_update_pending:
            penalties += 8.0
        score = int(round(_clamp(weighted - penalties, 0.0, 100.0)))
        min_score = max(0, min(100, _safe_int(cfg.get("top_prediction_min_trader_score"), 60)))
        if score < min_score:
            blockers.append("trader_score_below_threshold")
        if score >= 80:
            band = "A"
        elif score >= 70:
            band = "B"
        elif score >= 60:
            band = "C"
        else:
            band = "WATCH"
        return {
            "eligible": not blockers,
            "score": score,
            "band": band,
            "breakdown": breakdown,
            "penalties": round(penalties, 2),
            "blockers": list(dict.fromkeys(blockers)),
            "ai_reviewed": ai_used and not news_update_pending,
            "news_update_pending": news_update_pending,
            "opportunity_score": round(opportunity, 2),
        }

    def _refresh_prediction_scores(
        self,
        items: List[Dict[str, Any]],
        cfg: Dict[str, Any],
        *,
        now: Optional[datetime] = None,
    ) -> None:
        current = now or _utc_now()
        for item in items or []:
            if not isinstance(item, dict):
                continue
            details = self._prediction_score_details(item, cfg, now=current)
            item["trader_score"] = details["score"]
            item["trader_score_band"] = details["band"]
            item["trader_score_breakdown"] = details["breakdown"]
            item["trader_score_penalties"] = details["penalties"]
            item["recommendation_eligible"] = bool(details["eligible"])
            item["recommendation_blockers"] = details["blockers"]
            item["news_update_pending"] = bool(details["news_update_pending"])

    def _build_top_predictions(
        self,
        items: List[Dict[str, Any]],
        cfg: Dict[str, Any],
        *,
        now: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        current = now or _utc_now()
        self._refresh_prediction_scores(items, cfg, now=current)
        eligible = [
            item
            for item in (items or [])
            if (
                isinstance(item, dict)
                and bool(item.get("recommendation_eligible"))
                and _safe_int(item.get("trader_score"), 0) >= max(0, min(100, _safe_int(cfg.get("top_prediction_min_trader_score"), 60)))
                and (event_dt := _parse_iso(item.get("earnings_time_utc"))) is not None
                and current.date()
                <= event_dt.date()
                <= current.date() + timedelta(days=max(1, _safe_int(cfg.get("lookahead_days"), 30)))
            )
        ]
        # Conviction is primary. For an equal Trader Score the nearer earnings
        # event wins (DELL before BB); there are no per-day allocation quotas.
        eligible.sort(
            key=lambda item: (
                -_safe_int(item.get("trader_score"), 0),
                _parse_iso(item.get("earnings_time_utc"))
                or datetime.max.replace(tzinfo=timezone.utc),
                -int(bool(
                    isinstance(item.get("ai_decision"), dict)
                    and item["ai_decision"].get("used")
                    and not item.get("news_update_pending")
                )),
                -_safe_int(item.get("coverage_score"), 0),
                -_safe_int(item.get("opportunity_score"), 0),
                -abs(_safe_int(item.get("direction_bias_score"), 0)),
                str(item.get("symbol") or ""),
            ),
        )
        limit = max(1, min(20, _safe_int(cfg.get("top_predictions_limit"), 10)))
        selected = eligible[:limit]
        out: List[Dict[str, Any]] = []
        for rank, item in enumerate(selected, start=1):
            ai = item.get("ai_decision") if isinstance(item.get("ai_decision"), dict) else {}
            digest = item.get("news_digest") if isinstance(item.get("news_digest"), dict) else {}
            article_count, source_count = self._item_news_counts(item)
            reason_candidates = [
                str(ai.get("rationale") or "").strip(),
                *[str(value or "").strip() for value in (item.get("why") or [])],
            ]
            rationale = next((value for value in reason_candidates if value), "Evidence-weighted setup.")
            out.append(
                {
                    "rank": rank,
                    "symbol": str(item.get("symbol") or "").upper(),
                    "side": str(item.get("direction_label") or "").upper(),
                    "trader_score": _safe_int(item.get("trader_score"), 0),
                    "score_band": item.get("trader_score_band"),
                    "earnings_time_utc": item.get("earnings_time_utc"),
                    "session": item.get("session"),
                    "event_status": item.get("event_status"),
                    "event_source": item.get("event_source"),
                    "event_source_url": item.get("event_source_url"),
                    "event_verified_at": item.get("event_verified_at"),
                    "event_date_confidence": item.get("event_date_confidence"),
                    "event_session_confidence": item.get("event_session_confidence"),
                    "event_conflicting_dates": copy.deepcopy(item.get("event_conflicting_dates") or []),
                    "gap_risk": item.get("gap_risk_numeric"),
                    "direction_score": item.get("direction_bias_score"),
                    "coverage_score": item.get("coverage_score"),
                    "confidence_score": item.get("confidence_score"),
                    "opportunity_score": item.get("opportunity_score"),
                    "ai_reviewed": bool(ai.get("used")) and not bool(item.get("news_update_pending")),
                    "ai_verdict": ai.get("verdict") if ai.get("used") else None,
                    "ai_confidence": ai.get("confidence") if ai.get("used") else None,
                    "ai_generated_at": ai.get("generated_at") if ai.get("used") else None,
                    "news_update_pending": bool(item.get("news_update_pending")),
                    "news_articles": article_count,
                    "news_sources": source_count,
                    "news_newest_utc": digest.get("newest_utc"),
                    "recommendation_eligible": bool(item.get("recommendation_eligible")),
                    "recommendation_blockers": list(item.get("recommendation_blockers") or []),
                    "execution_state": item.get("execution_state"),
                    "execution_eligible_now": bool(item.get("execution_eligible_now")),
                    "position_direction_conflict": bool(item.get("position_direction_conflict")),
                    "active_position_context": copy.deepcopy(item.get("active_position_context") or {}),
                    "rationale": rationale[:360],
                    "score_breakdown": copy.deepcopy(item.get("trader_score_breakdown") or {}),
                }
            )
        return out

    def _attach_pre_earnings_decisions(
        self,
        cfg: Dict[str, Any],
        *,
        items: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        """§51 — attach a Pre-Earnings decision summary to each item.

        ``run_cycle`` passes the already-normalized Pre-Earnings subsection,
        while direct callers may pass the complete application config. Resolve
        both shapes here and always give the decision engine the complete config
        so autonomy, AI/Critic, Risk, and Capital policies remain available.
        Non-fatal: a per-item error leaves the item with a WATCH decision rather
        than failing the cycle.
        """
        supplied_cfg = cfg if isinstance(cfg, dict) else {}
        nested_cfg = supplied_cfg.get("pre_earnings")
        if isinstance(nested_cfg, dict):
            pe_cfg = nested_cfg
            decision_cfg = supplied_cfg
        else:
            pe_cfg = supplied_cfg
            manager_cfg = getattr(getattr(self, "config_manager", None), "config", None)
            decision_cfg = manager_cfg if isinstance(manager_cfg, dict) else {"pre_earnings": pe_cfg}
        target_items = items if isinstance(items, list) else (self._state.get("items") or [])
        if not bool(pe_cfg.get("decision_enabled", False)):
            return target_items
        max_tickers = max(0, _safe_int(pe_cfg.get("decision_max_tickers"), 25))
        # An answer is kept while the question is the same. Asking again on every
        # cycle spent about 150 model calls an hour, day and night, on unchanged
        # tickers. 0 = ask on every cycle.
        reuse_minutes = max(0, _safe_int(pe_cfg.get("decision_reuse_minutes"), 120))
        earlier_answers: Dict[str, Dict[str, Any]] = {}
        if reuse_minutes:
            for saved in list((getattr(self, "_state", None) or {}).get("items") or []):
                if isinstance(saved, dict) and isinstance(saved.get("pre_earnings_decision"), dict):
                    earlier_answers[str(saved.get("symbol") or "").upper().strip()] = saved["pre_earnings_decision"]
        processed = 0
        for item in target_items:
            if not isinstance(item, dict) or not item.get("symbol"):
                continue
            if processed >= max_tickers:
                break
            if item.get("recommendation_eligible") is False:
                blockers = [
                    str(value).strip()
                    for value in (item.get("recommendation_blockers") or [])
                    if str(value).strip()
                ]
                item["pre_earnings_decision"] = {
                    "action": "WATCH",
                    "recommendation": "avoid",
                    "confidence": None,
                    "available": False,
                    "reason": "recommendation_ineligible"
                    + (f": {', '.join(blockers[:4])}" if blockers else ""),
                    "validation_stage": "PRE_EARNINGS_PREFILTER",
                    "critic_veto": False,
                }
                # The prefilter verdict costs nothing. Counting it against
                # decision_max_tickers let the first 25 ineligible rows (the
                # list is date-ordered) use up the quota, so eligible setups
                # further down never received a decision at all.
                continue
            try:
                fingerprint = decision_inputs_fingerprint(item, decision_cfg)
            except Exception as exc:  # noqa: BLE001 - without a fingerprint the model is simply asked
                logger.debug("Pre-Earnings decision fingerprint failed for %s: %s", item.get("symbol"), exc)
                fingerprint = ""
            own_answer = item.get("pre_earnings_decision")
            kept = self._reusable_pre_earnings_decision(
                own_answer if isinstance(own_answer, dict)
                else earlier_answers.get(str(item.get("symbol") or "").upper().strip()),
                fingerprint,
                reuse_minutes,
            )
            if kept is not None:
                item["pre_earnings_decision"] = kept
                # Counted, so that the calls saved here are not spent further down the list.
                processed += 1
                continue
            try:
                result = evaluate_pre_earnings_decision(item, config=decision_cfg, services=None)
                review = result.review or {}
                item["pre_earnings_decision"] = {
                    "action": result.decision.action.value if result.decision else "WATCH",
                    "recommendation": review.get("recommendation", "watch"),
                    "confidence": review.get("confidence"),
                    "available": bool(review.get("available")),
                    "reason": review.get("reason", ""),
                    "validation_stage": review.get("validation_stage", ""),
                    "critic_veto": bool(review.get("critic_veto")),
                    "inputs_fingerprint": fingerprint,
                    "decided_at": _iso(_utc_now()),
                }
            except Exception as exc:
                logger.warning(f"Pre-Earnings decision attach failed for {item.get('symbol')}: {exc}")
                item["pre_earnings_decision"] = {
                    "action": "WATCH", "recommendation": "watch", "confidence": None,
                    "available": False, "reason": f"decision_error:{exc}",
                    "validation_stage": "", "critic_veto": False,
                }
            processed += 1
        return target_items

    @staticmethod
    def _reusable_pre_earnings_decision(
        earlier: Any, fingerprint: str, reuse_minutes: int,
    ) -> Optional[Dict[str, Any]]:
        """The earlier answer, when it answered this very question recently enough.

        An answer is kept only when the model really gave it (``available``),
        its inputs are the ones at hand (same fingerprint) and it is younger
        than ``reuse_minutes``; the age limit bounds what the fingerprint does
        not see (live order-flow evidence). Anything else is asked again.
        """
        if reuse_minutes <= 0 or not fingerprint or not isinstance(earlier, dict):
            return None
        if not earlier.get("available") or earlier.get("inputs_fingerprint") != fingerprint:
            return None
        decided_at = _parse_iso(earlier.get("decided_at"))
        if decided_at is None:
            return None
        age_seconds = (_utc_now() - decided_at).total_seconds()
        if age_seconds < 0 or age_seconds > reuse_minutes * 60:
            return None
        return copy.deepcopy(earlier)

    def _attach_direction_track_records(self, cfg: Dict[str, Any]) -> None:
        """Attach settled side calibration to every item before persistence."""
        try:
            metrics = self._compute_track_record_metrics(cfg)
            per_direction = metrics.get("per_direction") if isinstance(metrics, dict) else {}
            if not isinstance(per_direction, dict):
                per_direction = {}
            for item in self._state.get("items") or []:
                if not isinstance(item, dict):
                    continue
                direction = str(item.get("direction_label") or "").upper().strip()
                record = per_direction.get(direction)
                item["direction_track_record"] = copy.deepcopy(record) if isinstance(record, dict) else {
                    "n": 0,
                    "hits": 0,
                    "hit_rate": None,
                    "directional": direction in {"LONG", "SHORT"},
                }
        except Exception as exc:
            logger.warning("Pre-Earnings direction calibration attach failed: %s", exc)

    def _attach_daily_ranking(
        self,
        payload: Dict[str, Any],
        cfg: Dict[str, Any],
        *,
        universe_event_count: Optional[int] = None,
        universe_symbols: Optional[set[str]] = None,
        scored_count: Optional[int] = None,
        news_articles_scanned: Optional[int] = None,
    ) -> None:
        if not isinstance(payload, dict):
            return
        now = _utc_now()
        status = payload.get("status") if isinstance(payload.get("status"), dict) else {}
        as_of = _latest_pre_earnings_snapshot_time(payload)
        age_seconds = max(0, int((now - as_of).total_seconds())) if as_of else None
        max_age = max(600, _safe_int(cfg.get("recommendation_max_age_seconds"), 1800))
        stale = age_seconds is None or age_seconds > max_age
        items = [item for item in (payload.get("items") or []) if isinstance(item, dict)]
        predictions = self._build_top_predictions(items, cfg, now=now)
        if stale:
            predictions = []

        previous_ranking = payload.get("ranking") if isinstance(payload.get("ranking"), dict) else {}
        payload["top_predictions"] = predictions
        reported_rows = [
            row for row in (payload.get("reported_today") or []) if isinstance(row, dict)
        ]
        rejected_rows = [
            row for row in (payload.get("calendar_validation_rejections") or [])
            if isinstance(row, dict)
        ]
        active_symbols = {
            str(symbol or "").upper().strip()
            for symbol in (universe_symbols or set())
            if str(symbol or "").strip()
        }
        preserve_exact_coverage = (
            not active_symbols
            and universe_event_count is None
            and previous_ranking.get("coverage_basis") == "active_calendar_symbols"
        )
        if active_symbols:
            scored_symbols = {
                str(item.get("symbol") or "").upper().strip()
                for item in items
                if str(item.get("symbol") or "").upper().strip() in active_symbols
            }
            rejected_symbols = {
                str(row.get("symbol") or "").upper().strip()
                for row in (reported_rows + rejected_rows)
                if str(row.get("symbol") or "").upper().strip() in active_symbols
            } - scored_symbols
            effective_universe_count = len(active_symbols)
            effective_scored_count = len(scored_symbols)
            screened_out_count = len(rejected_symbols)
            covered_count = len(scored_symbols | rejected_symbols)
            coverage_basis = "active_calendar_symbols"
            coverage_rows = [
                row for row in (items + reported_rows + rejected_rows)
                if str(row.get("symbol") or "").upper().strip() in active_symbols
            ]
        elif preserve_exact_coverage:
            effective_universe_count = _safe_int(previous_ranking.get("universe_event_count"), len(items))
            effective_scored_count = _safe_int(previous_ranking.get("scored_count"), len(items))
            screened_out_count = _safe_int(previous_ranking.get("screened_out_count"), 0)
            covered_count = _safe_int(
                previous_ranking.get("covered_count"),
                effective_scored_count + screened_out_count,
            )
            coverage_basis = "active_calendar_symbols"
            coverage_rows = []
        else:
            effective_universe_count = int(
                universe_event_count
                if universe_event_count is not None
                else _safe_int(previous_ranking.get("universe_event_count"), len(items))
            )
            effective_scored_count = int(scored_count if scored_count is not None else len(items))
            screened_out_count = len(reported_rows) + len(rejected_rows)
            covered_count = min(
                effective_universe_count,
                effective_scored_count + screened_out_count,
            ) if effective_universe_count > 0 else effective_scored_count
            coverage_basis = "aggregate_counts"
            coverage_rows = items + reported_rows + rejected_rows
        full_universe_scored = (
            effective_universe_count > 0
            and effective_scored_count >= effective_universe_count
        )
        full_universe_covered = (
            effective_universe_count > 0
            and covered_count >= effective_universe_count
        )
        scored_event_dates = sorted(
            event_dt.date().isoformat()
            for row in coverage_rows
            if (event_dt := _parse_iso(row.get("earnings_time_utc"))) is not None
        )
        coverage_frontier = (
            scored_event_dates[-1]
            if scored_event_dates
            else previous_ranking.get("coverage_frontier_event_date")
        )
        if active_symbols:
            metric_items = [
                item for item in items
                if str(item.get("symbol") or "").upper().strip() in active_symbols
            ]
            eligible_count = sum(bool(item.get("recommendation_eligible")) for item in metric_items)
            ai_reviewed_count = sum(
                bool(
                    isinstance(item.get("ai_decision"), dict)
                    and item["ai_decision"].get("used")
                    and not item.get("news_update_pending")
                )
                for item in metric_items
            )
        else:
            # Also the cache-read path: single-symbol rescores, on-demand AI
            # verdicts and rows that have since reported change the served
            # rows between full cycles, so these two counts always describe
            # the rows actually served. Freezing them at the last full cycle
            # showed "32 AI verified" above an empty table after a restart and
            # "0 AI verified" next to rows carrying fresh verdicts (tab audit
            # 2026-11-15).
            eligible_count = sum(bool(item.get("recommendation_eligible")) for item in items)
            ai_reviewed_count = sum(
                bool(
                    isinstance(item.get("ai_decision"), dict)
                    and item["ai_decision"].get("used")
                    and not item.get("news_update_pending")
                )
                for item in items
            )
        payload["ranking"] = {
            "method": "two_stage_evidence_weighted_v2",
            "ranking_order": "trader_score_desc_then_event_date_asc",
            "coverage_order": "nearest_event_first",
            "coverage_basis": coverage_basis,
            "coverage_frontier_event_date": coverage_frontier,
            "score_is_probability": False,
            "min_trader_score": max(0, min(100, _safe_int(cfg.get("top_prediction_min_trader_score"), 60))),
            "selection_policy": "ELIGIBILITY_GATES_AND_MINIMUM_SCORE",
            "direction_edge": self._direction_edge_summary(),
            "top_limit": max(1, min(20, _safe_int(cfg.get("top_predictions_limit"), 10))),
            "universe_event_count": effective_universe_count,
            "scored_count": effective_scored_count,
            "light_scanned_count": effective_universe_count,
            "deep_scored_count": effective_scored_count,
            "full_universe_deep_scored": full_universe_scored,
            "screened_out_count": screened_out_count,
            "covered_count": covered_count,
            "full_universe_covered": full_universe_covered,
            "calendar_coverage_pct": round(
                min(100.0, covered_count / effective_universe_count * 100.0),
                1,
            ) if effective_universe_count > 0 else 0.0,
            "deep_score_coverage_pct": round(
                min(100.0, effective_scored_count / effective_universe_count * 100.0),
                1,
            ) if effective_universe_count > 0 else 0.0,
            "eligible_count": eligible_count,
            "ai_reviewed_count": ai_reviewed_count,
            "news_articles_scanned": int(
                news_articles_scanned
                if news_articles_scanned is not None
                else sum(self._item_news_counts(item)[0] for item in items)
            ),
            "snapshot_age_seconds": age_seconds,
            "recommendation_max_age_seconds": max_age,
            "state": "STALE" if stale else "FRESH",
            "generated_at": _iso(now),
        }
        flags = dict(payload.get("flags") or {})
        flags["recommendations_stale"] = bool(stale)
        flags["top_predictions_available"] = bool(predictions)
        payload["flags"] = flags

    def _score_catalyst_direction_flags(self, news_rows: List[Dict[str, Any]]) -> float:
        score = 0.0
        for row in news_rows[:20]:
            text = _norm_text(_headline_blob(row))
            if any(x in text for x in ("offering", "dilution")):
                score = min(score, -0.7)
            if any(x in text for x in ("fda approval", "major contract", "strategic partnership", "raises guidance")):
                score = max(score, 0.7)
        return float(_clamp(score, -1.0, 1.0))

    def _estimate_earnings_trend_score(self, symbol: str) -> Optional[float]:
        sym = str(symbol or "").upper().strip()
        if not sym:
            return None
        cache = getattr(self, "_growth_ttl_cache", None)
        if cache is None:
            cache = self._growth_ttl_cache = {}
        hit = cache.get(sym)
        now = time.time()
        if hit and (now - hit[0]) < self._GROWTH_TTL_SECONDS:
            growth = hit[1]
        else:
            growth = hit[1] if hit is not None else None
            if self._external_budget_exhausted():
                if not isinstance(growth, dict):
                    return None
            try:
                if (
                    not self._external_budget_exhausted()
                    and self._reserve_external_call("yfinance_growth")
                ):
                    fresh_growth = StockDataClient.get_income_statement_growth(sym, ttl=86400)
                    if isinstance(fresh_growth, dict):
                        growth = fresh_growth
            except Exception:
                pass
            if isinstance(growth, dict):
                cache[sym] = (now, growth)

        if not isinstance(growth, dict):
            return None
        eps_g = _safe_float(
            growth.get("earningsGrowth", growth.get("earningsQuarterlyGrowth")), None
        )
        rev_g = _safe_float(growth.get("revenueGrowth"), None)
        if eps_g is None and rev_g is None:
            return None
        blend = 0.0
        if eps_g is not None:
            blend += eps_g
        if rev_g is not None:
            blend += 0.6 * rev_g
        return float(_clamp(blend, -1.0, 1.0))

    def _compute_historical_earnings_gap_stats(self, symbol: str, max_events: int = 8) -> Dict[str, Any]:
        """Compute historical post-earnings gap-move stats for a symbol.

        Reads from the economic-calendar earnings cache first. When that cache
        hasn't been warmed for this symbol (e.g. NVDA, recently-listed names),
        falls back to yfinance past-earnings dates + daily OHLC bars so GapRisk
        can still be computed from REAL data instead of silently returning null.
        Deterministic: no fabrication — if neither source yields data, sample_size=0
        and the caller surfaces an explicit `gap_risk_unavailable` flag.
        """
        now = _utc_now()
        moves: List[float] = []
        used_days = set()
        source = "calendar_cache"

        events = self._get_cached_earnings_history(symbol, max_events=max_events * 3)
        for event in events:
            if len(moves) >= max_events:
                break
            event_dt = _parse_iso(event.get("date") or event.get("date_formatted"))
            if event_dt is None or event_dt >= now:
                continue
            event_day = event_dt.date().isoformat()
            if event_day in used_days:
                continue
            session = self._normalize_session(event.get("when") or event.get("session"))
            move = self._compute_event_gap_move_pct(symbol, event_dt, session)
            if move is None:
                continue
            used_days.add(event_day)
            moves.append(abs(float(move)))

        # A non-empty calendar can still be unusable (future-only/conflicting
        # rows). Fill the remaining sample from Yahoo instead of falling back
        # only when the entire calendar list is empty.
        if len(moves) < max_events:
            calendar_sample_size = len(moves)
            daily_bars = self._load_symbol_daily_bars(symbol)
            if daily_bars:
                for event_dt, session in self._resolve_yf_past_earnings_dates(symbol, n=max_events):
                    if len(moves) >= max_events:
                        break
                    if event_dt is None or event_dt >= now:
                        continue
                    event_day = event_dt.date().isoformat()
                    if event_day in used_days:
                        continue
                    move = self._compute_event_gap_move_pct(symbol, event_dt, session, bars=daily_bars)
                    if move is None:
                        continue
                    used_days.add(event_day)
                    moves.append(abs(float(move)))
            if len(moves) > calendar_sample_size:
                source = "calendar+yfinance" if calendar_sample_size else "yfinance_fallback"
            elif calendar_sample_size == 0:
                source = "yfinance_fallback"

        if not moves:
            return {"median_move_pct": None, "sample_size": 0, "moves": [], "source": source}
        return {
            "median_move_pct": float(median(moves)),
            "sample_size": len(moves),
            "moves": moves[:max_events],
            "source": source,
        }

    def _load_symbol_daily_bars(self, symbol: str) -> List[Dict[str, Any]]:
        """~1y of daily OHLC bars (cached 1h), used for earnings gap evaluation."""
        sym = str(symbol or "").upper().strip()
        if not sym:
            return []
        cache = getattr(self, "_daily_bars_cache", None)
        if cache is None:
            cache = self._daily_bars_cache = {}
        now = time.time()
        hit = cache.get(sym)
        if hit and (now - hit[0]) < 3600.0:
            return hit[1]
        if self._external_budget_exhausted():
            return list(hit[1]) if hit and isinstance(hit[1], list) else []
        if not self._reserve_external_call("yfinance_daily_bars"):
            return list(hit[1]) if hit and isinstance(hit[1], list) else []
        try:
            records = StockDataClient.get_historical_chart(sym, timeframe="1day", limit=300)
        except Exception as exc:
            _log_provider_error_once("yfinance_daily_bars", sym, exc)
            records = []
        out: List[Dict[str, Any]] = []
        for rec in records or []:
            # Providers may mix date-only/naive values with offset-aware ISO
            # timestamps. Normalize every bar before sorting or comparing it
            # with the UTC earnings event; Python intentionally rejects mixed
            # naive/aware datetime ordering.
            dt_val = _parse_iso(rec.get("date"))
            if dt_val is None:
                continue
            o = _safe_float(rec.get("open"), None)
            c = _safe_float(rec.get("close"), None)
            if o is None or c is None:
                continue
            out.append({"dt": dt_val, "open": o, "close": c})
        out.sort(key=lambda x: x["dt"])
        if out:
            cache[sym] = (now, out)
            return out
        return list(hit[1]) if hit and isinstance(hit[1], list) else []

    def _get_yfinance_earnings_history(
        self,
        symbol: str,
        *,
        max_events: int = 8,
        allow_remote: bool = False,
        force_refresh: bool = False,
    ) -> List[Dict[str, Any]]:
        """Normalized completed Yahoo earnings rows, positively cached for 12h.

        A legacy empty entry is honored by ordinary cache reads but an explicit
        warmup can bypass it with ``force_refresh``. New empty/error responses
        are never cached, and a prior successful sample remains the fallback.
        """
        sym = str(symbol or "").upper().strip()
        if not sym:
            return []
        cache = getattr(self, "_yf_earnings_history_cache", None)
        if cache is None:
            cache = self._yf_earnings_history_cache = {}
        now_t = time.time()
        hit = cache.get(sym)
        if hit and (now_t - hit[0]) < 43200.0:
            hit_rows = list(hit[1]) if isinstance(hit[1], list) else []
            if hit_rows or not force_refresh:
                return hit_rows[: max(1, int(max_events))]
        if not allow_remote:
            return (
                list(hit[1])[: max(1, int(max_events))]
                if hit and isinstance(hit[1], list)
                else []
            )
        if not self._reserve_external_call("yfinance_history"):
            return (
                list(hit[1])[: max(1, int(max_events))]
                if hit and isinstance(hit[1], list)
                else []
            )

        out: List[Dict[str, Any]] = []
        try:
            import yfinance as yf
            limit = max(16, int(max_events) * 3)
            df = yf.Ticker(StockDataClient._clean_symbol(sym)).get_earnings_dates(limit=limit)
            now_dt = _utc_now()
            if df is not None and len(df):
                for idx, raw in df.iterrows():
                    try:
                        local_dt = idx.to_pydatetime()
                    except Exception:
                        continue
                    # session from the report's LOCAL hour (before tz)= AMC after midday
                    local_hour = local_dt.hour
                    session = "AMC" if local_hour >= 12 else "BMO"
                    dt = local_dt if local_dt.tzinfo else local_dt.replace(tzinfo=timezone.utc)
                    dt = dt.astimezone(timezone.utc)
                    if dt < now_dt:
                        estimate = raw.get("EPS Estimate") if hasattr(raw, "get") else None
                        actual = raw.get("Reported EPS") if hasattr(raw, "get") else None
                        out.append(
                            {
                                "date": _iso(dt),
                                "date_formatted": dt.date().isoformat(),
                                "session": session,
                                "when": session.lower(),
                                "epsEstimated": _safe_float(estimate, None),
                                "epsActual": _safe_float(actual, None),
                                "source": "yfinance",
                            }
                        )
            out.sort(
                key=lambda row: _parse_iso(row.get("date")) or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )
            out = out[: max(1, int(max_events))]
        except Exception as exc:
            _log_provider_error_once("yfinance_earnings_history", sym, exc)
            out = []
        if out:
            cache[sym] = (now_t, out)
            return list(out)
        # A provider miss must not poison the cache. If a successful sample has
        # merely gone stale, keep serving it until Yahoo recovers.
        return (
            list(hit[1])[: max(1, int(max_events))]
            if hit and isinstance(hit[1], list) and hit[1]
            else []
        )

    def _resolve_yf_past_earnings_dates(self, symbol: str, n: int = 4) -> List[Tuple[datetime, str]]:
        """Past Yahoo earnings dates backed by the shared EPS-history cache."""
        rows = self._get_yfinance_earnings_history(
            symbol,
            max_events=max(8, n * 2),
            allow_remote=True,
        )
        out: List[Tuple[datetime, str]] = []
        for row in rows:
            dt = _parse_iso(row.get("date") or row.get("date_formatted"))
            if dt is not None:
                out.append((dt, self._normalize_session(row.get("session") or row.get("when"))))
        out.sort(key=lambda pair: pair[0], reverse=True)
        return out[: max(1, n)]

    def _compute_recent_earnings_reactions(self, symbol: str, n: int = 3) -> List[Dict[str, Any]]:
        """Last n PAST earnings: signed post-earnings gap move (pre-earnings close ->
        post-earnings open). +% = the stock popped (a LONG would have won), -% = it
        dropped (a SHORT would have won). Real 'what happened after' history."""
        now = _utc_now()
        # Source 1: economic-calendar cache (carries session info where present).
        pairs: List[Tuple[datetime, str]] = []
        for e in self._get_cached_earnings_history(symbol, max_events=n * 4):
            dt = _parse_iso(e.get("date") or e.get("date_formatted"))
            if dt is not None and dt < now:
                pairs.append((dt, self._normalize_session(e.get("when") or e.get("session"))))
        # Source 2: direct yfinance fallback for names the cache hasn't warmed.
        if len(pairs) < n:
            pairs.extend(self._resolve_yf_past_earnings_dates(symbol, n * 2))
        pairs.sort(key=lambda p: p[0], reverse=True)

        daily_bars = self._load_symbol_daily_bars(symbol)
        out: List[Dict[str, Any]] = []
        seen_days: set = set()
        for dt, sess in pairs:
            if len(out) >= n:
                break
            day = dt.date().isoformat()
            if day in seen_days:  # collapse duplicate same-day earnings rows
                continue
            mv = self._compute_event_gap_move_pct(symbol, dt, sess, bars=daily_bars)
            if mv is None:
                continue
            seen_days.add(day)
            out.append({
                "date": day,
                "move_pct": round(float(mv), 2),
                "dir": "UP" if mv >= 0 else "DOWN",
            })
        return out

    def _compute_recent_reaction_tilt(self, reactions: List[Dict[str, Any]]) -> Optional[float]:
        """Directional lean of recent post-earnings reactions in [-1, 1] (mean of capped
        signed moves). Used as a small, real, deterministic input to the direction bias."""
        if not reactions:
            return None
        vals = []
        for r in reactions:
            mv = _safe_float(r.get("move_pct"), None)
            if mv is None:
                continue
            # cap each event at +-10% so one biotech-style outlier can't dominate the lean
            vals.append(max(-10.0, min(10.0, mv)) / 10.0)
        if not vals:
            return None
        return round(sum(vals) / len(vals), 4)

    def _get_track_record_cached(self, directional_threshold: int = 15, recent_per_symbol: int = 3) -> Dict[str, Any]:
        """Directional hit-rate of our settled pre-earnings calls, from _feedback['outcomes'].
        A 'call' = a settled outcome whose prediction was directional (|bias| >= threshold);
        a hit = sign(predicted_direction_bias) == sign(realized_signed_move_pct). Cached ~60s
        because it scans the full outcomes log (thousands of rows)."""
        now = time.time()
        cached = getattr(self, "_track_record_cache", None)
        if cached is not None and (now - cached[0]) < 60.0:
            return cached[1]
        feedback = self.get_feedback_snapshot(deduplicate_outcomes=True)
        outs = feedback.get("outcomes", []) or []
        overall_hit = overall_total = 0
        per: Dict[str, List[int]] = {}
        recent: Dict[str, List[Dict[str, Any]]] = {}
        # The outcomes log holds duplicates: the same earnings event got registered under
        # slightly different timestamps (00:00 vs 09:00) across cycles, so one real report
        # can appear many times. Collapse to one row per (symbol, calendar day).
        seen_events: set = set()
        for o in outs:
            if not isinstance(o, dict):
                continue
            pb = _safe_int(o.get("predicted_direction_bias"), 0)
            rm = _safe_float(o.get("realized_signed_move_pct"), None)
            if rm is None or abs(pb) < directional_threshold:
                continue
            _sym0 = str(o.get("symbol") or "").upper().strip()
            _ev = _parse_iso(o.get("event_utc"))
            _dedup_key = (_sym0, _ev.date().isoformat() if _ev else str(o.get("event_utc")))
            if _dedup_key in seen_events:
                continue
            seen_events.add(_dedup_key)
            hit = (pb > 0) == (rm > 0)
            overall_total += 1
            overall_hit += 1 if hit else 0
            sym = str(o.get("symbol") or "").upper().strip()
            if not sym:
                continue
            rec = per.setdefault(sym, [0, 0])
            rec[0] += 1 if hit else 0
            rec[1] += 1
            recent.setdefault(sym, []).append({
                "event_utc": o.get("event_utc"),
                "predicted": "LONG" if pb > 0 else "SHORT",
                "realized_move_pct": round(float(rm), 2),
                "hit": bool(hit),
            })
        per_symbol: Dict[str, Any] = {}
        for sym, (h, t) in per.items():
            rl = recent.get(sym, [])[-recent_per_symbol:]
            per_symbol[sym] = {
                "hits": h,
                "total": t,
                "accuracy": round(100.0 * h / t, 1) if t else None,
                "recent": list(reversed(rl)),
            }
        result = {
            "overall": {
                "hits": overall_hit,
                "total": overall_total,
                "accuracy": round(100.0 * overall_hit / overall_total, 1) if overall_total else None,
            },
            "per_symbol": per_symbol,
        }
        self._track_record_cache = (now, result)
        return result

    def _get_cached_earnings_history(self, symbol: str, max_events: int = 24) -> List[Dict[str, Any]]:
        symbol_u = str(symbol or "").upper().strip()
        if not symbol_u:
            return []
        try:
            calendar = get_economic_calendar_agent(self.config_manager.config)
            lock = getattr(calendar, "_cache_lock", None)
            if lock is not None:
                with lock:
                    cache_map = dict((getattr(calendar, "_events_cache", {}) or {}).get("earnings", {}) or {})
            else:
                cache_map = dict((getattr(calendar, "_events_cache", {}) or {}).get("earnings", {}) or {})
            rows = [dict(x) for x in (cache_map.get(symbol_u, []) or []) if isinstance(x, dict)]
            rows.sort(
                key=lambda r: _parse_iso(r.get("date") or r.get("date_formatted"))
                or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )
            return rows[:max(1, int(max_events))]
        except Exception:
            return []

    def _compute_track_record_metrics(
        self,
        cfg: Dict[str, Any],
        outcomes: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Per-verdict and per-confidence-band directional accuracy over all
        matured outcomes (Task #11 — History / Track-Record panel).

        - Verdict bucket uses the app's ±15 directional rule — the same rule
          the endpoint advertises as `directional_threshold: 15`, the recent
          rows render via `direction_label_from_score`, and the per-symbol
          track record scores with: LONG when bias >= +15, SHORT when
          bias <= -15, NEUTRAL otherwise. Bucketing by raw bias SIGN instead
          mixed thousands of sub-threshold coin-flips the app itself calls
          NEUTRAL into the LONG/SHORT headline hit-rates (re-audit P2), so
          the population behind the headline stats did not match the app's
          own directional definition. NEUTRAL rows are counted but disclosed
          as non-directional (`hit_rate` None, `directional` False).
        - A "call" requires a finite, non-zero realized signed move; NaN/None/
          zero moves are skipped (they are ambiguous and historically produced
          wrong deterministic hit/miss from `nan > 0` being False).
        - Hit = sign(bias) == sign(realized_signed_move_pct).
        - Confidence bands contain only `predicted_ai_confidence` (0-1 scaled
          to 0-100). Legacy score magnitude is never mixed into AI confidence. Bands:
          0-50 / 50-70 / 70-85 / 85-100. Only directional calls (|bias| >= 15)
          are banded, since hit is undefined for NEUTRAL.

        Returns concrete numbers: n, hits, hit_rate (% or None), avg_realized_move.
        """
        if outcomes is None:
            feedback = self.get_feedback_snapshot(deduplicate_outcomes=True)
            outcomes = list(feedback.get("outcomes", []) or [])
        else:
            outcomes = self._dedupe_feedback_outcomes(list(outcomes or []))
        bands = [("0-50", 0.0, 50.0), ("50-70", 50.0, 70.0),
                 ("70-85", 70.0, 85.0), ("85-100", 85.0, 100.0)]

        # Same plausibility rule as the Evidence Lab (core.pre_earnings_learning):
        # a one-day earnings move beyond +/-100% is a price-data error (split,
        # ticker reuse, bad bar) until someone verifies it by hand. One such
        # row (CLGN +859%) turned the Top 10 "avg realized move" into 24.7%
        # and the Top 10 SHORT average into 146%.
        from core.pre_earnings_learning import MAX_PLAUSIBLE_MOVE_PCT

        excluded = {"implausible": 0}

        def _realized(o: Dict[str, Any]) -> Optional[float]:
            v = _safe_float(o.get("realized_signed_move_pct"), None)
            if v is None or not math.isfinite(v) or v == 0.0:
                return None
            if abs(v) > MAX_PLAUSIBLE_MOVE_PCT:
                excluded["implausible"] += 1
                return None
            return float(v)

        per_verdict: Dict[str, Dict[str, Any]] = {
            "LONG": {"n": 0, "hits": 0, "move_sum": 0.0},
            "SHORT": {"n": 0, "hits": 0, "move_sum": 0.0},
            "NEUTRAL": {"n": 0, "hits": 0, "move_sum": 0.0},
        }
        per_ai_verdict: Dict[str, Dict[str, Any]] = {
            "LONG": {"n": 0, "hits": 0, "move_sum": 0.0},
            "SHORT": {"n": 0, "hits": 0, "move_sum": 0.0},
            "NEUTRAL": {"n": 0, "hits": 0, "move_sum": 0.0},
        }
        per_band: Dict[str, Dict[str, Any]] = {b[0]: {"n": 0, "hits": 0, "move_sum": 0.0} for b in bands}
        per_ai_band: Dict[str, Dict[str, Any]] = {b[0]: {"n": 0, "hits": 0, "move_sum": 0.0} for b in bands}
        overall = {"total": 0, "hits": 0, "move_sum": 0.0, "up": 0}
        event_times: List[datetime] = []
        top10_overall = {"total": 0, "hits": 0, "move_sum": 0.0}
        top10_settled = 0
        top10_per_side: Dict[str, Dict[str, Any]] = {
            "LONG": {"total": 0, "hits": 0, "move_sum": 0.0},
            "SHORT": {"total": 0, "hits": 0, "move_sum": 0.0},
        }
        ai_coverage = 0

        for o in outcomes:
            if not isinstance(o, dict):
                continue
            if bool(o.get("was_top_prediction")):
                top10_settled += 1
            if o.get("predicted_ai_verdict"):
                ai_coverage += 1
            event_time = _parse_iso(o.get("event_utc"))
            if event_time is not None:
                event_times.append(event_time)
            bias = _safe_int(o.get("predicted_direction_bias"), 0)
            s = _realized(o)
            if s is None:
                continue  # not yet realized / NaN / zero -> not a callable outcome

            # Top-10 performance is scored against the recommendation captured
            # at the instant the event first entered the shortlist. It must not
            # inherit a direction from an older generic prediction, nor change
            # when a later refresh re-ranks/re-scores the same event.
            if bool(o.get("was_top_prediction")):
                top_bias = _safe_int(o.get("top_prediction_bias"), bias)
                top_side = str(o.get("top_prediction_direction") or "").upper().strip()
                if top_side not in {"LONG", "SHORT"}:
                    top_side = "LONG" if top_bias >= 15 else ("SHORT" if top_bias <= -15 else "")
                if top_side:
                    top_hit = (top_side == "LONG" and s > 0) or (top_side == "SHORT" and s < 0)
                    top10_overall["total"] += 1
                    top10_overall["move_sum"] += abs(s)
                    top10_per_side[top_side]["total"] += 1
                    top10_per_side[top_side]["move_sum"] += abs(s)
                    if top_hit:
                        top10_overall["hits"] += 1
                        top10_per_side[top_side]["hits"] += 1
            # Bucket by the app's ±15 directional rule (LONG >= +15,
            # SHORT <= -15, else NEUTRAL) — never by raw sign. A +2 lean is
            # not a LONG call; the app labels it NEUTRAL everywhere else
            # (direction_label_from_score, recent-row labels, per-symbol
            # track record), so scoring it as LONG here would inflate the
            # headline hit-rates with coin-flips (re-audit P2).
            vkey = "LONG" if bias >= 15 else ("SHORT" if bias <= -15 else "NEUTRAL")
            per_verdict[vkey]["n"] += 1
            per_verdict[vkey]["move_sum"] += abs(s)
            hit = (bias > 0 and s > 0) or (bias < 0 and s < 0)
            ai_verdict = str(o.get("predicted_ai_verdict") or "").upper()
            if ai_verdict in per_ai_verdict:
                ai_hit = (
                    (ai_verdict == "LONG" and s > 0)
                    or (ai_verdict == "SHORT" and s < 0)
                )
                per_ai_verdict[ai_verdict]["n"] += 1
                per_ai_verdict[ai_verdict]["move_sum"] += abs(s)
                if ai_verdict != "NEUTRAL" and ai_hit:
                    per_ai_verdict[ai_verdict]["hits"] += 1
            if vkey != "NEUTRAL":
                if hit:
                    per_verdict[vkey]["hits"] += 1
                overall["total"] += 1
                overall["move_sum"] += abs(s)
                if s > 0:
                    overall["up"] += 1
                if hit:
                    overall["hits"] += 1
                # Confidence band (directional calls only).
                ai_conf = _safe_float(o.get("predicted_ai_confidence"), None)
                if ai_conf is not None and math.isfinite(ai_conf):
                    conf_pct = max(0.0, min(100.0, ai_conf * 100.0))
                    ai_conf_available = True
                else:
                    conf_pct = max(0.0, min(100.0, float(abs(bias))))
                    ai_conf_available = False
                for label, lo, hi in bands:
                    if lo < conf_pct <= hi or (label == "0-50" and conf_pct <= lo):
                        per_band[label]["n"] += 1
                        if hit:
                            per_band[label]["hits"] += 1
                        per_band[label]["move_sum"] += abs(s)
                        if ai_conf_available:
                            per_ai_band[label]["n"] += 1
                            if hit:
                                per_ai_band[label]["hits"] += 1
                            per_ai_band[label]["move_sum"] += abs(s)
                        break

        def _rate(n: int, h: int) -> Optional[float]:
            return round(100.0 * h / n, 1) if n else None

        def _avg(n: int, ms: float) -> Optional[float]:
            return round(ms / n, 2) if n else None

        per_verdict_out = {}
        for k in ("LONG", "SHORT", "NEUTRAL"):
            d = per_verdict[k]
            per_verdict_out[k] = {
                "n": d["n"],
                "hits": d["hits"],
                "hit_rate": None if k == "NEUTRAL" else _rate(d["n"], d["hits"]),
                "avg_realized_move": _avg(d["n"], d["move_sum"]),
                # NEUTRAL is disclosed as non-directional: it holds the
                # sub-threshold leans (|bias| < 15) the app refuses to call
                # LONG/SHORT, so its count is shown but never scored.
                "directional": k != "NEUTRAL",
            }
        per_ai_verdict_out = {}
        for k in ("LONG", "SHORT", "NEUTRAL"):
            d = per_ai_verdict[k]
            per_ai_verdict_out[k] = {
                "n": d["n"],
                "hits": d["hits"],
                "hit_rate": None if k == "NEUTRAL" else _rate(d["n"], d["hits"]),
                "avg_realized_move": _avg(d["n"], d["move_sum"]),
                "directional": k != "NEUTRAL",
            }
        per_band_out = [
            {
                "band": label,
                "n": per_band[label]["n"],
                "hits": per_band[label]["hits"],
                "hit_rate": _rate(per_band[label]["n"], per_band[label]["hits"]),
                "avg_realized_move": _avg(per_band[label]["n"], per_band[label]["move_sum"]),
            }
            for label, _, _ in bands
        ]
        per_ai_band_out = [
            {
                "band": label,
                "n": per_ai_band[label]["n"],
                "hits": per_ai_band[label]["hits"],
                "hit_rate": _rate(per_ai_band[label]["n"], per_ai_band[label]["hits"]),
                "avg_realized_move": _avg(per_ai_band[label]["n"], per_ai_band[label]["move_sum"]),
            }
            for label, _, _ in bands
        ]
        # Naive yardstick on the SAME directional calls: how often would
        # "always LONG" have been right? A hit rate is only an edge if it
        # beats this; the headline 54% looked like one until set against the
        # ~56% of these events that simply moved up.
        always_long_rate = _rate(overall["total"], overall["up"])
        overall_hit_rate = _rate(overall["total"], overall["hits"])
        return {
            "sample_size": len(outcomes),
            "ai_coverage": ai_coverage,
            "first_event_utc": _iso(min(event_times)) if event_times else None,
            "last_event_utc": _iso(max(event_times)) if event_times else None,
            "excluded_implausible_moves": int(excluded["implausible"]),
            "max_plausible_move_pct": float(MAX_PLAUSIBLE_MOVE_PCT),
            "overall": {
                "total": overall["total"],
                "hits": overall["hits"],
                "hit_rate": overall_hit_rate,
                "avg_realized_move": _avg(overall["total"], overall["move_sum"]),
                "always_long_hit_rate": always_long_rate,
                "edge_vs_always_long_pct": (
                    round(overall_hit_rate - always_long_rate, 1)
                    if overall_hit_rate is not None and always_long_rate is not None
                    else None
                ),
            },
            "per_verdict": per_verdict_out,
            "per_direction": per_verdict_out,
            "per_ai_verdict": per_ai_verdict_out,
            "per_band": per_band_out,
            "per_ai_band": per_ai_band_out,
            "confidence_band_source": "per_ai_band=ai_only; per_band=legacy_compatible",
            "top_predictions": {
                "total": top10_overall["total"],
                "settled": top10_settled,
                "unscored": max(0, top10_settled - top10_overall["total"]),
                "hits": top10_overall["hits"],
                "hit_rate": _rate(top10_overall["total"], top10_overall["hits"]),
                "avg_realized_move": _avg(top10_overall["total"], top10_overall["move_sum"]),
                "pending": sum(
                    1
                    for row in (self._feedback.get("open_predictions", {}) or {}).values()
                    if isinstance(row, dict) and bool(row.get("was_top_prediction"))
                ),
                "per_side": {
                    side: {
                        "total": data["total"],
                        "hits": data["hits"],
                        "hit_rate": _rate(data["total"], data["hits"]),
                        "avg_realized_move": _avg(data["total"], data["move_sum"]),
                    }
                    for side, data in top10_per_side.items()
                },
            },
        }

    @staticmethod
    def _track_record_baseline_caveat(metrics: Dict[str, Any]) -> Optional[str]:
        """Deterministic honesty line shown with the LLM track-record summary.

        The model once described a 54.0% hit rate as "a modest positive edge"
        while the Evidence Lab on the same page said no claim beats the naive
        baseline. The comparison is arithmetic, so it is computed here and not
        left to the model.
        """
        overall = (metrics or {}).get("overall") if isinstance(metrics, dict) else None
        if not isinstance(overall, dict):
            return None
        hit_rate = _safe_float(overall.get("hit_rate"), None)
        baseline = _safe_float(overall.get("always_long_hit_rate"), None)
        total = _safe_int(overall.get("total"), 0)
        if hit_rate is None or baseline is None or total <= 0:
            return None
        if hit_rate <= baseline:
            return (
                f"Not an edge: the {hit_rate:.1f}% directional hit rate does not beat "
                f"always-LONG ({baseline:.1f}% of these {total} settled events moved up). "
                "Read the summary as a description of past calls, not as proof they work."
            )
        return (
            f"In-sample only: {hit_rate:.1f}% directional hit rate vs {baseline:.1f}% for "
            f"always-LONG on the same {total} settled events. The out-of-sample test is "
            "in the Evidence Lab below."
        )

    def _compute_track_record_ai_summary(self, cfg: Dict[str, Any], metrics: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Call the LLM with a SEPARATE prompt (not the per-ticker catalyst
        classifier) to produce a CONCISE track-record assessment: a one-to-two
        sentence summary of how the LONG/SHORT calls performed + one short
        actionable recommendation for future predictions. Cloud-only (local
        models are too slow for an aggregate call) and non-fatal: returns None
        on any error / circuit open / AI disabled.
        """
        if not bool(cfg.get("use_ollama", True)):
            return None
        try:
            if not (ollama.is_cloud_host() or ollama.check_availability()):
                return None
            model = self._analyst_model()
            if not model:
                return None
            ov = metrics.get("overall", {}) or {}
            pv = metrics.get("per_verdict", {}) or {}
            pb = metrics.get("per_band", []) or []
            system = (
                "You are a quantitative analyst reviewing a pre-earnings "
                "DIRECTIONAL track record. Return a CONCISE assessment as STRICT "
                "JSON with exactly two keys: (1) summary — one or two sentences "
                "on how the LONG/SHORT calls performed (overall edge, which side "
                "did better, any standout confidence band); (2) recommendation — "
                "one short actionable line on what to adjust for future "
                "predictions. Be honest and terse; do not invent statistics not "
                "present in the data; do not include a separate data-verification "
                "section. A hit rate is an edge ONLY if it is above the naive "
                "always-LONG baseline given in the data; when it is not, say "
                "plainly that the calls show no edge over that baseline and never "
                "use the words 'edge', 'positive' or 'outperform' for them. "
                "Return STRICT JSON only: summary, recommendation."
            )
            prompt = (
                "Pre-Earnings directional track record (settled outcomes):\n"
                f"- Sample size: {metrics.get('sample_size', 0)} outcomes "
                f"({metrics.get('ai_coverage', 0)} carry an AI verdict).\n"
                f"- Overall directional: {ov.get('total', 0)} calls, "
                f"{ov.get('hits', 0)} hits, hit_rate {ov.get('hit_rate')}%, "
                f"avg realized move {ov.get('avg_realized_move')}%.\n"
                f"- Naive baseline on the same calls (always LONG): "
                f"{ov.get('always_long_hit_rate')}% would have been right; "
                f"hit_rate minus baseline = {ov.get('edge_vs_always_long_pct')} points.\n"
                f"- LONG: {pv.get('LONG', {}).get('n', 0)} calls, "
                f"{pv.get('LONG', {}).get('hits', 0)} hits, "
                f"hit_rate {pv.get('LONG', {}).get('hit_rate')}%.\n"
                f"- SHORT: {pv.get('SHORT', {}).get('n', 0)} calls, "
                f"{pv.get('SHORT', {}).get('hits', 0)} hits, "
                f"hit_rate {pv.get('SHORT', {}).get('hit_rate')}%.\n"
                "Confidence-band hit rates (band: n/hits/hit_rate%):\n"
                + "\n".join(
                    f"  {b.get('band')}: {b.get('n', 0)}/{b.get('hits', 0)} "
                    f"({b.get('hit_rate')}%)"
                    for b in pb
                )
                + "\nReturn STRICT JSON only: summary, recommendation."
            )
            timeout_sec = max(5, min(60, _safe_int(cfg.get("ollama_timeout_seconds"), 6)))
            timeout_sec = call_timeout_for(model, timeout_sec)
            result = ollama.generate(
                prompt,
                model=model,
                system=system,
                timeout_override=timeout_sec,
            )
            if not isinstance(result, dict) or result.get("error"):
                return None
            raw = str(result.get("response") or "").strip()
            parsed = None
            try:
                parsed = json.loads(raw)
            except Exception:
                # Tolerate code-fenced JSON.
                start, end = raw.find("{"), raw.rfind("}")
                if start != -1 and end > start:
                    parsed = json.loads(raw[start:end + 1])
            if not isinstance(parsed, dict):
                return None
            summary = {
                "summary": str(parsed.get("summary") or "").strip(),
                "recommendation": str(parsed.get("recommendation") or parsed.get("future_recommendations") or "").strip(),
                "model": str(result.get("_meta", {}).get("model") or model or "").strip(),
                "generated_at": _iso(_utc_now()),
                "caveat": self._track_record_baseline_caveat(metrics),
            }
            if not (summary["summary"] or summary["recommendation"]):
                return None
            return summary
        except Exception:
            logger.exception("Pre-Earnings track-record AI summary failed (non-fatal)")
            return None

    def get_track_record_ai_summary(self, cfg: Dict[str, Any], ttl_seconds: float = 300.0) -> Optional[Dict[str, Any]]:
        """Cached (TTL 300s) aggregate AI summary + recommendations. Computes
        fresh metrics and calls the LLM (separate prompt) only when the record
        it describes changed; returns None if AI is unavailable. Non-fatal."""
        now = time.monotonic()
        cached = getattr(self, "_track_record_ai_cache", None)
        if cached is not None and (now - float(cached[0])) < ttl_seconds and cached[1] is not None:
            return cached[1]
        metrics = self._compute_track_record_metrics(cfg)
        # The summary is made from these numbers alone. They change when an outcome
        # settles, a few times a day; asking every five minutes repeated the question.
        question = self._track_record_question(metrics)
        if cached is not None and cached[1] is not None and len(cached) > 2 and cached[2] == question:
            self._track_record_ai_cache = (now, cached[1], question)
            return cached[1]
        summary = self._compute_track_record_ai_summary(cfg, metrics)
        self._track_record_ai_cache = (now, summary, question)
        return summary

    def _track_record_question(self, metrics: Dict[str, Any]) -> str:
        """Identifies what the track-record summary is asked about: its numbers and the model."""
        try:
            model = self._analyst_model() or ""
        except Exception:  # noqa: BLE001
            model = ""
        numbers = [(metrics or {}).get(key) for key in
                   ("sample_size", "ai_coverage", "overall", "per_verdict", "per_band")]
        canonical = json.dumps([numbers, model], sort_keys=True, default=str)
        return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]

    def _compute_event_gap_move_pct(
        self, symbol: str, event_dt: datetime, session: str, bars: Optional[List[Dict[str, Any]]] = None
    ) -> Optional[float]:
        close_prev, open_next = self._get_event_reference_prices(symbol, event_dt, session, bars=bars)
        # Guard against NaN: yfinance OHLC bars can carry `float('nan')` for
        # open/close on illiquid tickers. `x in (None, 0.0)` does NOT match NaN
        # (nan != nan), so without an isfinite check the gap move becomes NaN,
        # propagates into persisted outcomes, and `json.dump` emits the
        # non-standard `NaN` literal → invalid JSON on disc (audit B1).
        if (
            close_prev is None
            or open_next is None
            or not math.isfinite(float(close_prev))
            or not math.isfinite(float(open_next))
            or float(close_prev) <= 0.0
            or float(open_next) <= 0.0
        ):
            return None
        return float((open_next - close_prev) / close_prev * 100.0)

    def _get_event_reference_prices(
        self,
        symbol: str,
        event_dt: datetime,
        session: str,
        bars: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[Optional[float], Optional[float]]:
        if bars is None:
            # Daily bars provide official session boundaries and a consistent
            # split-adjusted history. Intraday data can straddle split bases.
            bars = self._load_symbol_daily_bars(symbol)
        if not bars:
            return None, None
        bars = sorted(bars, key=lambda bar: bar["dt"])
        event_date = event_dt.date()
        sess = self._normalize_session(session)
        if sess not in {"BMO", "AMC"}:
            return None, None
        if not any(bar["dt"].date() == event_date for bar in bars):
            return None, None
        if sess == "BMO":
            prev_candidates = [b for b in bars if b["dt"].date() < event_date]
            next_candidates = [b for b in bars if b["dt"].date() >= event_date]
        elif sess == "AMC":
            prev_candidates = [b for b in bars if b["dt"].date() <= event_date]
            next_candidates = [b for b in bars if b["dt"].date() > event_date]
        else:
            prev_candidates = [b for b in bars if b["dt"] <= event_dt]
            next_candidates = [b for b in bars if b["dt"] > event_dt]

        if not prev_candidates or not next_candidates:
            return None, None

        prev = prev_candidates[-1]
        nxt = next_candidates[0]
        # Do not turn a missing event day or weeks-old price into an outcome.
        # Four calendar days accommodates normal weekends plus a holiday.
        if not 1 <= (nxt["dt"].date() - prev["dt"].date()).days <= 4:
            return None, None
        close_prev = _safe_float(prev.get("close"), None)
        open_next = _safe_float(nxt.get("open"), None)
        return close_prev, open_next

    def _load_symbol_ohlc_bars(self, symbol: str) -> List[Dict[str, Any]]:
        # StockDataClient returns a list of dicts, not a DataFrame
        sym = str(symbol or "").upper().strip()
        if not sym:
            return []
        cache = getattr(self, "_ohlc_bars_cache", None)
        if cache is None:
            cache = self._ohlc_bars_cache = {}
        now = time.time()
        hit = cache.get(sym)
        if hit and (now - hit[0]) < 3600.0:
            return hit[1]
        if self._external_budget_exhausted():
            return list(hit[1]) if hit and isinstance(hit[1], list) else []
        if not self._reserve_external_call("yfinance_ohlc_bars"):
            return list(hit[1]) if hit and isinstance(hit[1], list) else []
        try:
            records = StockDataClient.get_historical_chart(sym, interval="4hour", limit=400)
        except Exception:
            records = []
        if not records:
            return list(hit[1]) if hit and isinstance(hit[1], list) else []

        out: List[Dict[str, Any]] = []
        for rec in records:
            dt_val = _parse_iso(rec.get("date"))
            if dt_val is None:
                continue
            o = _safe_float(rec.get("open"), None)
            c = _safe_float(rec.get("close"), None)
            if o is None or c is None:
                continue
            out.append({"dt": dt_val, "open": o, "close": c})

        out.sort(key=lambda x: x["dt"])
        if out:
            cache[sym] = (now, out)
        return out

    def _estimate_expected_move_pct(self, symbol: str) -> Optional[float]:
        quote = self._get_cached_quote(symbol)
        if not isinstance(quote, dict) or not quote:
            return None
        day_low = _safe_float(quote.get("dayLow"), None)
        day_high = _safe_float(quote.get("dayHigh"), None)
        price = _safe_float(quote.get("price"), None)
        if day_low is None or day_high is None or price in (None, 0.0):
            return None
        return float(abs(day_high - day_low) / abs(price) * 100.0)

    def _build_why_bullets(
        self,
        symbol: str,
        gap_risk: int,
        direction_bias: int,
        news_count: int,
        severity_label: str,
        mismatch: bool,
        event: Dict[str, Any],
        contributors: List[Dict[str, Any]],
        gap_risk_unavailable_reason: Optional[str] = None,
        direction_components: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        # Name the inputs the direction score was really built from. The line
        # used to read "from cached news sentiment and catalyst flags" on every
        # row — including rows with zero news whose whole +33 came from the
        # eToro crowd skew (tab audit 2026-11-15: GRRR).
        direction_sources: List[str] = []
        if isinstance(direction_components, dict):
            labels = (
                ("historical", "EPS beat history"),
                ("analyst_revision", "analyst consensus"),
                ("options", "options positioning"),
                ("post_earnings_history", "past earnings reactions"),
                ("retail_positioning", "eToro retail positioning"),
            )
            direction_sources = [
                label for key, label in labels
                if direction_components.get(key) is not None
            ]
            if direction_components.get("news_ai") is not None:
                direction_sources.append(
                    "news sentiment"
                    if news_count > 0
                    else "no news (sentiment held neutral)"
                )
        direction_line = (
            f"DirectionBias={direction_bias} from {', '.join(direction_sources)}."
            if direction_sources
            else f"DirectionBias={direction_bias} from cached news sentiment and catalyst flags."
        )
        if gap_risk_unavailable_reason:
            gap_line = (
                f"{symbol}: GapRisk unavailable — {gap_risk_unavailable_reason}"
            )
        else:
            gap_line = (
                f"{symbol}: GapRisk={gap_risk} from deterministic weighted contributors."
            )
        out = [
            gap_line,
            direction_line,
            f"News intensity in lookback: {news_count} cached items.",
        ]
        if severity_label:
            out.append(f"Highest catalyst severity: {severity_label}.")
        if event:
            out.append(
                "Earnings event context: "
                f"{event.get('date_formatted') or event.get('date') or 'date n/a'} "
                f"({self._normalize_session(event.get('when'))})."
            )
        if mismatch:
            out.append("Calendar mismatch detected: news implies earnings but calendar entry is missing near event window.")
        if contributors:
            top = sorted(contributors, key=lambda c: float(c.get("weight", 0.0)), reverse=True)[:1]
            for c in top:
                out.append(f"Top contributor: {c.get('name')} ({c.get('note')}).")
        return out[:5]

    @staticmethod
    def _build_what_to_watch(
        direction_bias: int,
        severity_label: str,
        mismatch: bool,
        event: Dict[str, Any],
    ) -> List[str]:
        out = [
            "Compare reported EPS/revenue against consensus and management guidance tone.",
            "Track opening print vs prior close for immediate gap follow-through or fade.",
        ]
        if severity_label.startswith("NEGATIVE"):
            out.append("Watch for downside continuation catalysts (offering/guidance/legal updates).")
        elif severity_label.startswith("POSITIVE"):
            out.append("Watch for sustained upside only if volume confirms catalyst quality.")
        if direction_bias <= -40:
            out.append("Monitor borrow availability and short squeeze risk before sizing short exposure.")
        if direction_bias >= 40:
            out.append("Monitor post-call guidance confirmation before extending long exposure.")
        if mismatch:
            out.append("Verify earnings date/time from issuer relations: calendar mismatch flag is active.")
        if event and event.get("when"):
            out.append(f"Session timing matters: {str(event.get('when')).upper()} can change post-earnings liquidity.")
        return out[:5]

    @staticmethod
    def _build_bias_reasons(
        direction_bias: int,
        news_sentiment: float,
        catalyst_flags: float,
        analyst_proxy: float,
        earnings_trend: Optional[float],
    ) -> List[str]:
        reasons = [
            f"Composite bias score = {direction_bias} (range -100..100).",
            f"News sentiment proxy contribution: {round(news_sentiment, 3)}.",
            f"Catalyst flag contribution: {round(catalyst_flags, 3)}.",
            f"Analyst revision proxy: {round(analyst_proxy, 3)}.",
        ]
        if earnings_trend is not None:
            reasons.append(f"Earnings trend proxy: {round(earnings_trend, 3)}.")
        else:
            reasons.append("Earnings trend proxy unavailable in local cache.")
        return reasons[:5]

    def _feedback_template(self) -> Dict[str, Any]:
        return {
            "version": 1,
            "updated_utc": _iso(_utc_now()),
            "open_predictions": {},
            "outcomes": [],
            "weights": {},
            "last_recalibration_quarter": None,
            "calibration_model": {},
        }

    def _feedback_guard(self) -> threading.RLock:
        """Return the feedback-store lock, including for lightweight test agents."""
        lock = getattr(self, "_feedback_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._feedback_lock = lock
        return lock

    @staticmethod
    def _feedback_outcome_event_key(row: Dict[str, Any]) -> Optional[Tuple[str, str]]:
        if not isinstance(row, dict):
            return None
        symbol = str(row.get("symbol") or "").upper().strip()
        event_dt = _parse_iso(row.get("event_utc"))
        if not symbol or event_dt is None:
            return None
        return symbol, event_dt.date().isoformat()

    @classmethod
    def _dedupe_feedback_outcomes(cls, outcomes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Collapse timestamp variants of one earnings event into one rich row."""
        keyed: Dict[Tuple[str, str], Dict[str, Any]] = {}
        order: List[Tuple[str, Any]] = []
        unkeyed_index = 0
        for raw in outcomes or []:
            if not isinstance(raw, dict):
                continue
            row = copy.deepcopy(raw)
            key = cls._feedback_outcome_event_key(row)
            if key is None:
                order.append(("unkeyed", unkeyed_index))
                keyed[("__unkeyed__", str(unkeyed_index))] = row
                unkeyed_index += 1
                continue
            if key not in keyed:
                keyed[key] = row
                order.append(("keyed", key))
                continue

            existing = keyed[key]
            existing_dt = _parse_iso(existing.get("evaluation_utc") or existing.get("event_utc"))
            row_dt = _parse_iso(row.get("evaluation_utc") or row.get("event_utc"))
            if row_dt and (existing_dt is None or row_dt >= existing_dt):
                primary, secondary = row, existing
            else:
                primary, secondary = existing, row
            merged = dict(primary)
            for field, value in secondary.items():
                if field not in merged or merged.get(field) in (None, "", [], {}):
                    merged[field] = copy.deepcopy(value)
            merged["was_top_prediction"] = bool(
                existing.get("was_top_prediction") or row.get("was_top_prediction")
            )
            for rank_field in (
                "top_prediction_rank",
                "top_prediction_entry_rank",
                "top_prediction_best_rank",
            ):
                ranks = [
                    _safe_int(candidate.get(rank_field), 0)
                    for candidate in (existing, row)
                    if _safe_int(candidate.get(rank_field), 0) > 0
                ]
                if ranks:
                    merged[rank_field] = min(ranks)
            keyed[key] = merged

        deduped: List[Dict[str, Any]] = []
        for kind, value in order:
            lookup = value if kind == "keyed" else ("__unkeyed__", str(value))
            row = keyed.get(lookup)
            if isinstance(row, dict):
                deduped.append(row)
        return deduped

    def get_feedback_snapshot(self, *, deduplicate_outcomes: bool = True) -> Dict[str, Any]:
        with self._feedback_guard():
            snapshot = copy.deepcopy(self._feedback)
        if deduplicate_outcomes:
            snapshot["outcomes"] = self._dedupe_feedback_outcomes(
                list(snapshot.get("outcomes", []) or [])
            )
        return snapshot

    def _load_feedback_store(self) -> Dict[str, Any]:
        template = self._feedback_template()
        try:
            if PRE_EARNINGS_FEEDBACK_PATH.exists():
                with PRE_EARNINGS_FEEDBACK_PATH.open("r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    merged = dict(template)
                    merged.update(loaded)
                    if not isinstance(merged.get("open_predictions"), dict):
                        merged["open_predictions"] = {}
                    if not isinstance(merged.get("outcomes"), list):
                        merged["outcomes"] = []
                    else:
                        # Backfill legacy NaN/Inf realized-move rows (pre-audit-B1
                        # the store could emit literal `NaN`/`Infinity` tokens,
                        # invalid RFC 8259 JSON). Sanitize to None so metrics and
                        # the UI never render `NaN%`. See Task #11 / audit B1.
                        merged["outcomes"] = self._dedupe_feedback_outcomes(
                            self._sanitize_outcome_moves(merged["outcomes"])
                        )
                    if not isinstance(merged.get("weights"), dict):
                        merged["weights"] = {}
                    return merged
        except Exception as e:
            logger.warning(f"Pre-earnings feedback load failed: {e}")
        return template

    @staticmethod
    def _sanitize_outcome_moves(outcomes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        clean: List[Dict[str, Any]] = []
        for o in outcomes or []:
            if not isinstance(o, dict):
                continue
            row = dict(o)
            for k in ("realized_signed_move_pct", "realized_gap_move_pct"):
                v = row.get(k)
                if v is None:
                    continue
                try:
                    fv = float(v)
                except (TypeError, ValueError):
                    row[k] = None
                    continue
                if not math.isfinite(fv):
                    row[k] = None
            clean.append(row)
        return clean

    def _persist_feedback_store(self) -> None:
        with self._feedback_guard():
            PRE_EARNINGS_FEEDBACK_PATH.parent.mkdir(parents=True, exist_ok=True)
            temp_path = PRE_EARNINGS_FEEDBACK_PATH.with_suffix(
                f".{os.getpid()}.{threading.get_ident()}.tmp"
            )
            try:
                outcomes = self._dedupe_feedback_outcomes(
                    list(self._feedback.get("outcomes", []) or [])
                )
                if len(outcomes) > 4000:
                    self._archive_outcomes(outcomes[:-4000])
                self._feedback["outcomes"] = outcomes[-4000:]
                self._feedback["updated_utc"] = _iso(_utc_now())
                snapshot = copy.deepcopy(self._feedback)
                with temp_path.open("w", encoding="utf-8") as f:
                    json.dump(snapshot, f, indent=2, ensure_ascii=False, allow_nan=False)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(str(temp_path), str(PRE_EARNINGS_FEEDBACK_PATH))
            except Exception as e:
                logger.error(f"Pre-earnings feedback persist failed: {e}")
                try:
                    if temp_path.exists():
                        temp_path.unlink()
                except Exception:
                    pass

    def _recent_outcomes(self, cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
        lookback_days = max(30, _safe_int(cfg.get("feedback_lookback_days"), 365))
        cutoff = _utc_now() - timedelta(days=lookback_days)
        out = []
        feedback = self.get_feedback_snapshot(deduplicate_outcomes=True)
        for row in list(feedback.get("outcomes", []) or []):
            if not isinstance(row, dict):
                continue
            dt = _parse_iso(row.get("evaluation_utc") or row.get("event_utc"))
            if dt is None or dt < cutoff:
                continue
            out.append(row)
        return out

    def _evaluate_matured_feedback_predictions(self, cfg: Dict[str, Any]) -> bool:
        changed = False
        now = _utc_now()
        open_predictions = self._feedback.setdefault("open_predictions", {})
        outcomes = self._feedback.setdefault("outcomes", [])
        high_gap_threshold = _safe_float(cfg.get("high_gap_threshold_pct"), 5.0) or 5.0

        stale_keys: List[str] = []
        for key, pred in list(open_predictions.items()):
            if not isinstance(pred, dict):
                stale_keys.append(key)
                continue
            event_dt = _parse_iso(pred.get("event_utc"))
            if event_dt is None:
                stale_keys.append(key)
                continue

            evaluation_dt = event_dt + timedelta(hours=36)
            if now < evaluation_dt:
                continue

            symbol = str(pred.get("symbol") or "").upper().strip()
            session = self._normalize_session(pred.get("session"))
            signed_move_pct = self._compute_event_gap_move_pct(symbol, event_dt, session)

            # Defense in depth: a non-finite (NaN/Inf) move is treated as
            # unresolved — never persisted as an outcome (would emit a `NaN`
            # literal into the JSON store). See audit B1.
            if signed_move_pct is not None and not math.isfinite(float(signed_move_pct)):
                signed_move_pct = None

            # Price cache may lag; drop very old unresolved predictions to keep store bounded.
            if signed_move_pct is None:
                if now > (event_dt + timedelta(days=21)):
                    stale_keys.append(key)
                    changed = True
                continue

            realized_move_pct = abs(float(signed_move_pct))
            # Peer grouping for the settled history row (owner requirement,
            # 2026-11): reuse the prediction's key, re-resolving legacy
            # UNKNOWNs through the theme->industry->sector chain (estimates
            # cache, budget-respecting) so the read-through join keeps working.
            peer_key_value, peer_key_label_value = self._settlement_peer_key(pred, symbol, cfg)
            outcome = {
                "prediction_key": key,
                "symbol": symbol,
                "event_utc": _iso(event_dt),
                "evaluation_utc": _iso(now),
                "session": session,
                "sector": pred.get("sector") or "UNKNOWN",
                "peer_key": peer_key_value,
                "peer_key_label": peer_key_label_value,
                "cap_bucket": pred.get("cap_bucket") or "UNKNOWN",
                "gap_risk_available": pred.get("gap_risk_available") is not False,
                "predicted_gap_score_raw": pred.get("predicted_gap_score_raw"),
                "predicted_gap_risk": _safe_int(pred.get("predicted_gap_risk"), 0),
                "predicted_gap_score": _safe_int(pred.get("predicted_gap_score"), _safe_int(pred.get("predicted_gap_risk"), 0)),
                "predicted_direction_bias": _safe_int(pred.get("predicted_direction_bias"), 0),
                "predicted_trader_score": _safe_int(pred.get("predicted_trader_score"), 0),
                "predicted_trader_score_band": pred.get("predicted_trader_score_band"),
                "prediction_score_version": pred.get("prediction_score_version"),
                "recommendation_eligible": bool(pred.get("recommendation_eligible")),
                "top_prediction_rank": pred.get("top_prediction_rank"),
                "was_top_prediction": bool(pred.get("was_top_prediction")),
                "top_prediction_first_seen_utc": pred.get("top_prediction_first_seen_utc"),
                "top_prediction_direction": pred.get("top_prediction_direction"),
                "top_prediction_bias": pred.get("top_prediction_bias"),
                "top_prediction_trader_score": pred.get("top_prediction_trader_score"),
                "top_prediction_score_band": pred.get("top_prediction_score_band"),
                "top_prediction_entry_rank": pred.get("top_prediction_entry_rank"),
                "top_prediction_best_rank": pred.get("top_prediction_best_rank"),
                "top_prediction_latest_rank": pred.get("top_prediction_latest_rank"),
                "top_prediction_snapshot_quality": pred.get("top_prediction_snapshot_quality"),
                "predicted_prob_high_gap": _safe_float(pred.get("predicted_prob_high_gap"), 0.0),
                "contributors": dict(pred.get("contributors") or {}),
                "realized_gap_move_pct": round(realized_move_pct, 6),
                "realized_signed_move_pct": round(float(signed_move_pct), 6),
                "realized_high_gap": bool(realized_move_pct >= high_gap_threshold),
                "high_gap_threshold_pct": float(high_gap_threshold),
            }
            top_side = str(outcome.get("top_prediction_direction") or "").upper().strip()
            if top_side not in {"LONG", "SHORT"} and outcome["was_top_prediction"]:
                top_bias = _safe_int(
                    outcome.get("top_prediction_bias"),
                    outcome["predicted_direction_bias"],
                )
                top_side = "LONG" if top_bias >= 15 else ("SHORT" if top_bias <= -15 else "")
                outcome["top_prediction_direction"] = top_side or None
            outcome["top_prediction_hit"] = (
                ((top_side == "LONG" and float(signed_move_pct) > 0)
                 or (top_side == "SHORT" and float(signed_move_pct) < 0))
                if outcome["was_top_prediction"] and top_side and float(signed_move_pct) != 0.0
                else None
            )
            # Echo AI provenance from the open prediction onto the outcome so
            # the track-record can slice historical accuracy by AI verdict and
            # confidence band. Legacy predictions (no ai fields) -> None.
            outcome["predicted_ai_verdict"] = pred.get("predicted_ai_verdict")
            outcome["predicted_ai_confidence"] = _safe_float(pred.get("predicted_ai_confidence"), None)
            outcome["predicted_ai_catalyst"] = pred.get("predicted_ai_catalyst")
            outcome["predicted_ai_eligible"] = pred.get("predicted_ai_eligible")
            outcome["ai_model"] = pred.get("ai_model")
            outcome["predicted_news_articles_read"] = pred.get("predicted_news_articles_read")
            outcome["predicted_news_sources_read"] = pred.get("predicted_news_sources_read")
            if not outcome["predicted_ai_verdict"]:
                # Provenance repair (audit P1-6): the prediction may have been
                # registered before AI provenance was persisted, while the
                # durable per-event AI registry still holds the verdict the
                # user was shown for this exact earnings event. Backfill from
                # the sidecar so settled outcomes keep monotonic AI provenance
                # (ai_coverage > 0) instead of losing it to null.
                cached_ai_entry = self._lookup_cached_ai_for_item(
                    {"symbol": symbol, "earnings_time_utc": _iso(event_dt)}
                )
                cached_ai = (cached_ai_entry or {}).get("ai_decision")
                cached_saved_dt = _parse_iso(
                    (cached_ai_entry or {}).get("saved_at")
                    or (cached_ai.get("generated_at") if isinstance(cached_ai, dict) else None)
                )
                # Only a verdict written before the event is a prediction.
                if (
                    isinstance(cached_ai, dict)
                    and cached_ai.get("used")
                    and cached_saved_dt is not None
                    and cached_saved_dt < event_dt
                ):
                    outcome["predicted_ai_verdict"] = str(cached_ai.get("verdict") or "").strip() or None
                    outcome["predicted_ai_confidence"] = _safe_float(cached_ai.get("confidence"), None)
                    outcome["predicted_ai_catalyst"] = str(cached_ai.get("catalyst_label") or "").strip() or None
                    outcome["ai_model"] = str(cached_ai.get("model") or "").strip() or None
                    outcome["predicted_ai_provenance"] = "ai_cache_backfill"
            for evidence_key in ("first_direction_evidence", "top_direction_evidence"):
                outcome[evidence_key] = copy.deepcopy(pred.get(evidence_key))
            from core.pre_earnings_learning import SHADOW_FIELDS
            from core.estimate_revisions import FIELDS as REVISION_FIELDS
            from core.pre_event_features import PRICE_FEATURE_FIELDS
            for shadow_key in SHADOW_FIELDS + REVISION_FIELDS + PRICE_FEATURE_FIELDS:
                if pred.get(shadow_key) is not None:
                    outcome[shadow_key] = pred.get(shadow_key)
            outcomes.append(outcome)
            stale_keys.append(key)
            changed = True

        for key in stale_keys:
            open_predictions.pop(key, None)

        # Collapse legacy timestamp variants before enforcing the storage cap,
        # preventing one real report from influencing calibration repeatedly.
        deduped_outcomes = self._dedupe_feedback_outcomes(outcomes)
        if len(deduped_outcomes) != len(outcomes):
            changed = True
        if len(deduped_outcomes) > 4000:
            deduped_outcomes.sort(
                key=lambda r: _parse_iso(r.get("evaluation_utc") or r.get("event_utc"))
                or datetime.min.replace(tzinfo=timezone.utc)
            )
            deduped_outcomes = deduped_outcomes[-4000:]
            changed = True
        self._feedback["outcomes"] = deduped_outcomes

        return changed

    def _retro_backfill_outcome_ai_provenance(self) -> bool:
        state_lock = getattr(self, "_lock", None)
        if state_lock is not None:
            with state_lock:
                cache_snapshot = copy.deepcopy(getattr(self, "_ai_cache", {}) or {})
        else:
            cache_snapshot = copy.deepcopy(getattr(self, "_ai_cache", {}) or {})
        if not cache_snapshot:
            return False
        with self._feedback_guard():
            return self._retro_backfill_outcome_ai_provenance_locked(cache_snapshot)

    def _retro_backfill_outcome_ai_provenance_locked(
        self,
        cache_snapshot: Dict[str, Any],
    ) -> bool:
        """One-time, bounded retro repair of settled outcomes lacking an AI
        verdict (re-audit P1).

        Outcomes settled before AI provenance was persisted (and before the
        settlement-time backfill above existed) carry
        ``predicted_ai_verdict=None`` forever, so the track-record
        ``ai_coverage`` read 0 for weeks even though the durable per-event AI
        registry still holds the verdict the user was shown for those exact
        earnings events. For each settled outcome without a verdict, look the
        registry up by the same symbol + same event window (<=14-day drift —
        the same rule the settlement backfill and ``_lookup_cached_ai_for_item``
        use) and fill verdict/confidence/catalyst/model with
        ``predicted_ai_provenance="ai_cache_backfill"``. Honest by
        construction: no matching registry entry (or a used entry without a
        verdict) leaves the outcome null. Cheap by construction: the registry
        is indexed once per call and matching is O(n) over outcomes; the
        feedback store is persisted through the normal atomic path ONLY when
        at least one outcome actually changed (second and later calls are
        pure no-ops). Returns True when anything was repaired.
        """
        outcomes = self._feedback.get("outcomes", []) or []
        if not outcomes:
            return False

        # Index the durable per-event registry once per call: symbol ->
        # [(event_dt, saved_dt, ai_decision)]. Only valid (used) analyses are
        # eligible — the same validity gate `_load_ai_cache` applies.
        registry: Dict[str, List[Tuple[Optional[datetime], datetime, Dict[str, Any]]]] = {}
        for entry in cache_snapshot.values():
            if not isinstance(entry, dict):
                continue
            ai = entry.get("ai_decision")
            if not isinstance(ai, dict) or not ai.get("used"):
                continue
            symbol = str(entry.get("symbol") or "").upper().strip()
            if not symbol:
                continue
            event_dt = _parse_iso(entry.get("event_utc") or ai.get("event_utc"))
            saved_dt = (
                _parse_iso(entry.get("saved_at") or ai.get("generated_at"))
                or datetime.min.replace(tzinfo=timezone.utc)
            )
            registry.setdefault(symbol, []).append((event_dt, saved_dt, ai))
        if not registry:
            return False

        changed = False
        for o in outcomes:
            if not isinstance(o, dict) or o.get("predicted_ai_verdict"):
                continue  # already carries a verdict — provenance is monotonic
            symbol = str(o.get("symbol") or "").upper().strip()
            event_dt = _parse_iso(o.get("event_utc"))
            if not symbol or event_dt is None:
                continue  # cannot prove the same-event relationship -> skip
            best: Optional[Tuple[datetime, Dict[str, Any]]] = None
            for cached_event, saved_dt, ai in registry.get(symbol, []):
                if cached_event is None:
                    continue
                if abs((event_dt - cached_event).total_seconds()) > 14 * 86400:
                    continue  # different quarterly event — never inherit
                if saved_dt >= event_dt or saved_dt.year <= 1:
                    continue  # written after the event (or undated): hindsight
                if best is None or saved_dt > best[0]:
                    best = (saved_dt, ai)
            if best is None:
                continue  # honest null: no registry verdict for this event
            ai = best[1]
            verdict = str(ai.get("verdict") or "").strip() or None
            if verdict is None:
                continue  # a used entry without a verdict proves nothing
            o["predicted_ai_verdict"] = verdict
            o["predicted_ai_confidence"] = _safe_float(ai.get("confidence"), None)
            o["predicted_ai_catalyst"] = str(ai.get("catalyst_label") or "").strip() or None
            o["ai_model"] = str(ai.get("model") or "").strip() or None
            o["predicted_ai_provenance"] = "ai_cache_backfill"
            changed = True

        if changed:
            self._persist_feedback_store()
        return changed

    def _register_open_predictions(self, items: List[Dict[str, Any]], cfg: Dict[str, Any]) -> bool:
        changed = False
        high_gap_threshold = _safe_float(cfg.get("high_gap_threshold_pct"), 5.0) or 5.0
        open_predictions = self._feedback.setdefault("open_predictions", {})
        registration_now = _utc_now()
        registration_now_iso = _iso(registration_now)
        ranked_shortlist = self._build_top_predictions(items, cfg, now=registration_now)
        shortlist_by_event = {
            (
                str(row.get("symbol") or "").upper().strip(),
                str(row.get("earnings_time_utc") or "")[:10],
            ): row
            for row in ranked_shortlist
            if isinstance(row, dict)
        }

        for item in items or []:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol") or "").upper().strip()
            event_utc = _parse_iso(item.get("earnings_time_utc"))
            if not symbol or event_utc is None:
                continue
            key = f"{symbol}|{_iso(event_utc)}"
            contributors = {}
            for row in list(item.get("contributors", []) or []):
                if not isinstance(row, dict):
                    continue
                name = str(row.get("name") or "").strip()
                if not name:
                    continue
                val = _safe_float(row.get("value"), None)
                if val is None:
                    continue
                contributors[name] = float(val)

            event_day_key = _iso(event_utc)[:10] if _iso(event_utc) else ""
            top_row = shortlist_by_event.get((symbol, event_day_key))
            current_top_rank = _safe_int(top_row.get("rank"), 0) if top_row else 0
            payload_core = {
                "symbol": symbol,
                "event_utc": _iso(event_utc),
                "session": self._normalize_session(item.get("session")),
                "sector": str(item.get("sector") or "UNKNOWN"),
                # Peer grouping (owner requirement, 2026-11): carried onto the
                # settled outcome so history groups by theme/sector; "UNKNOWN"
                # is re-resolved at settlement via the theme->industry->sector
                # chain instead of staying a permanent blind spot.
                "peer_key": str(item.get("peer_key") or "UNKNOWN"),
                "peer_key_label": (str(item.get("peer_key_label") or "").strip() or None),
                "cap_bucket": str(item.get("market_cap_bucket") or _cap_bucket(_safe_float(item.get("market_cap"), None))),
                # False when there was no history to score: the 0 below is a
                # placeholder then, never a 0% forecast.
                "gap_risk_available": item.get("gap_risk") is not None,
                "predicted_gap_score_raw": item.get("gap_risk_raw"),
                "predicted_gap_risk": _safe_int(item.get("gap_risk"), 0),
                "predicted_gap_score": _safe_int(item.get("gap_risk"), 0),
                "predicted_direction_bias": direction_bias_score_from_item(item),
                "predicted_trader_score": _safe_int(item.get("trader_score"), 0),
                "predicted_trader_score_band": item.get("trader_score_band"),
                "prediction_score_version": "two_stage_evidence_weighted_v2",
                "recommendation_eligible": bool(item.get("recommendation_eligible")),
                "recommendation_blockers": list(item.get("recommendation_blockers") or []),
                "top_prediction_rank": current_top_rank or None,
                "was_top_prediction": bool(top_row),
                "predicted_prob_high_gap": round(_safe_int(item.get("gap_risk"), 0) / 100.0, 6),
                "contributors": contributors,
                "high_gap_threshold_pct": float(high_gap_threshold),
            }
            # Persist AI provenance (verdict/confidence/catalyst/model) so matured
            # outcomes can be sliced by AI verdict and confidence band. Items
            # without an ai_decision block (AI off / not enriched) carry None,
            # which keeps them in the overall count but excludes them from
            # per-AI slices. See Task #11 (History / Track-Record panel).
            ai = item.get("ai_decision") if isinstance(item.get("ai_decision"), dict) else None
            if ai and ai.get("used"):
                payload_core["predicted_ai_verdict"] = str(ai.get("verdict") or "").strip() or None
                payload_core["predicted_ai_confidence"] = _safe_float(ai.get("confidence"), None)
                payload_core["predicted_ai_catalyst"] = str(ai.get("catalyst_label") or "").strip() or None
                payload_core["predicted_ai_eligible"] = ai.get("qualifies_pre_earnings")
                payload_core["ai_model"] = str(ai.get("model") or "").strip() or None
                articles_read = _safe_int(ai.get("news_articles_read"), -1)
                sources_read = _safe_int(ai.get("news_sources_read"), -1)
                payload_core["predicted_news_articles_read"] = articles_read if articles_read >= 0 else None
                payload_core["predicted_news_sources_read"] = sources_read if sources_read >= 0 else None
            else:
                payload_core["predicted_ai_verdict"] = None
                payload_core["predicted_ai_confidence"] = None
                payload_core["predicted_ai_catalyst"] = None
                payload_core["predicted_ai_eligible"] = None
                payload_core["ai_model"] = None
                payload_core["predicted_news_articles_read"] = None
                payload_core["predicted_news_sources_read"] = None

            prev = open_predictions.get(key)
            from core.pre_earnings_evidence import attach_frozen_evidence
            attach_frozen_evidence(
                payload_core, prev, item, cfg, registration_now_iso, bool(top_row)
            )
            from core.pre_earnings_learning import SHADOW_FIELDS, shadow_predictions
            if registration_now < event_utc:
                # Learned models are shadow-only: recorded before the event with
                # a model fitted on outcomes already settled, never traded.
                payload_core.update(
                    shadow_predictions(self._feedback.get("learning_model"), payload_core)
                )
                revision_cache, price_cache = self._pre_event_feature_caches()
                revision_snapshot = revision_cache.get(symbol, self._REVISION_MAX_AGE_SECONDS)
                if revision_snapshot:
                    payload_core.update(revision_snapshot)
                price_features = (price_cache.get(symbol) or {}).get("features")
                if price_features:
                    payload_core.update(price_features)
            from core.estimate_revisions import FIELDS as REVISION_FIELDS
            from core.pre_event_features import PRICE_FEATURE_FIELDS
            if isinstance(prev, dict):
                for shadow_key in SHADOW_FIELDS + REVISION_FIELDS + PRICE_FEATURE_FIELDS:
                    if shadow_key not in payload_core and prev.get(shadow_key) is not None:
                        payload_core[shadow_key] = prev.get(shadow_key)
            if isinstance(prev, dict):
                # A transient structured-only rebuild must not erase AI
                # provenance already registered for this exact earnings event.
                # This feedback record becomes the settled audit trail after
                # the report, so null is never a fresher fact than a valid
                # saved verdict. A genuinely fresh AI block still replaces all
                # fields above.
                if not (ai and ai.get("used")) and prev.get(
                    "predicted_ai_verdict"
                ):
                    for ai_key in (
                        "predicted_ai_verdict",
                        "predicted_ai_confidence",
                        "predicted_ai_catalyst",
                        "predicted_ai_eligible",
                        "ai_model",
                        "predicted_news_articles_read",
                        "predicted_news_sources_read",
                    ):
                        payload_core[ai_key] = prev.get(ai_key)

                # Top-10 provenance is monotonic. Once a recommendation was
                # displayed in the shortlist, a later rank change or temporary
                # loss of eligibility must not erase what the trader saw.
                if bool(prev.get("was_top_prediction")):
                    payload_core["was_top_prediction"] = True
                    previous_best = _safe_int(
                        prev.get("top_prediction_best_rank"),
                        _safe_int(prev.get("top_prediction_rank"), 0),
                    )
                    best_rank = min(
                        rank for rank in (previous_best, current_top_rank) if rank > 0
                    ) if previous_best > 0 or current_top_rank > 0 else 0
                    legacy_top_bias = _safe_int(
                        prev.get("top_prediction_bias"),
                        _safe_int(prev.get("predicted_direction_bias"), 0),
                    )
                    current_top_side = str((top_row or {}).get("side") or "").upper().strip()
                    legacy_top_side = str(prev.get("top_prediction_direction") or "").upper().strip()
                    if legacy_top_side not in {"LONG", "SHORT"}:
                        legacy_top_side = current_top_side if current_top_side in {"LONG", "SHORT"} else (
                            "LONG" if legacy_top_bias >= 15 else ("SHORT" if legacy_top_bias <= -15 else "")
                        )
                    snapshot_quality = prev.get("top_prediction_snapshot_quality") or "legacy_reconstructed"
                    payload_core.update({
                        "top_prediction_first_seen_utc": prev.get("top_prediction_first_seen_utc") or registration_now_iso,
                        "top_prediction_direction": legacy_top_side or None,
                        "top_prediction_bias": legacy_top_bias,
                        "top_prediction_trader_score": prev.get("top_prediction_trader_score") if prev.get("top_prediction_trader_score") is not None else (
                            _safe_int((top_row or {}).get("trader_score"), 0) if top_row else _safe_int(prev.get("predicted_trader_score"), 0)
                        ),
                        "top_prediction_score_band": prev.get("top_prediction_score_band") or (
                            (top_row or {}).get("score_band") if top_row else prev.get("predicted_trader_score_band")
                        ),
                        "top_prediction_entry_rank": prev.get("top_prediction_entry_rank") or prev.get("top_prediction_rank"),
                        "top_prediction_best_rank": best_rank or None,
                        "top_prediction_latest_rank": current_top_rank or prev.get("top_prediction_latest_rank") or prev.get("top_prediction_rank"),
                        "top_prediction_rank": best_rank or prev.get("top_prediction_rank"),
                        "top_prediction_snapshot_quality": snapshot_quality,
                    })
                elif top_row:
                    top_side = str(top_row.get("side") or item.get("direction_label") or "").upper().strip()
                    top_bias = direction_bias_score_from_item(item)
                    payload_core.update({
                        "top_prediction_first_seen_utc": registration_now_iso,
                        "top_prediction_direction": top_side if top_side in {"LONG", "SHORT"} else None,
                        "top_prediction_bias": top_bias,
                        "top_prediction_trader_score": _safe_int(top_row.get("trader_score"), 0),
                        "top_prediction_score_band": top_row.get("score_band"),
                        "top_prediction_entry_rank": current_top_rank,
                        "top_prediction_best_rank": current_top_rank,
                        "top_prediction_latest_rank": current_top_rank,
                        "top_prediction_snapshot_quality": "captured_at_entry",
                    })
                prev_core = {k: prev.get(k) for k in payload_core.keys()}
                if prev_core == payload_core:
                    continue
                payload = dict(payload_core)
                payload["created_utc"] = prev.get("created_utc") or _iso(_utc_now())
                payload["updated_utc"] = _iso(_utc_now())
                open_predictions[key] = payload
                changed = True
            else:
                payload = dict(payload_core)
                if top_row:
                    top_side = str(top_row.get("side") or item.get("direction_label") or "").upper().strip()
                    top_bias = direction_bias_score_from_item(item)
                    payload.update({
                        "top_prediction_first_seen_utc": registration_now_iso,
                        "top_prediction_direction": top_side if top_side in {"LONG", "SHORT"} else None,
                        "top_prediction_bias": top_bias,
                        "top_prediction_trader_score": _safe_int(top_row.get("trader_score"), 0),
                        "top_prediction_score_band": top_row.get("score_band"),
                        "top_prediction_entry_rank": current_top_rank,
                        "top_prediction_best_rank": current_top_rank,
                        "top_prediction_latest_rank": current_top_rank,
                        "top_prediction_snapshot_quality": "captured_at_entry",
                    })
                payload["created_utc"] = registration_now_iso
                payload["updated_utc"] = payload["created_utc"]
                open_predictions[key] = payload
                changed = True
        return changed

    def _recalibrate_gap_weights(self, outcomes: List[Dict[str, Any]]) -> Optional[Dict[str, float]]:
        if not outcomes:
            return None
        base = {
            "historical_move": 0.25,
            "news_intensity": 0.10,
            "catalyst_severity": 0.20,
            "liquidity_risk": 0.10,
            "implied_move": 0.35,
        }
        signals = {k: [] for k in base.keys()}
        for row in outcomes:
            if not isinstance(row, dict):
                continue
            y = _safe_float(row.get("realized_gap_move_pct"), None)
            if y is None:
                continue
            # Normalize realized gap into 0..1 target.
            y_norm = _clamp(y / 15.0, 0.0, 1.0)
            contrib = dict(row.get("contributors") or {})
            for name in signals.keys():
                x = _safe_float(contrib.get(name), None)
                if x is None:
                    continue
                signals[name].append(abs(float(x) - y_norm))

        tuned: Dict[str, float] = {}
        for name, base_weight in base.items():
            errs = signals.get(name, [])
            if len(errs) < 12:
                tuned[name] = base_weight
                continue
            mae = sum(errs) / max(1, len(errs))
            quality = _clamp(1.0 - mae, 0.05, 0.95)
            tuned[name] = base_weight * (0.7 + 0.6 * quality)

        total = sum(tuned.values()) or 1.0
        return {k: float(v) / total for k, v in tuned.items()}

    def _get_active_gap_weights(self, cfg: Dict[str, Any]) -> Tuple[Dict[str, float], bool]:
        changed = False
        base = {
            "historical_move": 0.25,
            "news_intensity": 0.10,
            "catalyst_severity": 0.20,
            "liquidity_risk": 0.10,
            "implied_move": 0.35,
        }
        out = dict(base)
        min_samples = max(20, _safe_int(cfg.get("recalibration_min_samples"), 40))
        outcomes = self._recent_outcomes(cfg)
        quarter = _quarter_key()
        last_quarter = str(self._feedback.get("last_recalibration_quarter") or "")

        stored = self._feedback.get("weights")
        if isinstance(stored, dict) and stored:
            for key in out.keys():
                val = _safe_float(stored.get(key), None)
                if val is not None and val > 0:
                    out[key] = val
            total = sum(out.values()) or 1.0
            out = {k: float(v) / total for k, v in out.items()}

        if len(outcomes) >= min_samples and quarter != last_quarter:
            tuned = self._recalibrate_gap_weights(outcomes)
            if isinstance(tuned, dict) and tuned:
                out = dict(tuned)
                self._feedback["weights"] = dict(tuned)
                self._feedback["last_recalibration_quarter"] = quarter
                changed = True
        return out, changed

    _REVISION_MAX_AGE_SECONDS = 12 * 3600
    _PRICE_FEATURE_MAX_AGE_SECONDS = 6 * 3600

    def _pre_event_feature_caches(self):
        from core.estimate_revisions import RevisionSnapshotCache

        if getattr(self, "_estimate_revision_cache", None) is None:
            self._estimate_revision_cache = RevisionSnapshotCache()
        if getattr(self, "_price_feature_cache", None) is None:
            self._price_feature_cache = {}
        return self._estimate_revision_cache, self._price_feature_cache

    def _refresh_pre_event_features(
        self, items: List[Dict[str, Any]], cfg: Dict[str, Any], *, now: Optional[datetime] = None
    ) -> int:
        """Fetch revision snapshots and relative returns for the nearest upcoming
        events, within a per-cycle budget. Network happens here, never under the
        feedback lock; registration only reads the caches."""
        if not bool(cfg.get("estimate_revision_capture_enabled", True)):
            return 0
        if os.environ.get("TEST_MODE") == "1" or os.environ.get("PYTEST_CURRENT_TEST"):
            return 0
        from concurrent.futures import ThreadPoolExecutor

        from core import estimate_revisions, pre_event_features

        current = now or _utc_now()
        horizon = current + timedelta(days=max(1, _safe_int(cfg.get("pre_event_feature_lookahead_days"), 7)))
        budget = max(0, _safe_int(cfg.get("pre_event_feature_max_fetch_per_cycle"), 40))
        revision_cache, price_cache = self._pre_event_feature_caches()
        upcoming = []
        for item in items or []:
            event = _parse_iso(item.get("earnings_time_utc")) if isinstance(item, dict) else None
            symbol = str((item or {}).get("symbol") or "").upper().strip()
            if symbol and event is not None and current < event <= horizon:
                upcoming.append((event, symbol))
        symbols = list(dict.fromkeys(symbol for _, symbol in sorted(upcoming)))
        stale_revisions = [
            s for s in symbols if not revision_cache.is_fresh(s, self._REVISION_MAX_AGE_SECONDS)
        ][:budget]
        stale_prices = [
            s for s in symbols
            if current.timestamp() - float((price_cache.get(s) or {}).get("ts", 0.0)) > self._PRICE_FEATURE_MAX_AGE_SECONDS
        ][:budget]
        if not stale_revisions and not stale_prices:
            return 0
        benchmark = self._load_symbol_daily_bars("SPY") if stale_prices else []

        def _price_features(symbol: str):
            return pre_event_features.relative_returns(self._load_symbol_daily_bars(symbol), benchmark, current)

        with ThreadPoolExecutor(max_workers=4) as pool:
            for symbol, snapshot in zip(stale_revisions, pool.map(estimate_revisions.fetch_snapshot, stale_revisions)):
                revision_cache.put(symbol, snapshot)
            for symbol, features in zip(stale_prices, pool.map(_price_features, stale_prices)):
                price_cache[symbol] = {"ts": current.timestamp(), "features": features}
        return len(stale_revisions) + len(stale_prices)

    def _outcome_archive_path(self) -> Path:
        return PRE_EARNINGS_FEEDBACK_PATH.with_name("pre_earnings_outcomes_archive.jsonl")

    def _archive_outcomes(self, rows: List[Dict[str, Any]]) -> None:
        """Append outcomes trimmed from the live store. Learning needs history."""
        if not rows:
            return
        archive = self._outcome_archive_path()
        archive.parent.mkdir(parents=True, exist_ok=True)
        with archive.open("a", encoding="utf-8") as handle:
            for row in rows:
                if isinstance(row, dict):
                    handle.write(json.dumps(row, ensure_ascii=False, allow_nan=False, default=str) + "\n")

    def _load_outcome_archive(self) -> List[Dict[str, Any]]:
        archive = self._outcome_archive_path()
        if not archive.exists():
            return []
        rows: List[Dict[str, Any]] = []
        try:
            with archive.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict):
                        rows.append(row)
        except OSError as e:
            logger.warning(f"Pre-earnings outcome archive unreadable: {e}")
        return rows

    def _learning_outcomes(self) -> List[Dict[str, Any]]:
        """Archived plus live settled outcomes; live rows win on duplicate keys."""
        with self._feedback_guard():
            live = copy.deepcopy(list(self._feedback.get("outcomes", []) or []))
        merged: Dict[str, Dict[str, Any]] = {}
        for row in self._load_outcome_archive() + live:
            if isinstance(row, dict):
                key = str(row.get("prediction_key") or f"{row.get('symbol')}|{row.get('event_utc')}")
                merged[key] = row
        return list(merged.values())

    def _refresh_learning_model(self) -> bool:
        """Refit the shadow model when new outcomes have settled. Caller holds no
        trading authority: the model only produces frozen shadow predictions."""
        from core.pre_earnings_learning import fit_learning_model

        outcomes = self._learning_outcomes()
        with self._feedback_guard():
            current = self._feedback.get("learning_model")
            if isinstance(current, dict) and current.get("source_outcome_count") == len(outcomes):
                return False
        model = fit_learning_model(outcomes, trained_at=_iso(_utc_now()))
        if model is None:
            return False
        model["source_outcome_count"] = len(outcomes)
        with self._feedback_guard():
            self._feedback["learning_model"] = model
        self._learning_report_cache = None
        return True

    def get_learning_report(self, ttl_seconds: float = 600.0) -> Dict[str, Any]:
        """Walk-forward evidence for every Pre-Earnings claim (read-only, cached)."""
        from core.pre_earnings_learning import build_learning_report

        cached = getattr(self, "_learning_report_cache", None)
        if isinstance(cached, dict) and (time.time() - cached.get("ts", 0.0)) < ttl_seconds:
            return copy.deepcopy(cached["report"])
        outcomes = self._learning_outcomes()
        with self._feedback_guard():
            model = copy.deepcopy(self._feedback.get("learning_model"))
        report = build_learning_report(outcomes, now=_utc_now(), model=model)
        self._learning_report_cache = {"ts": time.time(), "report": report}
        return copy.deepcopy(report)

    def _direction_edge_summary(self) -> Optional[Dict[str, Any]]:
        """Cached verdict for the Top 10 header. Never computes on the hot path."""
        cached = getattr(self, "_learning_report_cache", None)
        report = cached.get("report") if isinstance(cached, dict) else None
        if not isinstance(report, dict):
            return None
        claims = {c.get("claim"): c for c in report.get("direction_claims") or [] if isinstance(c, dict)}
        top = claims.get("top10_direction") or {}
        heuristic = claims.get("heuristic_score") or {}
        return {
            "direction_edge_proven": bool(report.get("direction_edge_proven")),
            "top10_verdict": top.get("verdict"),
            "top10_events": top.get("events"),
            "top10_accuracy_pct": top.get("accuracy_pct"),
            "heuristic_verdict": heuristic.get("verdict"),
            "heuristic_accuracy_pct": heuristic.get("accuracy_pct"),
            "baseline_accuracy_pct": heuristic.get("baseline_accuracy_pct"),
            "headline": report.get("headline"),
            "generated_at_utc": report.get("generated_at_utc"),
        }

    def _build_group_calibration_model(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        outcomes = self._recent_outcomes(cfg)
        min_samples = max(8, _safe_int(cfg.get("feedback_min_samples"), 24))
        if not outcomes:
            return {"sample_size": 0, "global_factor": 1.0, "group_factors": {}, "group_samples": {}}

        exp_vals = []
        obs_vals = []
        group_sum: Dict[str, Dict[str, float]] = {}

        for row in outcomes:
            if row.get("gap_risk_available") is False:
                continue
            raw_score = _safe_float(row.get("predicted_gap_score_raw"), None)
            p = raw_score / 100.0 if raw_score is not None else _safe_float(row.get("predicted_prob_high_gap"), None)
            obs = 1.0 if bool(row.get("realized_high_gap")) else 0.0
            if p is None:
                p = _safe_int(row.get("predicted_gap_risk"), 0) / 100.0
            p = float(_clamp(p, 0.0, 1.0))
            exp_vals.append(p)
            obs_vals.append(obs)
            sector = str(row.get("sector") or "UNKNOWN").upper()
            cap = str(row.get("cap_bucket") or "UNKNOWN").upper()
            gkey = f"{sector}|{cap}"
            agg = group_sum.setdefault(gkey, {"n": 0.0, "exp": 0.0, "obs": 0.0})
            agg["n"] += 1.0
            agg["exp"] += p
            agg["obs"] += obs

            skey = f"{sector}|ANY"
            sagg = group_sum.setdefault(skey, {"n": 0.0, "exp": 0.0, "obs": 0.0})
            sagg["n"] += 1.0
            sagg["exp"] += p
            sagg["obs"] += obs

        avg_exp = sum(exp_vals) / max(1, len(exp_vals))
        avg_obs = sum(obs_vals) / max(1, len(obs_vals))
        global_factor = 1.0
        if avg_exp > 0.05:
            global_factor = float(_clamp(avg_obs / avg_exp, 0.80, 1.20))

        group_factors: Dict[str, float] = {}
        group_samples: Dict[str, int] = {}
        for key, agg in group_sum.items():
            n = int(agg.get("n", 0.0))
            if n < 4:
                continue
            exp_rate = float(agg.get("exp", 0.0)) / max(1.0, float(n))
            obs_rate = float(agg.get("obs", 0.0)) / max(1.0, float(n))
            if exp_rate <= 0.05:
                factor = 1.0
            else:
                raw_factor = obs_rate / exp_rate
                shrink = _clamp(float(n) / float(2 * min_samples), 0.0, 1.0)
                factor = 1.0 + ((raw_factor - 1.0) * shrink)
            group_factors[key] = float(_clamp(factor, 0.75, 1.25))
            group_samples[key] = n

        model = {
            "sample_size": len(outcomes),
            "global_factor": round(global_factor, 6),
            "group_factors": group_factors,
            "group_samples": group_samples,
            "as_of_utc": _iso(_utc_now()),
        }
        self._feedback["calibration_model"] = model
        return model

    @staticmethod
    def _resolve_calibration_factor(
        model: Optional[Dict[str, Any]],
        sector_key: str,
        cap_bucket: str,
    ) -> Tuple[float, int]:
        if not isinstance(model, dict):
            return 1.0, 0
        factors = model.get("group_factors") if isinstance(model.get("group_factors"), dict) else {}
        samples = model.get("group_samples") if isinstance(model.get("group_samples"), dict) else {}
        exact = f"{sector_key}|{cap_bucket}"
        sector_only = f"{sector_key}|ANY"
        if exact in factors:
            return float(_safe_float(factors.get(exact), 1.0) or 1.0), _safe_int(samples.get(exact), 0)
        if sector_only in factors:
            return float(_safe_float(factors.get(sector_only), 1.0) or 1.0), _safe_int(samples.get(sector_only), 0)
        global_factor = _safe_float(model.get("global_factor"), 1.0) or 1.0
        return float(global_factor), _safe_int(model.get("sample_size"), 0)

    def _compute_live_calibration_metrics(
        self,
        cfg: Dict[str, Any],
        calibration_model: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        outcomes = self._recent_outcomes(cfg)
        high_gap_threshold = _safe_float(cfg.get("high_gap_threshold_pct"), 5.0) or 5.0
        if not outcomes:
            return {
                "sample_size": 0,
                "high_gap_threshold_pct": float(high_gap_threshold),
                "brier": None,
                "mae": None,
                "avg_predicted_prob": None,
                "observed_high_gap_rate": None,
                "buckets": [],
                "directional_accuracy": None,
                "weights": dict(self._feedback.get("weights") or {}),
                "model": calibration_model if isinstance(calibration_model, dict) else {},
            }

        probs = []
        obs = []
        realized = []
        dir_hits = 0
        dir_total = 0
        buckets = {
            "0-39": {"count": 0, "obs_sum": 0.0, "prob_sum": 0.0, "move_sum": 0.0},
            "40-69": {"count": 0, "obs_sum": 0.0, "prob_sum": 0.0, "move_sum": 0.0},
            "70-100": {"count": 0, "obs_sum": 0.0, "prob_sum": 0.0, "move_sum": 0.0},
        }

        for row in outcomes:
            if row.get("gap_risk_available") is False:
                continue
            pred_score = _safe_int(row.get("predicted_gap_risk"), 0)
            p = _safe_float(row.get("predicted_prob_high_gap"), None)
            if p is None:
                p = pred_score / 100.0
            p = float(_clamp(p, 0.0, 1.0))
            y = 1.0 if bool(row.get("realized_high_gap")) else 0.0
            # Filter non-finite realized moves: already-corrupted stores may
            # contain NaN (audit B1). `nan or 0.0` evaluates to `nan` (NaN is
            # truthy), so the `or` fallback does NOT sanitize — we must guard
            # explicitly so aggregations never become NaN.
            _rm_raw = _safe_float(row.get("realized_gap_move_pct"), None)
            rm = _rm_raw if (_rm_raw is not None and math.isfinite(_rm_raw)) else 0.0
            probs.append(p)
            obs.append(y)
            realized.append(rm)

            if pred_score < 40:
                key = "0-39"
            elif pred_score < 70:
                key = "40-69"
            else:
                key = "70-100"
            agg = buckets[key]
            agg["count"] += 1
            agg["obs_sum"] += y
            agg["prob_sum"] += p
            agg["move_sum"] += rm

            pred_bias = _safe_int(row.get("predicted_direction_bias"), 0)
            signed = _safe_float(row.get("realized_signed_move_pct"), None)
            if signed is None or abs(pred_bias) < 30:
                continue
            pred_sign = 1 if pred_bias > 0 else -1
            real_sign = 1 if signed > 0 else (-1 if signed < 0 else 0)
            if real_sign == 0:
                continue
            dir_total += 1
            if pred_sign == real_sign:
                dir_hits += 1

        n = len(probs)
        brier = sum((p - y) ** 2 for p, y in zip(probs, obs)) / max(1, n)
        mae = sum(abs(p - y) for p, y in zip(probs, obs)) / max(1, n)
        out_buckets = []
        for label in ("0-39", "40-69", "70-100"):
            agg = buckets[label]
            cnt = int(agg["count"])
            out_buckets.append(
                {
                    "bucket": label,
                    "count": cnt,
                    "observed_high_gap_rate": round((agg["obs_sum"] / cnt), 6) if cnt else None,
                    "avg_predicted_prob": round((agg["prob_sum"] / cnt), 6) if cnt else None,
                    "avg_realized_move_pct": round((agg["move_sum"] / cnt), 6) if cnt else None,
                }
            )

        return {
            "sample_size": n,
            "high_gap_threshold_pct": float(high_gap_threshold),
            "brier": round(float(brier), 6),
            "mae": round(float(mae), 6),
            "avg_predicted_prob": round(sum(probs) / max(1, n), 6),
            "observed_high_gap_rate": round(sum(obs) / max(1, n), 6),
            "avg_realized_move_pct": round(sum(realized) / max(1, n), 6),
            "buckets": out_buckets,
            "directional_accuracy": round(dir_hits / dir_total, 6) if dir_total else None,
            "directional_sample_size": dir_total,
            "weights": dict(self._feedback.get("weights") or {}),
            "model": calibration_model if isinstance(calibration_model, dict) else {},
        }

    @staticmethod
    def _estimate_confidence_intervals(
        gap_risk: int,
        direction_bias: int,
        historical_samples: int,
        news_count: int,
        implied_available: bool,
        earnings_trend_available: bool,
        group_samples: int,
        global_samples: int,
    ) -> Dict[str, Any]:
        gap_width = 30.0
        if implied_available:
            gap_width -= 5.0
        if historical_samples >= 6:
            gap_width -= 6.0
        elif historical_samples >= 3:
            gap_width -= 3.0
        else:
            gap_width += 4.0
        if news_count >= 8:
            gap_width -= 4.0
        elif news_count <= 1:
            gap_width += 3.0
        if group_samples >= 20:
            gap_width -= 5.0
        elif group_samples <= 3:
            gap_width += 3.0
        if global_samples >= 80:
            gap_width -= 4.0
        elif global_samples <= 10:
            gap_width += 2.0
        gap_width = float(_clamp(gap_width, 8.0, 35.0))

        bias_width = 55.0
        if earnings_trend_available:
            bias_width -= 8.0
        else:
            bias_width += 2.0
        if news_count >= 6:
            bias_width -= 8.0
        elif news_count <= 1:
            bias_width += 6.0
        if group_samples >= 20:
            bias_width -= 5.0
        if global_samples >= 80:
            bias_width -= 6.0
        bias_width = float(_clamp(bias_width, 15.0, 65.0))

        gap_low = int(round(_clamp(gap_risk - (gap_width / 2.0), 0.0, 100.0)))
        gap_high = int(round(_clamp(gap_risk + (gap_width / 2.0), 0.0, 100.0)))
        bias_low = int(round(_clamp(direction_bias - (bias_width / 2.0), -100.0, 100.0)))
        bias_high = int(round(_clamp(direction_bias + (bias_width / 2.0), -100.0, 100.0)))

        # `level` used to be a hard-coded 0.68, which reads as "68% confidence
        # interval". Nothing here is estimated from a distribution: the width
        # is a rule-of-thumb that narrows as more inputs are available. Say so
        # instead of publishing a coverage probability nobody measured.
        return {
            "gap_risk": {
                "low": gap_low,
                "high": gap_high,
                "width": round(gap_width, 2),
                "level": None,
                "basis": "heuristic_band_not_statistical",
                "score": round(_clamp(1.0 - (gap_width / 40.0), 0.0, 1.0), 4),
            },
            "direction_bias": {
                "low": bias_low,
                "high": bias_high,
                "width": round(bias_width, 2),
                "level": None,
                "basis": "heuristic_band_not_statistical",
                "score": round(_clamp(1.0 - (bias_width / 80.0), 0.0, 1.0), 4),
            },
        }

    @staticmethod
    def _items_share_earnings_window(
        left: Dict[str, Any],
        right: Dict[str, Any],
    ) -> bool:
        if not isinstance(left, dict) or not isinstance(right, dict):
            return False
        left_symbol = str(left.get("symbol") or "").upper().strip()
        right_symbol = str(right.get("symbol") or "").upper().strip()
        if not left_symbol or left_symbol != right_symbol:
            return False
        left_dt = _parse_iso(left.get("earnings_time_utc"))
        right_dt = _parse_iso(right.get("earnings_time_utc"))
        if left_dt is None or right_dt is None:
            return left_dt is None and right_dt is None
        return abs((left_dt - right_dt).total_seconds()) <= 14 * 86400

    def _carry_forward_pre_earnings_decision(
        self,
        target: Dict[str, Any],
        source: Dict[str, Any],
    ) -> bool:
        """Keep the canonical decision across enrichment for the same event.

        Targeted coverage/news rescoring rebuilds the complete item but does not
        itself run the canonical decision engine.  Dropping this field during
        that atomic swap starves autonomous admission until another full cycle.
        """
        if not self._items_share_earnings_window(target, source):
            return False
        decision = source.get("pre_earnings_decision") if isinstance(source, dict) else None
        if not isinstance(decision, dict):
            return False
        target["pre_earnings_decision"] = copy.deepcopy(decision)
        return True

    def _record_concurrent_item_update_locked(
        self,
        symbol: str,
        item: Dict[str, Any],
        kind: str,
    ) -> int:
        revision = int(getattr(self, "_concurrent_item_revision", 0)) + 1
        self._concurrent_item_revision = revision
        updates = getattr(self, "_concurrent_item_updates", None)
        if not isinstance(updates, dict):
            updates = self._concurrent_item_updates = {}
        updates[str(symbol or "").upper().strip()] = {
            "revision": revision,
            "kind": str(kind or "targeted"),
            "item": copy.deepcopy(item),
            "recorded_at": _iso(_utc_now()),
        }
        return revision

    def _concurrent_rescore_is_richer_and_recent(
        self,
        cycle_item: Dict[str, Any],
        rescore_item: Dict[str, Any],
        entry: Dict[str, Any],
        now: datetime,
        grace_seconds: int,
    ) -> bool:
        """A shallow full-cycle rebuild must not clobber a deeper targeted rescore.

        Returns True only when the rescore item is strictly richer (higher
        coverage_score) than the cycle's rebuild, and was recorded within
        `grace_seconds` -- so a stale rescore cannot clobber a fresh cycle
        indefinitely. `_items_share_earnings_window` is checked by the caller,
        so this helper only scores richness + recency.
        """
        if not isinstance(rescore_item, dict) or not isinstance(cycle_item, dict):
            return False
        rescore_cov = _safe_int(rescore_item.get("coverage_score"), None)
        cycle_cov = _safe_int(cycle_item.get("coverage_score"), None)
        if rescore_cov is None or cycle_cov is None:
            return False
        if rescore_cov <= cycle_cov:
            return False
        recorded_at = _parse_iso(entry.get("recorded_at")) if isinstance(entry, dict) else None
        if recorded_at is None:
            return False
        try:
            age = (now.astimezone(timezone.utc) - recorded_at.astimezone(timezone.utc)).total_seconds()
        except Exception:
            return False
        return 0 <= age <= float(grace_seconds)

    def _merge_concurrent_item_updates_locked(
        self,
        items: List[Dict[str, Any]],
        since_revision: int,
        cfg: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Overlay atomic per-symbol work completed after a cycle started."""
        updates = getattr(self, "_concurrent_item_updates", {}) or {}
        if not updates:
            return items
        out = list(items or [])
        index = {
            str(item.get("symbol") or "").upper().strip(): idx
            for idx, item in enumerate(out)
            if isinstance(item, dict) and str(item.get("symbol") or "").strip()
        }
        grace_seconds = max(60, _safe_int(cfg.get("refresh_seconds"), 600))
        merge_now = _utc_now()
        for sym, entry in sorted(
            updates.items(),
            key=lambda pair: _safe_int((pair[1] or {}).get("revision"), 0),
        ):
            rev = _safe_int((entry or {}).get("revision"), 0)
            idx = index.get(sym)
            source = (entry or {}).get("item")
            if idx is None or not isinstance(source, dict):
                continue
            if not self._items_share_earnings_window(out[idx], source):
                continue
            if rev <= int(since_revision):
                # The cycle started after this rescore completed, so the
                # revision-gate would drop it. But a shallow full-cycle rebuild
                # (refresh_enrichment=False) must not erase a deeper targeted
                # rescore for the SAME earnings event: overlay the richer rescore
                # when it is strictly richer and still within one refresh window.
                if not self._concurrent_rescore_is_richer_and_recent(
                    out[idx], source, entry or {}, merge_now, grace_seconds
                ):
                    continue
            merged = copy.deepcopy(source)
            # If an early-pass AI call completed on the cycle copy while a
            # targeted data rescore completed concurrently, keep both pieces of
            # newer work instead of making the later atomic overlay erase AI.
            if (
                not (
                    isinstance(merged.get("ai_decision"), dict)
                    and merged["ai_decision"].get("used")
                )
                and isinstance(out[idx].get("ai_decision"), dict)
                and out[idx]["ai_decision"].get("used")
            ):
                merged["ai_decision"] = copy.deepcopy(out[idx]["ai_decision"])
                self._realign_reused_ai_event_to_item(merged["ai_decision"], merged)
                if isinstance(out[idx].get("ollama"), dict):
                    merged["ollama"] = copy.deepcopy(out[idx]["ollama"])
            # A richer targeted rescore is authoritative for item data, while
            # the cycle copy is authoritative for the canonical entry decision
            # computed after scoring. Preserve both for the same event.
            self._carry_forward_pre_earnings_decision(merged, out[idx])
            self._synchronize_direction_decision(merged, cfg)
            out[idx] = merged
        return out

    def _merge_concurrent_item_updates(
        self,
        items: List[Dict[str, Any]],
        since_revision: int,
        cfg: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        with self._lock:
            return self._merge_concurrent_item_updates_locked(
                items,
                since_revision,
                cfg,
            )

    def _early_ai_pass(
        self,
        cfg: Dict[str, Any],
        *,
        cycle_quota: Optional[Dict[str, int]] = None,
        concurrent_since_revision: Optional[int] = None,
    ) -> None:
        """Enrich AI verdicts on the PERSISTED items (from the last successful
        cycle) at the START of run_cycle, BEFORE the slow per-ticker rebuild.

        Why: run_cycle builds ~200 items with per-ticker yfinance fetches before
        reaching `_apply_ollama` at the end — that is many minutes, so without
        this early pass the user sees no `ai_decision` for 10+ min after a
        refresh. Enriching the already-persisted (real) items up-front lets the
        UI show AI verdicts promptly; the slow rebuild re-enriches fresh items
        afterward. The early and final passes share one quota, so this latency
        optimization cannot double the configured per-cycle AI call cap.

        Safety: deep-copies the persisted items, enriches the copies, then
        atomically swaps them back under `self._lock` and persists — it never
        mutates dicts a concurrent GET reader may be iterating. Non-fatal: any
        error is logged and swallowed so run_cycle is unaffected. No-op when
        items are already AI-enriched (avoids redundant LLM cost) or when
        `use_ollama` is disabled.
        """
        if not bool(cfg.get("use_ollama", True)):
            return
        try:
            if concurrent_since_revision is None:
                concurrent_since_revision = int(
                    getattr(self, "_concurrent_item_revision", 0)
                )
            src_items = list(self._state.get("items", []) or [])
            if not src_items:
                return
            # Skip the (cheap) LLM work entirely when the persisted items already
            # carry AI verdicts — e.g. a cycle that already completed its early pass.
            # A verdict that is STALE BY PEER EVIDENCE (peer outcomes settled after
            # it was generated) does NOT count as enriched: the early pass may
            # re-analyze it under the shared quota (owner re-analysis rule).
            if all(
                isinstance(it, dict)
                and isinstance(it.get("ai_decision"), dict)
                and it["ai_decision"].get("used")
                and not self._ai_verdict_stale_by_peer_evidence(it, it["ai_decision"])
                and (
                    not bool(cfg.get("news_change_reanalysis", True))
                    or not self._ai_verdict_stale_by_news_evidence(it, it["ai_decision"])
                )
                for it in src_items
            ):
                return
            items = copy.deepcopy(src_items)
            used = self._apply_ollama(items, cfg, cycle_quota=cycle_quota)
            if not used:
                return
            with self._lock:
                items = self._merge_concurrent_item_updates_locked(
                    items,
                    int(concurrent_since_revision),
                    cfg,
                )
                self._state["items"] = items
                status = dict(self._state.get("status", {}) or {})
                status["ai_early_pass_utc"] = _iso(_utc_now())
                self._state["status"] = status
                self._persist_locked()
            logger.info("Pre-Earnings early AI pass enriched persisted items.")
        except Exception:
            logger.exception("Pre-Earnings early AI pass failed (non-fatal)")

    def force_ai_for_symbol(self, symbol: str) -> Dict[str, Any]:
        """Force a fresh AI verdict for ONE ticker, on demand (UI 'Refresh AI').

        Bypasses the 'already enriched' skip — re-runs the LLM even when
        `ai_decision.used` is already true. Reuses `_apply_ollama` (cloud gate,
        model selection, ai_decision construction, bias blend) on a 1-item list,
        so the cap on `ollama_max_tickers` is irrelevant.

        Concurrency: deep-copies the target item OUTSIDE the lock, enriches the
        copy, then atomically swaps it back under `self._lock` + persists — the
        LLM call (3-7s) never blocks GET readers (same pattern as `_early_ai_pass`).

        Returns one of:
          {"status": "invalid_symbol"|"not_found"|"ai_unavailable"|"ai_failed", "symbol": sym}
          {"status": "ok", "symbol": sym, "ai_decision": {...}}
        """
        sym = str(symbol or "").upper().strip()
        if not sym:
            return {"status": "invalid_symbol", "symbol": sym}

        cfg = self._get_config()
        if not bool(cfg.get("use_ollama", True)):
            return {"status": "ai_unavailable", "symbol": sym}
        try:
            model = self._analyst_model()
        except Exception:
            model = None
        if not model:
            return {
                "status": "ai_unavailable",
                "symbol": sym,
                "reason": "no_active_model",
            }

        # Find + deep-copy the target item under the lock; release BEFORE the LLM call.
        with self._lock:
            items = list(self._state.get("items", []) or [])
            idx = next(
                (i for i, it in enumerate(items)
                 if isinstance(it, dict)
                 and str(it.get("symbol") or "").upper().strip() == sym),
                None,
            )
            if idx is None:
                return {"status": "not_found", "symbol": sym}
            item_copy = copy.deepcopy(items[idx])

        used = self._apply_ollama([item_copy], cfg, force=True)
        if (not used
                or not isinstance(item_copy.get("ai_decision"), dict)
                or not item_copy["ai_decision"].get("used")):
            return {
                "status": "ai_failed",
                "symbol": sym,
                "model": model,
                "provider_mode": "cloud" if ollama.is_cloud_host() else "local",
            }

        # Swap the enriched copy back into state under the lock + persist.
        with self._lock:
            items = list(self._state.get("items", []) or [])
            replaced = False
            for i, it in enumerate(items):
                if (isinstance(it, dict)
                        and str(it.get("symbol") or "").upper().strip() == sym):
                    if not self._items_share_earnings_window(it, item_copy):
                        return {"status": "stale_event", "symbol": sym}
                    latest = copy.deepcopy(it)
                    latest["ai_decision"] = copy.deepcopy(item_copy["ai_decision"])
                    latest["ai_decision"]["source"] = "forced"
                    self._realign_reused_ai_event_to_item(latest["ai_decision"], latest)
                    if isinstance(item_copy.get("ollama"), dict):
                        latest["ollama"] = copy.deepcopy(item_copy["ollama"])
                    self._synchronize_direction_decision(latest, cfg)
                    items[i] = latest
                    item_copy = latest
                    replaced = True
                    break
            if not replaced:
                return {"status": "not_found", "symbol": sym}
            self._state["items"] = items
            self._record_concurrent_item_update_locked(sym, item_copy, "forced_ai")
            self._persist_locked()
        logger.info("Pre-Earnings forced AI refresh for %s.", sym)
        return {"status": "ok", "symbol": sym, "ai_decision": item_copy["ai_decision"]}

    def _synchronize_direction_decision(
        self,
        item: Dict[str, Any],
        cfg: Dict[str, Any],
    ) -> None:
        """Make every derived direction field agree with one idempotent blend.

        Legacy snapshots stored the pre-LLM structured score in
        ``direction_bias`` but the blended result in ``direction_bias_score``.
        Preserve that structured base once, then always recompute the final score
        from it so repeated "Refresh AI" actions cannot blend a blend.
        """
        if not isinstance(item, dict):
            return
        ai_decision = item.get("ai_decision")
        ai_used = isinstance(ai_decision, dict) and bool(ai_decision.get("used"))
        ai_eligible = True
        if ai_used:
            ai_eligible = _ai_decision_qualifies_pre_earnings(ai_decision)

        structured = _safe_float(item.get("structured_direction_bias_score"), None)
        if structured is None and ai_used:
            legacy_norm = _safe_float(item.get("direction_bias"), None)
            if legacy_norm is not None and math.isfinite(float(legacy_norm)):
                structured = (
                    float(legacy_norm) * 100.0
                    if -1.0 <= float(legacy_norm) <= 1.0
                    else float(legacy_norm)
                )
        if structured is None:
            structured = _safe_float(item.get("direction_bias_score"), None)
        if structured is None or not math.isfinite(float(structured)):
            return
        structured = float(_clamp(structured, -100.0, 100.0))
        structured_score = int(round(structured))
        structured_label = direction_label_from_score(structured_score)
        # Audit P0-2: a news-only call (GapRisk unavailable + <2 non-news
        # components) keeps its clamped NEUTRAL structured view so the thin
        # data basis never displays as conviction. An explicit AI verdict may
        # still break the tie in the final blend (documented AI
        # participation), while the NEWS_ONLY_CALL badge stays on the row.
        news_only_call = bool(item.get("news_only_call"))
        if news_only_call:
            structured_label = "NEUTRAL"

        final_score = structured_score
        llm_score: Optional[int] = None
        alignment = "NO_LLM"
        if ai_used:
            verdict = str(ai_decision.get("verdict") or "NEUTRAL").strip().upper()
            if verdict not in {"LONG", "SHORT", "NEUTRAL"}:
                verdict = "NEUTRAL"
            sentiment = _safe_float(ai_decision.get("sentiment"), None)
            confidence = float(
                _clamp(_safe_float(ai_decision.get("confidence"), 0.0) or 0.0, 0.0, 1.0)
            )
            weight = float(
                _clamp(
                    _safe_float(
                        ai_decision.get("weight"),
                        cfg.get("ollama_decision_weight", 0.35),
                    )
                    or 0.0,
                    0.0,
                    0.6,
                )
            )
            if sentiment is not None and math.isfinite(float(sentiment)):
                sentiment = float(_clamp(sentiment, -1.0, 1.0))
                nudge = 10.0 * confidence if verdict == "LONG" else (
                    -10.0 * confidence if verdict == "SHORT" else 0.0
                )
                llm_component = float(_clamp((sentiment * 100.0) + nudge, -100.0, 100.0))
                llm_score = int(round(llm_component))
                final_score = int(
                    round(((1.0 - weight) * structured) + (weight * llm_component))
                )
                final_score = int(_clamp(final_score, -100, 100))
                item["ai_sentiment"] = round(sentiment, 3)
                ai_decision["direction_score"] = llm_score
                ai_decision["weight"] = round(weight, 4)

            if verdict in {"LONG", "SHORT"} and structured_label in {"LONG", "SHORT"}:
                alignment = "ALIGNED" if verdict == structured_label else "CONFLICT"
            elif verdict == "NEUTRAL" and structured_label == "NEUTRAL":
                alignment = "ALIGNED"
            else:
                alignment = "MIXED"

        # Audit P0-2: without an AI verdict the clamped structured view is also
        # the final view — the label must not re-inflate to LONG/SHORT.
        final_label = direction_label_from_score(final_score)
        if news_only_call and not ai_used:
            final_label = "NEUTRAL"
        if not ai_eligible:
            final_score = 0
            final_label = "NEUTRAL"
        item["structured_direction_bias_score"] = structured_score
        item["structured_direction_label"] = structured_label
        item["llm_direction_score"] = llm_score
        item["final_direction_bias_score"] = final_score
        item["direction_bias_score"] = final_score
        item["direction_bias"] = round(final_score / 100.0, 4)
        item["direction_label"] = final_label
        item["decision_alignment"] = alignment

        badges = [
            str(badge)
            for badge in (item.get("badges") or [])
            if str(badge) not in {"LONG_BIAS", "SHORT_BIAS", "AI_CONFLICT", "AI_INELIGIBLE"}
        ]
        # A clamped news-only call shows NEUTRAL; a magnitude badge next to it
        # would contradict the label (audit P0-2).
        _label_clamped = news_only_call and not ai_used
        if final_score >= 40 and not _label_clamped:
            badges.append("LONG_BIAS")
        elif final_score <= -40 and not _label_clamped:
            badges.append("SHORT_BIAS")
        if alignment == "CONFLICT":
            badges.append("AI_CONFLICT")
        if not ai_eligible:
            badges.append("AI_INELIGIBLE")
            flags = dict(item.get("flags") or {})
            flags["ai_ineligible"] = True
            item["flags"] = flags
        item["badges"] = list(dict.fromkeys(badges))

        gap_risk = _safe_int(item.get("gap_risk"), None)
        coverage_score = _safe_int(item.get("coverage_score"), None)
        if gap_risk is not None and coverage_score is not None:
            item["opportunity_score"] = self._compute_opportunity_score(
                gap_risk=gap_risk,
                direction_bias=final_score,
                coverage_score=coverage_score,
            )

        for container_key in ("confidence", "confidence_interval"):
            container = item.get(container_key)
            if not isinstance(container, dict):
                continue
            direction_ci = container.get("direction_bias")
            if not isinstance(direction_ci, dict):
                continue
            direction_ci = dict(direction_ci)
            width = _safe_float(direction_ci.get("width"), None)
            if width is None:
                low = _safe_float(direction_ci.get("low"), None)
                high = _safe_float(direction_ci.get("high"), None)
                if low is not None and high is not None:
                    width = max(0.0, float(high) - float(low))
            if width is not None:
                direction_ci["low"] = int(round(_clamp(final_score - (width / 2.0), -100.0, 100.0)))
                direction_ci["high"] = int(round(_clamp(final_score + (width / 2.0), -100.0, 100.0)))
                direction_ci["width"] = round(float(width), 2)
                container = dict(container)
                container["direction_bias"] = direction_ci
                item[container_key] = container

        reasons = [
            str(reason)
            for reason in (item.get("bias_reasons") or [])
            if not str(reason).startswith(
                (
                    "AI verdict ",
                    "Structured direction ",
                    "LLM direction ",
                    "Final blended direction ",
                )
            )
        ]
        summary_reasons = [
            f"Structured direction = {structured_label} {structured_score:+d}.",
        ]
        if ai_used and llm_score is not None:
            verdict = str(ai_decision.get("verdict") or "NEUTRAL").upper()
            confidence = int(round(float(_safe_float(ai_decision.get("confidence"), 0.0) or 0.0) * 100))
            weight_pct = int(round(float(_safe_float(ai_decision.get("weight"), 0.0) or 0.0) * 100))
            summary_reasons.append(
                f"LLM direction = {verdict} {llm_score:+d} "
                f"({confidence}% confidence, {weight_pct}% blend weight)."
            )
        summary_reasons.append(f"Final blended direction = {final_label} {final_score:+d}.")
        item["bias_reasons"] = (summary_reasons + reasons)[:7]

    def _ai_verdict_stale_by_peer_evidence(
        self,
        item: Dict[str, Any],
        cached_ai_entry: Optional[Dict[str, Any]],
    ) -> bool:
        """True when peer read-through evidence settled AFTER the saved AI
        verdict was generated.

        The owner's re-analysis rule ("if HUT and CIFR report below
        expectations, MARA/CLSK/RIOT must be RE-ANALYZED"): a verdict that
        predates the freshest settled peer evaluation is stale and the name
        re-enters the normal per-cycle enrichment pool. This helper only ever
        UNLOCKS re-analysis — callers keep serving the previous verdict when
        the LLM is unavailable, so staleness never deletes. Unprovable ordering
        (missing generated_at / peer timestamps) is honestly "not stale", so a
        legacy verdict without timestamps cannot burn LLM budget every cycle.
        """
        try:
            if not isinstance(item, dict) or not isinstance(cached_ai_entry, dict):
                return False
            readthrough = item.get("peer_readthrough")
            if not isinstance(readthrough, dict):
                return False
            latest_eval = _parse_iso(readthrough.get("latest_evaluation_utc"))
            if latest_eval is None:
                return False
            ai = cached_ai_entry.get("ai_decision")
            if not isinstance(ai, dict):
                # Tolerate being handed the ai_decision block itself.
                ai = cached_ai_entry if "used" in cached_ai_entry else None
            if not isinstance(ai, dict) or not ai.get("used"):
                return False
            generated = _parse_iso(ai.get("generated_at"))
            if generated is None:
                return False
            return latest_eval > generated
        except Exception:
            return False

    @classmethod
    def _ai_verdict_stale_by_news_evidence(
        cls,
        item: Dict[str, Any],
        cached_ai_entry: Optional[Dict[str, Any]],
    ) -> bool:
        """Unlock re-analysis only when a genuinely newer headline appeared."""
        try:
            if not isinstance(item, dict) or not isinstance(cached_ai_entry, dict):
                return False
            ai = cached_ai_entry.get("ai_decision")
            if not isinstance(ai, dict):
                ai = cached_ai_entry if "used" in cached_ai_entry else None
            if not isinstance(ai, dict) or not ai.get("used"):
                return False
            generated = _parse_iso(ai.get("generated_at"))
            if generated is None:
                return False

            digest = item.get("news_digest") if isinstance(item.get("news_digest"), dict) else {}
            current_fingerprint = str(digest.get("fingerprint") or "").strip()
            saved_fingerprint = str(ai.get("news_fingerprint") or "").strip()
            if saved_fingerprint and current_fingerprint == saved_fingerprint:
                return False

            saved_ids = {
                str(value or "").strip()
                for value in (ai.get("news_evidence_ids") or [])
                if str(value or "").strip()
            }
            for row in item.get("top_news") or []:
                if not isinstance(row, dict):
                    continue
                evidence_id = cls._news_evidence_id(row)
                if evidence_id in saved_ids:
                    continue
                published = _parse_iso(row.get("published_at") or row.get("published_utc"))
                # A small clock-skew guard avoids re-analysis when a provider
                # rewrites the same publish timestamp a few seconds later.
                if published is not None and published > (generated + timedelta(seconds=60)):
                    return True
            return False
        except Exception:
            return False

    def _carry_forward_ai_decision(
        self,
        item: Dict[str, Any],
        previous_item: Optional[Dict[str, Any]],
        cfg: Dict[str, Any],
    ) -> bool:
        """Carry a saved LLM verdict into a fresh structured rebuild.

        Page refreshes and normal scanner cycles must not silently spend another
        LLM call or erase a verdict the user already paid for. The verdict is
        reused only for the same earnings window (within 14 days), so a new
        quarterly event cannot inherit stale analysis from the prior quarter.
        """
        if not isinstance(item, dict):
            return False
        current_event = _parse_iso(item.get("earnings_time_utc"))
        source_item: Optional[Dict[str, Any]] = None
        source_label = "persisted"
        if isinstance(previous_item, dict):
            previous_ai = previous_item.get("ai_decision")
            previous_event = _parse_iso(
                (previous_ai or {}).get("event_utc")
                if isinstance(previous_ai, dict)
                else None
            ) or _parse_iso(previous_item.get("earnings_time_utc"))
            same_event = (
                current_event is None
                and previous_event is None
            ) or (
                current_event is not None
                and previous_event is not None
                and abs((current_event - previous_event).total_seconds()) <= 14 * 86400
            )
            if (
                isinstance(previous_ai, dict)
                and previous_ai.get("used")
                and same_event
            ):
                source_item = previous_item

        # A full-cycle snapshot can temporarily omit a symbol (ranking,
        # provider/calendar miss, or an older process completing late). The
        # event-keyed sidecar is deliberately independent of the visible top-N
        # rows, so a paid/user-requested analysis remains recoverable.
        if source_item is None:
            source_item = self._lookup_cached_ai_for_item(item)
            source_label = "persisted_cache"
        if not isinstance(source_item, dict):
            return False

        source_ai = source_item.get("ai_decision")
        if not isinstance(source_ai, dict) or not source_ai.get("used"):
            return False
        item["ai_decision"] = copy.deepcopy(source_ai)
        item["ai_decision"]["source"] = source_label
        if self._ai_verdict_stale_by_peer_evidence(item, item["ai_decision"]):
            # Peer outcomes settled AFTER this verdict was generated (owner
            # re-analysis rule). Mark the reuse honestly: `_apply_ollama`
            # re-enriches the name under the normal per-cycle quota, and only a
            # successful refresh replaces the verdict — when the LLM is down
            # the previous verdict keeps being served (never dropped to null).
            item["ai_decision"]["stale_by_peer_evidence"] = True
        if (
            bool(cfg.get("news_change_reanalysis", True))
            and self._ai_verdict_stale_by_news_evidence(item, item["ai_decision"])
        ):
            item["ai_decision"]["stale_by_news_evidence"] = True
        self._realign_reused_ai_event_to_item(item["ai_decision"], item)
        if isinstance(source_item.get("ollama"), dict):
            item["ollama"] = copy.deepcopy(source_item["ollama"])
        self._synchronize_direction_decision(item, cfg)
        return True

    @staticmethod
    def _realign_reused_ai_event_to_item(
        ai_decision: Dict[str, Any], item: Dict[str, Any]
    ) -> None:
        """Realign a reused LLM verdict's ``event_utc`` to the item's current
        ``earnings_time_utc``.

        A saved verdict is reused across small calendar date shifts within the
        same earnings window (<=14 days, see ``_carry_forward_ai_decision``).
        The verdict keeps the ``event_utc`` it was generated for, so when the
        calendar shifts the date (e.g. Aug 3 -> Aug 5) the attached
        ``ai_decision.event_utc`` disagrees with the item's
        ``earnings_time_utc``. That mismatch is invisible to the calendar
        mismatch detector and confusing in the detail view. Realign the verdict
        to the item's canonical event date and record the original in
        ``reused_from_event_utc`` for provenance. The caller has already verified
        the same-event (<=14d) relationship, so a differing event_utc here is a
        same-quarter date shift, not a new quarter; the window is still guarded
        defensively. Mutates ``ai_decision`` in place (callers pass a deepcopy).
        """
        if not isinstance(ai_decision, dict) or not ai_decision.get("used"):
            return
        if not isinstance(item, dict):
            return
        item_event = item.get("earnings_time_utc")
        if not item_event:
            return
        ai_event = ai_decision.get("event_utc")
        if ai_event == item_event:
            return
        ai_dt = _parse_iso(ai_event)
        item_dt = _parse_iso(item_event)
        if ai_dt is None or item_dt is None:
            return
        if abs((ai_dt - item_dt).total_seconds()) > 14 * 86400:
            return
        ai_decision["reused_from_event_utc"] = ai_event
        ai_decision["event_utc"] = item_event

    @staticmethod
    def _ai_cache_key(symbol: str, event_utc: Any) -> Optional[str]:
        sym = str(symbol or "").upper().strip()
        if not sym:
            return None
        event_dt = _parse_iso(event_utc)
        event_key = _iso(event_dt) if event_dt is not None else "NO_EVENT"
        return f"{sym}|{event_key}"

    def _load_ai_cache(self) -> Dict[str, Dict[str, Any]]:
        try:
            if not PRE_EARNINGS_AI_CACHE_PATH.exists():
                return {}
            with PRE_EARNINGS_AI_CACHE_PATH.open("r", encoding="utf-8") as f:
                payload = json.load(f)
            raw = payload.get("analyses", {}) if isinstance(payload, dict) else {}
            if not isinstance(raw, dict):
                return {}
            valid: Dict[str, Dict[str, Any]] = {}
            for key, entry in raw.items():
                if not isinstance(entry, dict):
                    continue
                ai_decision = entry.get("ai_decision")
                if not isinstance(ai_decision, dict) or not ai_decision.get("used"):
                    continue
                cache_key = self._ai_cache_key(
                    entry.get("symbol"),
                    entry.get("event_utc") or ai_decision.get("event_utc"),
                )
                if cache_key:
                    valid[cache_key] = copy.deepcopy(entry)
            return valid
        except Exception as exc:
            logger.warning("Pre-earnings AI cache load failed: %s", exc)
            return {}

    def _lookup_cached_ai_for_item(
        self,
        item: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(item, dict):
            return None
        symbol = str(item.get("symbol") or "").upper().strip()
        if not symbol:
            return None
        current_event = _parse_iso(item.get("earnings_time_utc"))
        lock = getattr(self, "_lock", None)
        if lock is None:
            entries = [
                copy.deepcopy(entry)
                for entry in (getattr(self, "_ai_cache", {}) or {}).values()
                if isinstance(entry, dict)
                and str(entry.get("symbol") or "").upper().strip() == symbol
            ]
        else:
            with lock:
                entries = [
                    copy.deepcopy(entry)
                    for entry in (getattr(self, "_ai_cache", {}) or {}).values()
                    if isinstance(entry, dict)
                    and str(entry.get("symbol") or "").upper().strip() == symbol
                ]

        matches: List[Dict[str, Any]] = []
        for entry in entries:
            ai_decision = entry.get("ai_decision")
            if not isinstance(ai_decision, dict) or not ai_decision.get("used"):
                continue
            cached_event = _parse_iso(
                entry.get("event_utc") or ai_decision.get("event_utc")
            )
            same_event = (
                current_event is None
                and cached_event is None
            ) or (
                current_event is not None
                and cached_event is not None
                and abs((current_event - cached_event).total_seconds()) <= 14 * 86400
            )
            if same_event:
                matches.append(entry)
        if not matches:
            return None
        matches.sort(
            key=lambda entry: (
                _parse_iso(
                    entry.get("saved_at")
                    or (entry.get("ai_decision") or {}).get("generated_at")
                )
                or datetime.min.replace(tzinfo=timezone.utc)
            ),
            reverse=True,
        )
        return matches[0]

    def _cache_ai_from_item_locked(self, item: Dict[str, Any]) -> bool:
        if not isinstance(item, dict):
            return False
        ai_decision = item.get("ai_decision")
        if not isinstance(ai_decision, dict) or not ai_decision.get("used"):
            return False
        symbol = str(item.get("symbol") or "").upper().strip()
        event_utc = ai_decision.get("event_utc") or item.get("earnings_time_utc")
        cache_key = self._ai_cache_key(symbol, event_utc)
        if not cache_key:
            return False
        event_dt = _parse_iso(event_utc)
        entry: Dict[str, Any] = {
            "symbol": symbol,
            "event_utc": _iso(event_dt) if event_dt is not None else None,
            "saved_at": (
                ai_decision.get("generated_at")
                or _iso(_utc_now())
            ),
            "ai_decision": copy.deepcopy(ai_decision),
        }
        if isinstance(item.get("ollama"), dict):
            entry["ollama"] = copy.deepcopy(item["ollama"])
        cache = getattr(self, "_ai_cache", None)
        if not isinstance(cache, dict):
            cache = self._ai_cache = {}
        changed = cache.get(cache_key) != entry
        cache[cache_key] = entry
        return changed

    def _persist_ai_cache_locked(self) -> None:
        cache = getattr(self, "_ai_cache", {}) or {}
        # Keep the journal bounded while retaining several years of quarterly
        # events. Newest entries win if a very large universe is analyzed.
        ordered = sorted(
            (
                (key, entry)
                for key, entry in cache.items()
                if isinstance(entry, dict)
                and isinstance(entry.get("ai_decision"), dict)
                and entry["ai_decision"].get("used")
            ),
            key=lambda pair: (
                _parse_iso(
                    pair[1].get("saved_at")
                    or (pair[1].get("ai_decision") or {}).get("generated_at")
                )
                or datetime.min.replace(tzinfo=timezone.utc)
            ),
            reverse=True,
        )[:1200]
        self._ai_cache = {key: copy.deepcopy(entry) for key, entry in ordered}
        PRE_EARNINGS_AI_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp_path = PRE_EARNINGS_AI_CACHE_PATH.with_suffix(".json.tmp")
        payload = {
            "version": 1,
            "updated_utc": _iso(_utc_now()),
            "analyses": self._ai_cache,
        }
        try:
            with temp_path.open("w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False, allow_nan=False)
            os.replace(str(temp_path), str(PRE_EARNINGS_AI_CACHE_PATH))
        except Exception as exc:
            logger.error("Pre-earnings AI cache persist failed: %s", exc)
            try:
                if temp_path.exists():
                    temp_path.unlink()
            except Exception:
                pass

    def _apply_ollama(
        self,
        items: List[Dict[str, Any]],
        cfg: Dict[str, Any],
        force: bool = False,
        *,
        cycle_quota: Optional[Dict[str, int]] = None,
    ) -> bool:
        if not bool(cfg.get("use_ollama", True)):
            return False
        if not items:
            return False

        # Recompute the final blend from each item's fresh structured score and
        # its persisted verdict before selecting new LLM work.
        for item in items:
            if (
                isinstance(item, dict)
                and isinstance(item.get("ai_decision"), dict)
                and item["ai_decision"].get("used")
            ):
                self._synchronize_direction_decision(item, cfg)

        max_tickers = max(1, int(cfg.get("ollama_max_tickers", 25)))
        timeout_sec = max(1, int(cfg.get("ollama_timeout_seconds", 12)))
        if force:
            # Manual owner requests may use a local model. Give them the global
            # interactive budget instead of the short background batch budget.
            try:
                timeout_sec = max(timeout_sec, min(120, int(ollama.get_timeout())))
            except Exception:
                pass

        # Refresh genuinely stale AI evidence before ordinary score-ranked work,
        # especially for an imminent report.  A pending refresh lowers the
        # trader score until AI has read the new headlines; ranking only by that
        # lowered score created a circular starvation bug where the ticker could
        # never re-enter the bounded AI batch.
        queue_now = _utc_now()

        def _ai_queue_priority(it: Dict[str, Any]) -> Tuple[Any, ...]:
            ai = it.get("ai_decision") if isinstance(it.get("ai_decision"), dict) else {}
            stale_news = bool(
                it.get("news_update_pending")
                or (
                    bool(cfg.get("news_change_reanalysis", True))
                    and bool(ai.get("used"))
                    and self._ai_verdict_stale_by_news_evidence(it, ai)
                )
            )
            event_delta = self._effective_event_delta_days(
                _parse_iso(it.get("earnings_time_utc")),
                self._normalize_session(it.get("session")),
                queue_now,
            )
            imminent = event_delta is not None and event_delta <= 1.0
            return (
                int(imminent),
                int(stale_news),
                _safe_int(it.get("trader_score"), -1),
                _safe_int(it.get("opportunity_score"), 0),
                _safe_int(it.get("coverage_score"), 0),
                _safe_int(it.get("confidence_score"), 0),
                abs(_safe_float(it.get("direction_bias_score"), 0.0) or 0.0),
                _safe_int(it.get("gap_risk"), 0),
            )

        ranked = sorted(
            items,
            key=_ai_queue_priority,
            reverse=True,
        )

        # Persistence + accumulating coverage (user requirement: AI analysis must be
        # SAVED and never silently re-run on restart/page-refresh — only re-run when the
        # USER requests it via force_ai_for_symbol). When not forced, drop items that
        # already carry a saved AI verdict BEFORE the cap slice, so the next N
        # not-yet-enriched tickers bubble up into the batch each cycle — coverage
        # accumulates to all tickers over successive cycles instead of re-doing the same
        # top-15 forever. force=True (Refresh AI button) bypasses the skip.
        if not force:
            ranked = [
                it for it in ranked
                if (
                    not (isinstance(it.get("ai_decision"), dict) and it["ai_decision"].get("used"))
                    # Peer read-through staleness (owner re-analysis rule): a
                    # saved verdict OLDER than the freshest settled peer
                    # outcome re-enters the enrichment pool, ranked by the same
                    # highest-GapRisk-first ordering and bounded by the same
                    # per-cycle quota — re-analysis is unlocked, never forced.
                    or self._ai_verdict_stale_by_peer_evidence(it, it.get("ai_decision"))
                    or (
                        bool(cfg.get("news_change_reanalysis", True))
                        and self._ai_verdict_stale_by_news_evidence(it, it.get("ai_decision"))
                    )
                )
                and not (
                    self._event_gate_blockers(it)
                    if (
                        "earnings_time_utc" in it
                        or "event_status" in it
                        or (isinstance(it.get("flags"), dict) and it["flags"].get("already_reported"))
                    )
                    else []
                )
            ]
        selected = ranked[:max_tickers]
        if not force and isinstance(cycle_quota, dict):
            remaining = max(0, _safe_int(cycle_quota.get("remaining"), 0))
            selected = selected[:remaining]
        if not selected:
            return False

        # AI sentiment is CLOUD-ONLY. The local model is too slow to be usable, so when
        # no OLLAMA_API_KEY is configured (host resolves to localhost) we skip the LLM
        # entirely and let the keyword sentiment stand — we never call the slow local model.
        # Automatic batches stay cloud-only to protect latency and resources.
        # An explicit owner Refresh AI (force=True) is allowed to use the
        # globally selected local Ollama model.
        if not force:
            try:
                if not ollama.is_cloud_host():
                    logger.info("Pre-Earnings background AI skipped: local model is manual-only.")
                    return False
            except Exception:
                return False

        try:
            model = self._analyst_model()
        except Exception:
            model = None
        if not model:
            return False
        timeout_sec = call_timeout_for(model, timeout_sec)

        used = False
        fail_count = 0
        for item in selected:
            stale_by_peer_evidence = (
                isinstance(item.get("ai_decision"), dict)
                and bool(item["ai_decision"].get("used"))
                and self._ai_verdict_stale_by_peer_evidence(item, item["ai_decision"])
            )
            stale_by_news_evidence = (
                bool(cfg.get("news_change_reanalysis", True))
                and isinstance(item.get("ai_decision"), dict)
                and bool(item["ai_decision"].get("used"))
                and self._ai_verdict_stale_by_news_evidence(item, item["ai_decision"])
            )
            # Defensive double-skip: even if selection filtered enriched items out, never
            # overwrite a saved verdict unless forced (persistence guarantee) — or the
            # verdict is stale by fresher peer evidence (owner re-analysis rule).
            if (
                not force
                and isinstance(item.get("ai_decision"), dict)
                and item["ai_decision"].get("used")
                and not stale_by_peer_evidence
                and not stale_by_news_evidence
            ):
                continue
            # Already-reported tickers (news says the earnings already happened) get
            # NO forward AI verdict in the normal cycle — no LLM budget, no LONG/SHORT
            # on a completed event. force=True (user Refresh-AI) still runs so the user
            # can get an on-demand confirmation; the honest prompt returns NEUTRAL.
            if (
                not force
                and (
                    "earnings_time_utc" in item
                    or "event_status" in item
                    or (isinstance(item.get("flags"), dict) and item["flags"].get("already_reported"))
                )
                and self._event_gate_blockers(item)
            ):
                continue
            if not force and isinstance(cycle_quota, dict):
                remaining = max(0, _safe_int(cycle_quota.get("remaining"), 0))
                if remaining <= 0:
                    break
                cycle_quota["remaining"] = remaining - 1
                cycle_quota["attempted"] = (
                    max(0, _safe_int(cycle_quota.get("attempted"), 0)) + 1
                )
            if force:
                enriched = self._ollama_enrich_item(
                    item,
                    model,
                    timeout_sec,
                    manual_request=True,
                )
            else:
                enriched = self._ollama_enrich_item(item, model, timeout_sec)
            if not isinstance(enriched, dict):
                fail_count += 1
                continue

            err = str(enriched.get("_error") or "").strip().upper()
            if err:
                fail_count += 1
                # ONLY the provider circuit breaker is a hard stop. Transient
                # TIMEOUT/ERROR/PARSE failures (e.g. a 404 while a cloud model
                # cold-starts) are tolerated — the next item is still attempted — so a
                # cold-start burst no longer cascade-excludes the rest of the eligible
                # batch. The old `fail_count >= 6` break left tickers like MASS/STRL/OBIO
                # without AI despite sitting inside the eligible top-N.
                if err == "CIRCUIT_OPEN":
                    logger.info("Pre-Earnings Ollama skipped: provider circuit is open.")
                    break
                continue

            fail_count = 0
            if isinstance(enriched.get("why_bullets"), list) and enriched.get("why_bullets"):
                item["why"] = [str(x).strip() for x in enriched.get("why_bullets", []) if str(x).strip()][:5]
            if isinstance(enriched.get("what_to_watch"), list) and enriched.get("what_to_watch"):
                item["what_to_watch"] = [str(x).strip() for x in enriched.get("what_to_watch", []) if str(x).strip()][:5]
            item["ollama"] = enriched

            # ── AI participation in the long/short DECISION and the SCORE ──
            # The LLM returns an explicit verdict (LONG/SHORT/NEUTRAL) plus a
            # scalar sentiment. Both are recorded on the item as a first-class
            # `ai_decision` block (surfaced in the UI), and the verdict+sentiment
            # are blended into `direction_bias_score` with a CONFIGURABLE weight
            # (cfg["ollama_decision_weight"], default 0.35) so the AI is a real
            # contributor but cannot, on headlines alone, override a data-driven
            # call. A confident AI verdict breaks a NEUTRAL tie.
            ai_sent = enriched.get("sentiment")
            verdict = str(enriched.get("decision") or "NEUTRAL").strip().upper()
            if verdict not in {"LONG", "SHORT", "NEUTRAL"}:
                verdict = "NEUTRAL"
            dec_conf = _clamp(_safe_float(enriched.get("decision_confidence"), 0.0) or 0.0, 0.0, 1.0)
            dec_rationale = str(enriched.get("decision_rationale") or "").strip()

            item["ai_decision"] = {
                "used": True,
                "verdict": verdict,
                "qualifies_pre_earnings": bool(enriched.get("qualifies_pre_earnings", True)),
                "confidence": round(float(dec_conf), 4),
                "sentiment": round(float(ai_sent), 4) if isinstance(ai_sent, (int, float)) else None,
                "rationale": dec_rationale[:280],
                "catalyst_label": str(enriched.get("catalyst_label") or "OTHER").strip().upper(),
                "model": str(enriched.get("answered_by") or "").strip() or model,
                "event_utc": item.get("earnings_time_utc"),
                "generated_at": _iso(_utc_now()),
                "source": "fresh",
                "news_fingerprint": (
                    (item.get("news_digest") or {}).get("fingerprint")
                    if isinstance(item.get("news_digest"), dict)
                    else None
                ),
                "news_evidence_ids": [
                    self._news_evidence_id(row)
                    for row in (item.get("top_news") or [])
                    if isinstance(row, dict)
                ],
                "news_articles_read": len(
                    [row for row in (item.get("top_news") or []) if isinstance(row, dict)]
                ),
                "news_sources_read": (
                    _safe_int((item.get("news_digest") or {}).get("unique_sources"), 0)
                    if isinstance(item.get("news_digest"), dict)
                    else 0
                ),
                "news_newest_utc": (
                    (item.get("news_digest") or {}).get("newest_utc")
                    if isinstance(item.get("news_digest"), dict)
                    else None
                ),
            }
            if str(enriched.get("answered_by") or "").strip():
                item["ai_decision"]["requested_model"] = model
                item["ai_decision"]["fallback_reason"] = str(enriched.get("fallback_reason") or "")[:200]
            if stale_by_news_evidence and not force:
                item["ai_decision"]["refresh_reason"] = "news_changed"
            elif stale_by_peer_evidence and not force:
                # Re-analysis provenance (owner rule): this verdict replaced one
                # that predated freshly settled peer outcomes.
                item["ai_decision"]["refresh_reason"] = "peer_readthrough"

            self._synchronize_direction_decision(item, cfg)
            used = True
        return used

    def _warm_ollama_model(self, model: str) -> None:
        """Warm the provider once. For a slow LOCAL model this loads it into memory
        (cold start ~50s); for cloud it's a no-op-fast probe. Uses the cloud-aware
        client so it follows ollama.com when an API key is configured."""
        try:
            ollama.generate("ok", model=model, timeout_override=90)
        except Exception:
            pass

    def _ollama_enrich_item(
        self,
        item: Dict[str, Any],
        model: str,
        timeout_sec: int,
        *,
        manual_request: bool = False,
    ) -> Optional[Dict[str, Any]]:
        symbol = str(item.get("symbol") or "").upper()
        try:
            live_cfg = self._get_config()
        except Exception:
            live_cfg = dict(self.DEFAULT_CONFIG)
        news_limit = max(4, min(20, _safe_int(live_cfg.get("news_evidence_limit"), 12)))
        prompt_char_budget = max(
            3000,
            min(16000, _safe_int(live_cfg.get("news_prompt_char_budget"), 9000)),
        )
        top_news = [
            row
            for row in list(item.get("top_news", []) or [])[:news_limit]
            if isinstance(row, dict) and str(row.get("title") or "").strip()
        ]
        news_lines: List[str] = []
        consumed_chars = 0
        for index, row in enumerate(top_news, start=1):
            title = re.sub(r"\s+", " ", str(row.get("title") or "")).strip()
            source = re.sub(r"\s+", " ", str(row.get("publisher") or row.get("source") or "unknown")).strip()
            published = str(row.get("published_utc") or row.get("published_at") or "unknown")
            summary = re.sub(r"\s+", " ", str(row.get("summary") or "")).strip()[:320]
            line = f"{index}. [{published}] [{source}] {title}"
            if summary and summary.lower() not in title.lower():
                line += f" — {summary}"
            remaining = prompt_char_budget - consumed_chars
            if remaining <= 0:
                break
            news_lines.append(line[:remaining])
            consumed_chars += len(news_lines[-1])

        # Verified structured context (real cached data, never fabricated) so the
        # verdict weighs evidence — not just headline tone. The LLM view is
        # blended into the final direction score, so it must see the same hard
        # inputs a human analyst would.
        gap_numeric = _safe_float(item.get("gap_risk_numeric"), None)
        gap_line = (
            f"{int(round(gap_numeric))}/100 ({str(item.get('gap_risk_level') or '').upper()})"
            if gap_numeric is not None
            else "N/A (insufficient history — do NOT read as low risk)"
        )
        hist_move = _safe_float(item.get("historical_median_move_pct"), None)
        hist_samples = _safe_int(item.get("historical_sample_size"), 0)
        hist_line = (
            f"{hist_move:.1f}% median absolute move over the last {hist_samples} reports"
            if hist_move is not None and hist_samples
            else "N/A"
        )
        beat_rate = _safe_float(item.get("beat_rate_historical"), None)
        beat_line = (
            f"{beat_rate * 100:.0f}% "
            f"({_safe_int(item.get('beat_rate_beats'), 0)}/{_safe_int(item.get('beat_rate_samples'), 0)} quarters beat estimates)"
            if beat_rate is not None
            else "N/A"
        )
        analyst_parts: List[str] = []
        upside = _safe_float(item.get("analyst_target_upside_pct"), None)
        if upside is not None:
            analyst_parts.append(f"price-target upside {upside:+.1f}%")
        rec_label = str(item.get("analyst_recommendation") or "").replace("_", " ").strip()
        if rec_label:
            analyst_parts.append(f"consensus {rec_label}")
        n_analysts = _safe_int(item.get("analyst_count"), 0)
        if n_analysts:
            analyst_parts.append(f"{n_analysts} analysts covering")
        analyst_line = ", ".join(analyst_parts) if analyst_parts else "N/A"
        # Retail positioning (owner request, 2026-09-05): eToro crowd skew as
        # CONTEXT ONLY — it already nudges direction component c6 at a small
        # weight; the prompt line is explicitly flagged crowd-contrarian-prone
        # so the model treats it as one input, not an instruction.
        retail_line = ""
        _etoro = item.get("etoro_context") if isinstance(item.get("etoro_context"), dict) else None
        if _etoro is not None and _etoro.get("buy_pct") is not None:
            _r_buy = _safe_float(_etoro.get("buy_pct"), None)
            _r_sell = _safe_float(_etoro.get("sell_pct"), None)
            if _r_buy is not None:
                if _r_sell is None:
                    _r_sell = 100.0 - _r_buy
                _r_rank = _safe_int(_etoro.get("popularity_rank"), None)
                _r_rank_txt = f", popularity #{_r_rank}" if _r_rank else ""
                retail_line = (
                    f"Retail positioning (eToro crowd, CONTEXT ONLY — crowds are often "
                    f"contrarian into earnings): {_r_buy:.0f}% buy / {_r_sell:.0f}% sell{_r_rank_txt}\n"
                )
        reaction_vals = [
            _safe_float(r.get("move_pct"), None)
            for r in list(item.get("recent_reactions") or [])[:3]
            if isinstance(r, dict)
        ]
        reaction_vals = [v for v in reaction_vals if v is not None]
        reactions_line = (
            ", ".join(f"{v:+.1f}%" for v in reaction_vals) + " (signed, newest first)"
            if reaction_vals
            else "N/A"
        )
        event_date = str(item.get("earnings_time_utc") or "")[:10] or "TBD"
        session = str(item.get("session") or "UNKNOWN").upper()
        sector = str(item.get("sector") or "").strip() or "N/A"
        # Peer read-through evidence (owner requirement, 2026-11): how this
        # name's settled theme/sector peers ACTUALLY moved after their own
        # reports in the last ~96h. Emitted only when real settled peer
        # outcomes exist — a name with no peer group or no fresh peer data gets
        # no line at all (honest), never a fake "neutral" one. Peers flagged
        # `early` are day-1 reactions (reported, not yet settled at +36h) —
        # fresher but not final; the prompt says so explicitly.
        peer_line = ""
        readthrough = item.get("peer_readthrough")
        if isinstance(readthrough, dict):
            rt_peers = [
                p for p in (readthrough.get("peers") or [])
                if isinstance(p, dict) and p.get("symbol")
            ]
            if rt_peers:
                def _rt_peer_txt(p: Dict[str, Any]) -> str:
                    txt = (
                        f"{str(p.get('symbol')).upper()} "
                        f"{_safe_float(p.get('signed_move_pct'), 0.0) or 0.0:+.1f}%"
                    )
                    if p.get("early"):
                        txt += " (day-1 reaction, not final)"
                    return txt
                moves_txt = ", ".join(_rt_peer_txt(p) for p in rt_peers[:5])
                rt_count = _safe_int(readthrough.get("count"), len(rt_peers))
                rt_pct = _safe_float(readthrough.get("pct_positive"), None)
                rt_pct_txt = (
                    f"{rt_pct * 100:.0f}% positive" if rt_pct is not None else "n/a positive"
                )
                rt_verdict = str(readthrough.get("verdict") or "mixed").strip()
                rt_hours = _safe_int(readthrough.get("window_hours"), 96)
                peer_line = (
                    f"Peer read-through (peer reports in last {rt_hours}h): "
                    f"{moves_txt} ({rt_count} peers, {rt_pct_txt}) — "
                    f"{rt_verdict} read-through; weigh this fresh evidence carefully for this name.\n"
                )
        structured_score = _safe_float(
            item.get("structured_direction_bias_score"),
            _safe_float(item.get("direction_bias_score"), None),
        )
        structured_line = (
            f"{structured_score:+.0f} (data-only, before LLM blend)"
            if structured_score is not None
            else "N/A"
        )

        system = (
            "You are a senior pre-earnings equity analyst. Judge the likely "
            "post-earnings reaction from the verified data and headlines "
            "provided. Weigh hard data (EPS beat history, analyst consensus, "
            "historical post-earnings moves) above headline tone. Peer "
            "read-through results — how this ticker's sector/theme peers "
            "actually moved after their own recent reports — are the freshest "
            "hard evidence: a strongly bearish or bullish read-through should "
            "shift your conviction unless this ticker's own data clearly "
            "diverges. Be factual, never invent numbers or events, and return "
            "STRICT JSON only."
        )
        prompt = (
            "Return STRICT JSON only.\n"
            "{"
            "\"why_bullets\":[\"...\"],"
            "\"what_to_watch\":[\"...\"],"
            "\"catalyst_label\":\"OFFERING|GUIDANCE|FDA|LAWSUIT|EARNINGS_VOLATILITY|M&A|OTHER\","
            "\"sentiment\":0.0,"
            "\"confidence\":0.0,"
            "\"qualifies_pre_earnings\":true,"
            "\"decision\":\"LONG|SHORT|NEUTRAL\","
            "\"decision_confidence\":0.0,"
            "\"decision_rationale\":\"one sentence\""
            "}\n"
            "sentiment = directional bias for this stock INTO its earnings, from -1.0 (very bearish) to +1.0 (very bullish), judged from the data + headlines.\n"
            "decision = your explicit long/short call for the post-earnings reaction: LONG (bullish), SHORT (bearish), or NEUTRAL (no edge).\n"
            "decision_confidence = 0.0..1.0 how confident you are in that call.\n"
            "decision_rationale = one concise sentence justifying the call from the verified data + headlines.\n"
            "qualifies_pre_earnings = false if results were already released, the event date is contradictory, or the evidence no longer describes an upcoming report.\n"
            "Constraints: max 5 bullets per list, factual, no promises. decision must agree with sign of sentiment.\n"
            "IMPORTANT: set qualifies_pre_earnings=false only when the evidence clearly says THIS upcoming "
            "event has already occurred (for example a current report release or current earnings-call transcript). "
            "Historical post-earnings reactions, older results, or prior-quarter guidance are evidence for the new "
            "event and must not be treated as proof that the current event already happened. When the current event "
            "has already occurred, "
            "the ticker no longer qualifies for a pre-earnings position — set decision=NEUTRAL, confidence low, "
            "sentiment 0.0, and decision_rationale noting the ticker already reported and no longer qualifies.\n"
            f"Symbol: {symbol}\n"
            f"Earnings event: {event_date} {session}\n"
            f"Sector: {sector}\n"
            + peer_line
            + f"GapRisk (0-100 heuristic setup score, NOT a probability): {gap_line}\n"
            f"Structured DirectionBias score (-100..+100): {structured_line}\n"
            f"Historical post-earnings move: {hist_line}\n"
            f"EPS beat rate: {beat_line}\n"
            f"Analyst consensus: {analyst_line}\n"
            + retail_line
            + f"Recent post-earnings reactions: {reactions_line}\n"
            f"News evidence reviewed: {len(news_lines)} articles from "
            f"{_safe_int((item.get('news_digest') or {}).get('unique_sources'), 0) if isinstance(item.get('news_digest'), dict) else 0} sources.\n"
            "Headlines and summaries (newest/material first; treat syndicated duplicates as one fact):\n"
            + ("\n".join(news_lines) if news_lines else "none")
            + "\n"
        )
        try:
            # Route through the cloud-aware client: uses ollama.com + the configured
            # model (e.g. gpt-oss:120b) when OLLAMA_API_KEY is set, else local.
            generate_kwargs = {
                "model": model,
                "system": system,
                "timeout_override": timeout_sec,
            }
            if manual_request:
                generate_kwargs["manual_request"] = True
            result = ollama.generate(prompt, **generate_kwargs)
            if not isinstance(result, dict):
                return {"_error": "ERROR"}
            if result.get("error"):
                err = str(result.get("error") or "").lower()
                return {"_error": "CIRCUIT_OPEN" if "circuit" in err else "ERROR"}
            raw = str(result.get("response") or "").strip()
            parsed = self._extract_json(raw)
            if not isinstance(parsed, dict):
                return {"_error": "PARSE"}
            why = parsed.get("why_bullets") if isinstance(parsed.get("why_bullets"), list) else []
            watch = parsed.get("what_to_watch") if isinstance(parsed.get("what_to_watch"), list) else []
            label = str(parsed.get("catalyst_label") or "OTHER").strip().upper()
            confidence = _safe_float(parsed.get("confidence"), 0.0) or 0.0
            confidence = _clamp(confidence, 0.0, 1.0)
            _sent_raw = parsed.get("sentiment")
            ai_sentiment = None
            if _sent_raw is not None:
                _s = _safe_float(_sent_raw, None)
                if _s is not None:
                    ai_sentiment = round(_clamp(_s, -1.0, 1.0), 4)

            # Explicit long/short verdict from the LLM (mandatory AI participation
            # in the decision, not just a scalar sentiment). Normalize so a
            # malformed/unsupported value degrades to NEUTRAL, never crashes.
            _verdict_raw = str(parsed.get("decision") or "").strip().upper()
            decision = _verdict_raw if _verdict_raw in {"LONG", "SHORT", "NEUTRAL"} else "NEUTRAL"
            decision_confidence = _clamp(_safe_float(parsed.get("decision_confidence"), 0.0) or 0.0, 0.0, 1.0)
            decision_rationale = str(parsed.get("decision_rationale") or "").strip()[:280]
            qualifies_raw = parsed.get("qualifies_pre_earnings")
            qualifies_pre_earnings = (
                qualifies_raw
                if isinstance(qualifies_raw, bool)
                else not bool(AI_INELIGIBLE_RE.search(decision_rationale))
            )
            if not qualifies_pre_earnings:
                decision = "NEUTRAL"
                ai_sentiment = 0.0
                decision_confidence = min(float(decision_confidence), 0.25)

            # When the Claude subscription cannot answer, the client continues on its fallback
            # model and says so. The saved view must then name the model that really wrote it.
            meta = result.get("_meta") if isinstance(result.get("_meta"), dict) else {}
            answered_by = str(meta.get("model") or "").strip() if meta.get("requested_model") else ""
            stand_in = (
                {"answered_by": answered_by,
                 "fallback_reason": str(meta.get("subscription_fallback_reason") or "").strip()[:200]}
                if answered_by and answered_by != str(model or "").strip() else {}
            )

            return {
                **stand_in,
                "why_bullets": [str(x).strip() for x in why if str(x).strip()][:5],
                "what_to_watch": [str(x).strip() for x in watch if str(x).strip()][:5],
                "catalyst_label": label if label else "OTHER",
                "confidence": round(confidence, 4),
                "sentiment": ai_sentiment,
                "qualifies_pre_earnings": bool(qualifies_pre_earnings),
                "decision": decision,
                "decision_confidence": round(float(decision_confidence), 4),
                "decision_rationale": decision_rationale,
            }
        except Exception as e:
            msg = str(e).lower()
            if "timed out" in msg or "timeout" in msg:
                return {"_error": "TIMEOUT"}
            return {"_error": "ERROR"}

    @staticmethod
    def _earnings_reported_status(earnings_time_utc: Any, session: Any, now_utc: datetime) -> bool:
        """True if the earnings release has already occurred by `now_utc`.

        Session-aware (mirrors PremarketScannerAgent._earnings_already_reported,
        premarket_scanner_agent.py:1273): calendar rows are date-only (midnight
        UTC), so a naive `dt < now` drops a same-day BMO minutes after midnight —
        hours before the actual report. Decide by DATE first, then session
        cutoff: BMO >= the actual 09:30 America/New_York market open, AMC >=
        the configured conservative after-hours cutoff. Unknown same-day session
        with a real time-of-day honors the exact time; date-only unknowns are
        kept (never silently drop an ambiguous same-day catalyst). Undated or
        malformed rows are never reported (missing-data flag, not stale).
        """
        dt = _parse_iso(earnings_time_utc)
        if dt is None:
            return False
        try:
            dt = dt.astimezone(timezone.utc)
            now_u = now_utc.astimezone(timezone.utc)
        except Exception:
            return False
        ev_date = dt.date()
        today = now_u.date()
        if ev_date > today:
            return False
        if ev_date < today:
            return True
        sess = str(session or "").strip().upper()
        if sess in {"BMO", "PRE-MARKET", "PREMARKET", "AMC", "AFTER_HOURS", "AFTERHOURS", "AFT"}:
            return earnings_session_reported(sess, now_u)
        if dt.hour or dt.minute:
            # Timed same-day event without a session hint: honor the exact time.
            return dt < now_u
        return False  # date-only, unknown session -> keep

    @staticmethod
    def _effective_event_delta_days(
        event_dt: Optional[datetime], session: str, now: datetime
    ) -> Optional[float]:
        """Proximity of an earnings event to `now`, session-aware.

        Calendar rows are date-only (midnight UTC), so a raw ``|event_dt - now|``
        makes a TODAY AMC ticker (reporting ~20:00 UTC) look ~18h in the past at
        18:00 UTC — scoring it BELOW a tomorrow reporter and getting it evicted
        by the preselect cap before it is ever built (PLTR was rank #639/3181).
        Mirror :meth:`_earnings_reported_status`: a same-day event NOT yet past
        its session cutoff is imminent (delta 0); a same-day event past its
        cutoff (already reported) is pushed far away (10d) so it does not waste
        a preselect slot — it is moved to ``reported_today`` downstream anyway.
        Future events keep their real (positive) distance; past events get 10d.
        """
        if event_dt is None:
            return None
        try:
            ev = event_dt.astimezone(timezone.utc)
            cur = now.astimezone(timezone.utc)
        except Exception:
            return None
        ev_date = ev.date()
        today = cur.date()
        if ev_date > today:
            return (ev - cur).total_seconds() / 86400.0
        if ev_date < today:
            return 10.0
        if PreEarningsAgent._earnings_reported_status(ev, session, cur):
            return 10.0
        return 0.0

    @staticmethod
    def _filter_reported_items(
        items: List[Dict[str, Any]],
        cfg: Dict[str, Any],
        now: datetime,
        watchlist: Optional[set] = None,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Partition items into (survivors, reported_today records).

        When `reported_session_filter` is enabled (default True), items are
        classified with `_earnings_reported_status` (session-aware) and the
        dropped side is emitted as transparency records instead of being
        silently discarded. When disabled, falls back to the legacy strict
        datetime comparison (`_filter_stale_items`) and emits no records
        (no way to distinguish "reported" from "never built" there).
        """
        if not bool((cfg or {}).get("reported_session_filter", True)):
            active = [
                item for item in items
                if isinstance(item, dict)
                and str(item.get("event_status") or "").upper() != "REPORTED"
                and not bool((item.get("flags") or {}).get("already_reported", False))
            ]
            return PreEarningsAgent._filter_stale_items(active, now), []
        wl = set(watchlist or set())
        survivors: List[Dict[str, Any]] = []
        reported: List[Dict[str, Any]] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            sym = str(it.get("symbol") or "").upper()
            item_flags = it.get("flags") if isinstance(it.get("flags"), dict) else {}
            evidence_reported = (
                str(it.get("event_status") or "").upper() == "REPORTED"
                or bool(item_flags.get("already_reported", False))
            )
            if evidence_reported or PreEarningsAgent._earnings_reported_status(
                it.get("earnings_time_utc"), it.get("session"), now
            ):
                reported.append({
                    "symbol": sym,
                    "earnings_time_utc": it.get("earnings_time_utc"),
                    "session": it.get("session"),
                    "gap_risk": it.get("gap_risk"),
                    "direction_label": it.get("direction_label"),
                    "is_watchlist": sym in wl,
                    "reason": "ALREADY_REPORTED_EVIDENCE" if evidence_reported else "SESSION_ELAPSED",
                    "evidence": [
                        str(value) for value in (item_flags.get("already_reported_evidence") or [])
                        if str(value).strip()
                    ][:5],
                })
            else:
                survivors.append(it)
        return survivors, reported

    @staticmethod
    def _filter_stale_items(items: List[Dict[str, Any]], now: datetime) -> List[Dict[str, Any]]:
        """Drop items whose earnings event has already occurred.

        Compares against the full event datetime (not just the date) so a BMO
        event reported at 12:00 UTC no longer lingers as "upcoming" later the
        same day (audit B4). Undated items are kept (they surface a missing-data
        flag, not a stale event). A confirmed event with actuals implies a past
        datetime and is dropped by the datetime comparison; a future
        date-confirmed event is kept (confirmed != reported).
        """
        out: List[Dict[str, Any]] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            dt = _parse_iso(it.get("earnings_time_utc"))
            if dt is None:
                # Undated → missing-data flag, keep (do not silently drop).
                out.append(it)
                continue
            if dt < now:
                # Already occurred → stale, drop.
                continue
            out.append(it)
        return out

    @staticmethod
    def _extract_json(raw: str) -> Optional[Dict[str, Any]]:
        text = str(raw or "").strip()
        if not text:
            return None
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text).strip()
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            candidate = text[start : end + 1]
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass
            cleaned = re.sub(r'("[^"]*")\s*\]\s*([,\}\]])', r'\1}\2', candidate)
            cleaned = re.sub(r',\s*([\]\}])', r'\1', cleaned)
            try:
                parsed = json.loads(cleaned)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass
        try:
            summary_m = re.search(r'"summary"\s*:\s*"([^"]+)"', text)
            sentiment_m = re.search(r'"sentiment"\s*:\s*"([^"]+)"', text)
            catalysts_m = re.search(r'"catalysts"\s*:\s*"([^"]+)"', text)
            risks_m = re.search(r'"risks"\s*:\s*"([^"]+)"', text)

            def _parse_sec_picks(sec_name: str) -> List[Dict[str, str]]:
                sec_m = re.search(r'"' + sec_name + r'"\s*:\s*\[(.*?)\]', text, re.DOTALL)
                if not sec_m:
                    return []
                sec_txt = sec_m.group(1)
                res = []
                for im in re.finditer(r'\{[^{}]*"symbol"\s*:\s*"([A-Z0-9._-]+)"[^{}]*"reason"\s*:\s*"([^"]+)"[^{}]*\}', sec_txt):
                    res.append({"symbol": im.group(1), "reason": im.group(2)})
                if not res:
                    for im in re.finditer(r'\{[^{}]*"reason"\s*:\s*"([^"]+)"[^{}]*"symbol"\s*:\s*"([A-Z0-9._-]+)"[^{}]*\}', sec_txt):
                        res.append({"symbol": im.group(2), "reason": im.group(1)})
                return res

            tl = _parse_sec_picks("top_long")
            ts = _parse_sec_picks("top_short")
            if summary_m or tl or ts:
                return {
                    "summary": summary_m.group(1) if summary_m else "",
                    "sentiment": sentiment_m.group(1).lower() if sentiment_m else "neutral",
                    "top_long": tl,
                    "top_short": ts,
                    "catalysts": catalysts_m.group(1) if catalysts_m else "",
                    "risks": risks_m.group(1) if risks_m else "",
                }
        except Exception:
            pass
        return None

    @staticmethod
    def _default_pulse_store() -> Dict[str, Any]:
        now_iso = _iso(_utc_now())
        return {
            "schema_version": 1,
            "created_at": now_iso,
            "updated_at": None,
            "limits": {
                "active_per_side": 500,
                "generated_picks_per_side": 3,
                "reason_characters": 500,
                "history_snapshots": "unbounded_unique",
                "archive_entries": "unbounded",
            },
            "next_snapshot_id": 1,
            "latest": None,
            "active": {
                "top_long": [],
                "top_short": [],
            },
            "archive": [],
            "snapshots": [],
        }

    def _ensure_pulse_store_runtime(self) -> None:
        """Support lightweight `__new__` unit fixtures without touching disk."""
        if not hasattr(self, "_pulse_store_lock"):
            self._pulse_store_lock = threading.RLock()
        if not hasattr(self, "_pulse_store"):
            self._pulse_store = self._default_pulse_store()

    @staticmethod
    def _normalize_pulse_store_timestamp(value: Any, fallback: str) -> str:
        parsed = _parse_iso(value)
        if parsed is not None:
            return str(_iso(parsed))
        fallback_parsed = _parse_iso(fallback)
        if fallback_parsed is not None:
            return str(_iso(fallback_parsed))
        return "1970-01-01T00:00:00+00:00"

    @classmethod
    def _normalize_loaded_snapshot_pick(
        cls,
        value: Dict[str, Any],
        source: str,
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(value, dict):
            return None
        symbol = str(value.get("symbol") or "").upper().strip()
        if not symbol:
            return None
        event_dt = _parse_iso(value.get("earnings_time_utc"))
        event_date = str(value.get("event_date") or "").strip() or None
        if event_date is None and event_dt is not None:
            event_date = event_dt.date().isoformat()
        event_key = str(value.get("event_key") or "").strip()
        if not event_key:
            event_key = f"{symbol}|{event_date or 'UNRESOLVED'}"
        normalized = copy.deepcopy(value)
        normalized.update({
            "symbol": symbol,
            "reason": str(value.get("reason") or "").strip()[:500],
            "source": str(value.get("source") or source).strip()[:64] or source,
            "eligible": bool(
                value.get(
                    "eligible",
                    event_date is not None and not event_key.endswith("|UNRESOLVED"),
                )
            ),
            "event_key": event_key,
            "event_date": event_date,
            "earnings_time_utc": _iso(event_dt) if event_dt is not None else None,
            "session": cls._normalize_session(value.get("session")),
        })
        return normalized

    def _normalize_loaded_pulse_snapshot(
        self,
        value: Dict[str, Any],
        *,
        snapshot_id: int,
        fallback_timestamp: str,
    ) -> Dict[str, Any]:
        source = str(value.get("source") or "legacy_state").strip()[:64] or "legacy_state"
        generated_at = self._normalize_pulse_store_timestamp(
            value.get("generated_at"),
            fallback_timestamp,
        )
        first_generated_at = self._normalize_pulse_store_timestamp(
            value.get("first_generated_at"),
            generated_at,
        )
        last_generated_at = self._normalize_pulse_store_timestamp(
            value.get("last_generated_at"),
            generated_at,
        )
        first_generated_at = min(
            (first_generated_at, last_generated_at),
            key=self._pulse_time_sort_value,
        )
        last_generated_at = max(
            (generated_at, last_generated_at),
            key=self._pulse_time_sort_value,
        )
        sources: List[str] = []
        for candidate in list(value.get("sources") or []) + [source]:
            normalized_source = str(candidate or "").strip()[:64]
            if normalized_source and normalized_source not in sources:
                sources.append(normalized_source)

        def normalize_picks(side: str) -> List[Dict[str, Any]]:
            normalized_picks: List[Dict[str, Any]] = []
            for pick in list(value.get(side) or [])[:3]:
                normalized = self._normalize_loaded_snapshot_pick(pick, source)
                if normalized is not None:
                    normalized_picks.append(normalized)
            return normalized_picks

        snapshot = {
            "snapshot_id": snapshot_id,
            "generated_at": last_generated_at,
            "first_generated_at": first_generated_at,
            "last_generated_at": last_generated_at,
            "recorded_at": self._normalize_pulse_store_timestamp(
                value.get("recorded_at"),
                generated_at,
            ),
            "source": source,
            "sources": sources,
            "occurrences": max(1, _safe_int(value.get("occurrences"), 1)),
            "summary": str(value.get("summary") or "").strip(),
            "sentiment": str(value.get("sentiment") or "").strip().lower(),
            "top_long": normalize_picks("top_long"),
            "top_short": normalize_picks("top_short"),
            "catalysts": str(value.get("catalysts") or "").strip(),
            "risks": str(value.get("risks") or "").strip(),
            "model": str(value.get("model") or "").strip(),
            "input_fingerprint": str(value.get("input_fingerprint") or "").strip() or None,
            "active_counts_after_merge": {
                "top_long": max(
                    0,
                    _safe_int(
                        (value.get("active_counts_after_merge") or {}).get("top_long"),
                        len(value.get("top_long") or []),
                    ),
                ),
                "top_short": max(
                    0,
                    _safe_int(
                        (value.get("active_counts_after_merge") or {}).get("top_short"),
                        len(value.get("top_short") or []),
                    ),
                ),
            },
        }
        snapshot["content_fingerprint"] = self._pulse_snapshot_fingerprint(snapshot)
        return snapshot

    def _normalize_loaded_pulse_latest(
        self,
        value: Any,
        snapshots: List[Dict[str, Any]],
        fallback_timestamp: str,
    ) -> Optional[Dict[str, Any]]:
        raw = copy.deepcopy(value) if isinstance(value, dict) else {}
        matching: Optional[Dict[str, Any]] = None
        raw_id = _safe_int(raw.get("snapshot_id"), 0)
        if raw_id:
            matching = next(
                (
                    snapshot
                    for snapshot in snapshots
                    if _safe_int(snapshot.get("snapshot_id"), 0) == raw_id
                ),
                None,
            )
        if matching is None and snapshots:
            matching = max(
                snapshots,
                key=lambda snapshot: (
                    self._pulse_time_sort_value(snapshot.get("generated_at")),
                    _safe_int(snapshot.get("snapshot_id"), 0),
                ),
            )
        if not raw and matching is None:
            return None
        base = matching or {}
        source = str(raw.get("source") or base.get("source") or "legacy_state").strip()[:64]
        generated_at = self._normalize_pulse_store_timestamp(
            raw.get("generated_at") or base.get("generated_at"),
            fallback_timestamp,
        )
        sources: List[str] = []
        for candidate in (
            list(raw.get("sources") or [])
            + list(base.get("sources") or [])
            + [source]
        ):
            normalized_source = str(candidate or "").strip()[:64]
            if normalized_source and normalized_source not in sources:
                sources.append(normalized_source)
        return {
            "snapshot_id": max(
                1,
                _safe_int(raw.get("snapshot_id") or base.get("snapshot_id"), 1),
            ),
            "generated_at": generated_at,
            "first_generated_at": self._normalize_pulse_store_timestamp(
                raw.get("first_generated_at") or base.get("first_generated_at"),
                generated_at,
            ),
            "last_generated_at": self._normalize_pulse_store_timestamp(
                raw.get("last_generated_at") or base.get("last_generated_at"),
                generated_at,
            ),
            "source": source or "legacy_state",
            "sources": sources or ["legacy_state"],
            "occurrences": max(
                1,
                _safe_int(raw.get("occurrences") or base.get("occurrences"), 1),
            ),
            "content_fingerprint": str(
                base.get("content_fingerprint")
                or raw.get("content_fingerprint")
                or self._pulse_snapshot_fingerprint({**base, **raw})
            ),
            "summary": str(raw.get("summary") or base.get("summary") or "").strip(),
            "sentiment": str(
                raw.get("sentiment") or base.get("sentiment") or ""
            ).strip().lower(),
            "catalysts": str(
                raw.get("catalysts") or base.get("catalysts") or ""
            ).strip(),
            "risks": str(raw.get("risks") or base.get("risks") or "").strip(),
            "model": str(raw.get("model") or base.get("model") or "").strip(),
        }

    def _load_pulse_store(self) -> Dict[str, Any]:
        """Load the durable Pulse registry without contacting any provider."""
        template = self._default_pulse_store()
        path = Path(getattr(self, "_pulse_store_path", PRE_EARNINGS_PULSE_HISTORY_PATH))
        try:
            if not path.exists():
                self._pulse_store_needs_persist = False
                return template
            with path.open("r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if not isinstance(loaded, dict):
                raise ValueError("Pulse history root must be an object")
            store = copy.deepcopy(template)
            store.update(loaded)
            fallback_timestamp = self._normalize_pulse_store_timestamp(
                loaded.get("created_at") or loaded.get("updated_at"),
                str(template["created_at"]),
            )
            store["created_at"] = fallback_timestamp
            store["updated_at"] = (
                self._normalize_pulse_store_timestamp(
                    loaded.get("updated_at"),
                    fallback_timestamp,
                )
                if loaded.get("updated_at")
                else None
            )
            loaded_limits = (
                loaded.get("limits")
                if isinstance(loaded.get("limits"), dict)
                else {}
            )
            store["limits"] = {
                "active_per_side": max(
                    10,
                    min(
                        2000,
                        _safe_int(loaded_limits.get("active_per_side"), 500),
                    ),
                ),
                "generated_picks_per_side": 3,
                "reason_characters": 500,
                "history_snapshots": "unbounded_unique",
                "archive_entries": "unbounded",
            }
            active = loaded.get("active") if isinstance(loaded.get("active"), dict) else {}
            store["active"] = {
                "top_long": [
                    copy.deepcopy(value)
                    for value in (active.get("top_long") or [])
                    if isinstance(value, dict) and str(value.get("symbol") or "").strip()
                ],
                "top_short": [
                    copy.deepcopy(value)
                    for value in (active.get("top_short") or [])
                    if isinstance(value, dict) and str(value.get("symbol") or "").strip()
                ],
            }
            store["archive"] = [
                copy.deepcopy(value)
                for value in (loaded.get("archive") or [])
                if isinstance(value, dict)
            ]
            normalized_snapshots: List[Dict[str, Any]] = []
            used_ids: set = set()
            next_available_id = 1
            for raw_snapshot in loaded.get("snapshots") or []:
                if not isinstance(raw_snapshot, dict):
                    continue
                requested_id = max(
                    1,
                    _safe_int(raw_snapshot.get("snapshot_id"), next_available_id),
                )
                while requested_id in used_ids:
                    requested_id += 1
                used_ids.add(requested_id)
                next_available_id = max(next_available_id, requested_id + 1)
                normalized_snapshots.append(
                    self._normalize_loaded_pulse_snapshot(
                        raw_snapshot,
                        snapshot_id=requested_id,
                        fallback_timestamp=fallback_timestamp,
                    )
                )
            store["snapshots"] = normalized_snapshots
            store["latest"] = self._normalize_loaded_pulse_latest(
                loaded.get("latest"),
                normalized_snapshots,
                fallback_timestamp,
            )
            store["next_snapshot_id"] = max(
                (max(used_ids) + 1) if used_ids else 1,
                _safe_int(loaded.get("next_snapshot_id"), 1),
            )
            store["schema_version"] = 1
            self._pulse_store_needs_persist = store != loaded
            self._pulse_store_file_mtime = path.stat().st_mtime
            return store
        except Exception as exc:
            logger.warning("Pre-earnings Pulse history load failed: %s", exc)
            self._pulse_store_needs_persist = False
            return template

    def _maybe_reload_pulse_store_locked(self) -> None:
        """Pick up Pulse sidecar merges written by other processes.

        The store is loaded once per process, but several processes (API
        server, engine subprocess, manual recovery) legitimately merge
        snapshots into the same sidecar. Without this check a serving
        process keeps its stale in-memory `latest`/`active` view forever
        and renders the live Pulse card empty even though the on-disk
        registry holds valid picks. Every mutation persists immediately,
        so the file is authoritative and safe to reload here.
        """
        path = getattr(self, "_pulse_store_path", None)
        if not path:
            return
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return
        if mtime <= float(getattr(self, "_pulse_store_file_mtime", 0.0)):
            return
        try:
            self._pulse_store = self._load_pulse_store()
        except Exception as exc:
            logger.warning("Pre-earnings Pulse history reload failed: %s", exc)
            return
        logger.info(
            "Pre-earnings Pulse sidecar changed on disk; reloaded registry "
            "(snapshots=%s, active_long=%s, active_short=%s)",
            len(self._pulse_store.get("snapshots") or []),
            len((self._pulse_store.get("active") or {}).get("top_long") or []),
            len((self._pulse_store.get("active") or {}).get("top_short") or []),
        )

    def _migrate_legacy_pulse_if_needed(self) -> None:
        """Seed the durable registry once from the canonical legacy snapshot."""
        legacy = self._state.get("pulse") if isinstance(self._state, dict) else None
        if not isinstance(legacy, dict):
            return
        with self._pulse_store_lock:
            snapshots = list(self._pulse_store.get("snapshots") or [])
            active = self._pulse_store.get("active") or {}
            if snapshots or list(active.get("top_long") or []) or list(active.get("top_short") or []):
                return
            self._merge_pulse_snapshot_locked(
                legacy,
                list(self._state.get("items") or []),
                self._get_config(),
                source="legacy_state",
                persist=True,
            )

    def _restore_persisted_pulse(self) -> None:
        """Expose the last durable Pulse immediately after a process restart."""
        previous_main_pulse = copy.deepcopy(
            self._state.get("pulse") if isinstance(self._state, dict) else None
        )
        with self._pulse_store_lock:
            active_changed = self._reconcile_pulse_active_locked(
                list(self._state.get("items") or []),
                self._get_config(),
            )
            if active_changed:
                self._pulse_store_needs_persist = True
                if hasattr(self, "_pulse_store_path"):
                    # News evidence is a confirmed event-state transition, not
                    # a schema-only migration. Persist it during restore so a
                    # cache-only API read cannot resurrect the recommendation.
                    self._persist_pulse_store_locked()
            pulse = self._pulse_from_store_locked(self._get_config())
        if not isinstance(pulse, dict):
            return
        main_pulse_changed = previous_main_pulse != pulse
        self._state["pulse"] = pulse
        flags = dict(self._state.get("flags") or {})
        flags["pulse_used"] = True
        self._state["flags"] = flags
        if active_changed or main_pulse_changed:
            # GET /api/pre-earnings is cache-first and may read the canonical
            # JSON before the agent loop runs. Repair any main/sidecar
            # divergence now, but leave byte-identical state untouched.
            with self._lock:
                self._persist_locked()

    def _persist_pulse_store_locked(self) -> None:
        """Atomically replace the Pulse sidecar while `_pulse_store_lock` is held."""
        path = Path(getattr(self, "_pulse_store_path", PRE_EARNINGS_PULSE_HISTORY_PATH))
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(
            f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        self._pulse_store["updated_at"] = _iso(_utc_now())
        try:
            with temp_path.open("w", encoding="utf-8") as handle:
                json.dump(self._pulse_store, handle, indent=2, ensure_ascii=False, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            for attempt in range(5):
                try:
                    os.replace(str(temp_path), str(path))
                    self._pulse_store_needs_persist = False
                    try:
                        self._pulse_store_file_mtime = path.stat().st_mtime
                    except OSError:
                        pass
                    break
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.05)
        except Exception as exc:
            logger.error("Pre-earnings Pulse history persist failed: %s", exc)
        finally:
            try:
                if temp_path.exists():
                    temp_path.unlink()
            except Exception:
                pass

    @staticmethod
    def _pulse_item_already_reported_news(item: Any) -> bool:
        if not isinstance(item, dict):
            return False
        flags = item.get("flags")
        return bool(
            isinstance(flags, dict)
            and flags.get("already_reported")
        )

    @staticmethod
    def _pulse_event_descriptor(
        item: Dict[str, Any],
        *,
        include_already_reported: bool = False,
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(item, dict):
            return None
        if (
            not include_already_reported
            and PreEarningsAgent._pulse_item_already_reported_news(item)
        ):
            return None
        if item.get("recommendation_eligible") is False:
            return None
        if (
            "recommendation_eligible" in item
            and str(item.get("direction_label") or "").upper() not in {"LONG", "SHORT"}
        ):
            return None
        symbol = str(item.get("symbol") or "").upper().strip()
        if not symbol or not _is_earnings_eligible_symbol(symbol):
            return None
        event_utc = str(
            item.get("earnings_time_utc")
            or item.get("earnings_date")
            or item.get("event_date")
            or ""
        ).strip()
        event_dt = _parse_iso(event_utc)
        event_date = event_dt.date().isoformat() if event_dt is not None else None
        session = PreEarningsAgent._normalize_session(item.get("session"))
        return {
            "symbol": symbol,
            # Event DATE is the stable identity. Calendar providers frequently
            # refine midnight rows into BMO/AMC timestamps during enrichment.
            "event_key": f"{symbol}|{event_date or 'UNDATED'}",
            "event_date": event_date,
            "earnings_time_utc": _iso(event_dt) if event_dt is not None else None,
            "session": session,
        }

    @classmethod
    def _pulse_descriptors_by_symbol(
        cls,
        items: List[Dict[str, Any]],
        now_utc: Optional[datetime] = None,
    ) -> Dict[str, Dict[str, Any]]:
        now = now_utc or _utc_now()
        descriptors: Dict[str, Dict[str, Any]] = {}
        for item in items or []:
            descriptor = cls._pulse_event_descriptor(item)
            if descriptor is None:
                continue
            if cls._earnings_reported_status(
                descriptor.get("earnings_time_utc"),
                descriptor.get("session"),
                now,
            ):
                continue
            symbol = descriptor["symbol"]
            current = descriptors.get(symbol)
            if current is None:
                descriptors[symbol] = descriptor
                continue
            current_dt = _parse_iso(current.get("earnings_time_utc"))
            candidate_dt = _parse_iso(descriptor.get("earnings_time_utc"))
            if candidate_dt is not None and (
                current_dt is None or candidate_dt < current_dt
            ):
                descriptors[symbol] = descriptor
        return descriptors

    @staticmethod
    def _pulse_time_sort_value(value: Any) -> float:
        parsed = _parse_iso(value)
        if parsed is None:
            return 0.0
        try:
            return parsed.timestamp()
        except Exception:
            return 0.0

    @staticmethod
    def _pulse_pick_copy_for_output(value: Dict[str, Any]) -> Dict[str, Any]:
        allowed = (
            "symbol",
            "reason",
            "event_key",
            "event_date",
            "earnings_time_utc",
            "session",
            "first_seen_at",
            "last_seen_at",
            "source",
            "sources",
            "occurrences",
        )
        return {
            key: copy.deepcopy(value.get(key))
            for key in allowed
            if value.get(key) is not None
        }

    def _evaluate_matured_pulse_picks_locked(self, cfg: Dict[str, Any]) -> bool:
        """Score archived Pulse picks against the realized post-earnings move.

        Mirrors `_evaluate_matured_feedback_predictions`: a pick is scored once
        its `event_dt + 36h` has passed (the next-day OHLC bar is available).
        `top_long` hits when `realized_signed_move_pct > 0`; `top_short` hits
        when `< 0`. Unresolved picks (no OHLC yet) are retried until
        `event_dt + 21d`, then marked `unresolved`. Only picks archived because
        their event actually reported (`event_reported` / `already_reported_news`)
        are scored — `active_limit` / `undated_event_expired` archives have no
        realized outcome. Reuses `_compute_event_gap_move_pct` (4h OHLC, cached
        1h) so no burst of yfinance calls beyond the per-item loop.
        """
        changed = False
        now = _utc_now()
        high_gap_threshold = _safe_float(cfg.get("high_gap_threshold_pct"), 5.0) or 5.0
        archive = self._pulse_store.setdefault("archive", [])
        for pick in archive:
            if not isinstance(pick, dict):
                continue
            if pick.get("realized_signed_move_pct") is not None or pick.get("unresolved"):
                continue
            # 2026-09-03 audit P2 (survivorship bias): only picks that survived
            # until the event were scored, so most archived picks were
            # permanently unscored and the displayed hit-rate overstated
            # quality. Score every archived pick whose event matured —
            # including direction_contradiction and event_replaced, which
            # carry a side and a real event date.
            if pick.get("archive_reason") not in (
                "event_reported", "already_reported_news",
                "direction_contradiction", "event_replaced",
            ):
                continue
            event_dt = _parse_iso(pick.get("earnings_time_utc"))
            if event_dt is None:
                continue
            if now < event_dt + timedelta(hours=36):
                continue
            symbol = str(pick.get("symbol") or "").upper().strip()
            session = self._normalize_session(pick.get("session"))
            signed = self._compute_event_gap_move_pct(symbol, event_dt, session)
            # Defense in depth: a non-finite (NaN/Inf) move is treated as
            # unresolved — never persisted (would emit a `NaN` literal).
            if signed is not None and not math.isfinite(float(signed)):
                signed = None
            if signed is None:
                # Price cache may lag; stop retrying after 21d to keep it bounded.
                if now > (event_dt + timedelta(days=21)):
                    pick["unresolved"] = True
                    pick["evaluation_utc"] = _iso(now)
                    changed = True
                continue
            side = pick.get("side")  # "top_long" | "top_short" (set at archive)
            realized = abs(float(signed))
            pick["realized_signed_move_pct"] = round(float(signed), 6)
            pick["realized_gap_move_pct"] = round(realized, 6)
            pick["realized_high_gap"] = bool(realized >= high_gap_threshold)
            pick["hit"] = bool(
                (side == "top_long" and signed > 0)
                or (side == "top_short" and signed < 0)
            )
            pick["evaluation_utc"] = _iso(now)
            changed = True
        # Bound the archive (keep newest 4000 by archived_at), mirroring the
        # per-item 4000-outcome cap.
        if len(archive) > 4000:
            archive.sort(
                key=lambda p: _parse_iso(p.get("archived_at"))
                or datetime.min.replace(tzinfo=timezone.utc)
            )
            self._pulse_store["archive"] = archive[-4000:]
            changed = True
        return changed

    def _compute_pulse_track_record_metrics(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        """Pulse pick directional accuracy over matured archived picks.

        Mirrors `_compute_track_record_metrics` but keyed on `side`
        (top_long / top_short) instead of verdict. Only picks with a finite,
        non-zero `realized_signed_move_pct` count as callable outcomes.
        60s in-process cache.
        """
        now = _utc_now()
        cached = getattr(self, "_pulse_track_record_cache", None)
        if (
            isinstance(cached, dict)
            and (now - cached.get("ts", datetime.min.replace(tzinfo=timezone.utc))).total_seconds() < 60
        ):
            return cached.get("data") or {}
        archive = list(self._pulse_store.get("archive", []) or [])
        overall = {"total": 0, "hits": 0, "move_sum": 0.0}
        per_side: Dict[str, Dict[str, Any]] = {
            "top_long": {"n": 0, "hits": 0, "move_sum": 0.0},
            "top_short": {"n": 0, "hits": 0, "move_sum": 0.0},
        }
        for p in archive:
            if not isinstance(p, dict):
                continue
            s = _safe_float(p.get("realized_signed_move_pct"), None)
            if s is None or not math.isfinite(s) or s == 0.0:
                continue
            side = p.get("side")
            if side not in per_side:
                continue
            hit = bool(p.get("hit"))
            per_side[side]["n"] += 1
            per_side[side]["move_sum"] += abs(s)
            if hit:
                per_side[side]["hits"] += 1
            overall["total"] += 1
            overall["move_sum"] += abs(s)
            if hit:
                overall["hits"] += 1

        def _rate(n: int, h: int) -> Optional[float]:
            return round(100.0 * h / n, 1) if n else None

        def _avg(n: int, ms: float) -> Optional[float]:
            return round(ms / n, 2) if n else None

        data = {
            "overall": {
                "total": overall["total"],
                "hits": overall["hits"],
                "hit_rate": _rate(overall["total"], overall["hits"]),
                "avg_realized_move": _avg(overall["total"], overall["move_sum"]),
            },
            "per_side": {
                k: {
                    "n": v["n"],
                    "hits": v["hits"],
                    "hit_rate": _rate(v["n"], v["hits"]),
                    "avg_realized_move": _avg(v["n"], v["move_sum"]),
                }
                for k, v in per_side.items()
            },
        }
        self._pulse_track_record_cache = {"ts": now, "data": data}
        return data

    def _archive_pulse_pick_locked(
        self,
        pick: Dict[str, Any],
        side: str,
        reason: str,
        now_iso: str,
    ) -> None:
        archived = copy.deepcopy(pick)
        archived["side"] = side
        archived["archived_at"] = now_iso
        archived["archive_reason"] = str(reason or "event_no_longer_active")
        self._pulse_store.setdefault("archive", []).append(archived)

    def _reconcile_pulse_active_locked(
        self,
        items: List[Dict[str, Any]],
        cfg: Dict[str, Any],
    ) -> bool:
        """Archive reported/replaced events; omission alone never deletes a pick."""
        changed = False
        now = _utc_now()
        now_iso = _iso(now)
        descriptors = self._pulse_descriptors_by_symbol(items, now)
        # Audit P0-1: an active pick whose current row label has flipped to the
        # opposing side (e.g. fresh data turned ACMR LONG +43 while an older
        # snapshot picked it short) must leave the active union — the card can
        # never display a side that contradicts the item row. Round 2: the
        # same removal applies when the row is NEUTRAL but its STRUCTURED
        # score clearly opposes the side (|structured| >= 15).
        labels = self._pulse_labels_by_symbol(items)
        structured_scores = self._pulse_structured_scores_by_symbol(items)
        current_eligibility = {
            str(item.get("symbol") or "").upper().strip(): bool(item.get("recommendation_eligible"))
            for item in (items or [])
            if isinstance(item, dict)
            and str(item.get("symbol") or "").strip()
            and "recommendation_eligible" in item
        }
        already_reported_news: Dict[str, List[Dict[str, Any]]] = {}
        for item in items or []:
            if not self._pulse_item_already_reported_news(item):
                continue
            descriptor = self._pulse_event_descriptor(
                item,
                include_already_reported=True,
            )
            if descriptor is not None:
                already_reported_news.setdefault(
                    str(descriptor.get("symbol") or ""),
                    [],
                ).append(descriptor)
        active = self._pulse_store.setdefault(
            "active",
            {"top_long": [], "top_short": []},
        )
        reconciled: Dict[str, List[Dict[str, Any]]] = {
            "top_long": [],
            "top_short": [],
        }
        seen_event_keys: Dict[str, Tuple[str, int]] = {}
        undated_retention_seconds = (
            max(7, _safe_int(cfg.get("pulse_undated_retention_days"), 45))
            * 86400
        )

        for side in ("top_long", "top_short"):
            for raw_pick in list(active.get(side) or []):
                if not isinstance(raw_pick, dict):
                    changed = True
                    continue
                pick = copy.deepcopy(raw_pick)
                symbol = str(pick.get("symbol") or "").upper().strip()
                if not symbol:
                    changed = True
                    continue
                pick["symbol"] = symbol
                current = descriptors.get(symbol)
                old_event_date = str(pick.get("event_date") or "").strip() or None
                old_event_utc = pick.get("earnings_time_utc")
                old_event_key = str(
                    pick.get("event_key")
                    or f"{symbol}|{old_event_date or 'UNDATED'}"
                )

                archive_reason: Optional[str] = None
                news_reported_same_event = any(
                    (
                        str(reported.get("event_key") or "") == old_event_key
                        or (
                            old_event_date is not None
                            and reported.get("event_date") == old_event_date
                        )
                        or old_event_date is None
                    )
                    for reported in already_reported_news.get(symbol, [])
                )
                if news_reported_same_event:
                    archive_reason = "already_reported_news"
                elif symbol in current_eligibility and not current_eligibility[symbol]:
                    archive_reason = "recommendation_ineligible"
                elif self._pulse_pick_contradicts_label(pick, side, labels, structured_scores):
                    archive_reason = "direction_contradiction"
                elif self._earnings_reported_status(
                    old_event_utc,
                    pick.get("session"),
                    now,
                ):
                    archive_reason = "event_reported"
                elif current is not None:
                    current_date = current.get("event_date")
                    if old_event_date and current_date and old_event_date != current_date:
                        archive_reason = "event_replaced"
                    else:
                        # Refine an undated/schedule-only pick in place without
                        # treating a BMO/AMC timestamp refinement as a new event.
                        for key in (
                            "event_key",
                            "event_date",
                            "earnings_time_utc",
                            "session",
                        ):
                            if current.get(key) != pick.get(key):
                                pick[key] = current.get(key)
                                changed = True
                elif not old_event_date:
                    first_seen = self._pulse_time_sort_value(pick.get("first_seen_at"))
                    if first_seen and (now.timestamp() - first_seen) > undated_retention_seconds:
                        archive_reason = "undated_event_expired"

                if archive_reason:
                    self._archive_pulse_pick_locked(
                        pick,
                        side,
                        archive_reason,
                        now_iso,
                    )
                    changed = True
                    continue

                event_key = str(pick.get("event_key") or old_event_key)
                pick["event_key"] = event_key
                if event_key in seen_event_keys:
                    prior_side, prior_index = seen_event_keys[event_key]
                    prior = reconciled[prior_side][prior_index]
                    prior_seen = self._pulse_time_sort_value(prior.get("last_seen_at"))
                    pick_seen = self._pulse_time_sort_value(pick.get("last_seen_at"))
                    if pick_seen > prior_seen:
                        reconciled[prior_side][prior_index] = pick
                    changed = True
                    continue
                seen_event_keys[event_key] = (side, len(reconciled[side]))
                reconciled[side].append(pick)

        limit = max(10, _safe_int(cfg.get("pulse_active_max_per_side"), 500))
        for side in ("top_long", "top_short"):
            overflow = reconciled[side][limit:]
            if overflow:
                for pick in overflow:
                    self._archive_pulse_pick_locked(
                        pick,
                        side,
                        "active_limit",
                        now_iso,
                    )
                reconciled[side] = reconciled[side][:limit]
                changed = True
            if reconciled[side] != list(active.get(side) or []):
                changed = True
        self._pulse_store["active"] = reconciled
        return changed

    @staticmethod
    def _pulse_snapshot_pick(
        pick: Dict[str, str],
        descriptor: Optional[Dict[str, Any]],
        source: str,
    ) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "symbol": str(pick.get("symbol") or "").upper().strip(),
            "reason": str(pick.get("reason") or "").strip()[:500],
            "source": source,
            "eligible": descriptor is not None,
        }
        if descriptor is not None:
            out.update(copy.deepcopy(descriptor))
        else:
            out.update({
                "event_key": f"{out['symbol']}|UNRESOLVED",
                "event_date": None,
                "earnings_time_utc": None,
                "session": "UNKNOWN",
            })
        return out

    @staticmethod
    def _pulse_snapshot_fingerprint(snapshot: Dict[str, Any]) -> str:
        """Hash material analyst content, excluding timestamps/provider metadata."""
        def picks(side: str) -> List[Dict[str, str]]:
            normalized: List[Dict[str, str]] = []
            for value in snapshot.get(side) or []:
                if not isinstance(value, dict):
                    continue
                symbol = str(value.get("symbol") or "").upper().strip()
                if not symbol:
                    continue
                normalized.append({
                    "symbol": symbol,
                    "reason": str(value.get("reason") or "").strip(),
                    # Identical prose for a later quarterly event is still a
                    # distinct historical analyst observation.
                    "event_key": str(value.get("event_key") or "").strip(),
                })
            return normalized

        material = {
            "summary": str(snapshot.get("summary") or "").strip(),
            "sentiment": str(snapshot.get("sentiment") or "").strip().lower(),
            "top_long": picks("top_long"),
            "top_short": picks("top_short"),
            "catalysts": str(snapshot.get("catalysts") or "").strip(),
            "risks": str(snapshot.get("risks") or "").strip(),
        }
        encoded = json.dumps(
            material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _upsert_active_pulse_pick_locked(
        self,
        incoming: Dict[str, Any],
        side: str,
        generated_at: str,
        source: str,
    ) -> None:
        event_key = str(incoming.get("event_key") or "")
        if not event_key:
            return
        active = self._pulse_store.setdefault(
            "active",
            {"top_long": [], "top_short": []},
        )
        found: Optional[Tuple[str, int, Dict[str, Any]]] = None
        for candidate_side in ("top_long", "top_short"):
            for index, candidate in enumerate(list(active.get(candidate_side) or [])):
                if str((candidate or {}).get("event_key") or "") == event_key:
                    found = (candidate_side, index, copy.deepcopy(candidate))
                    break
            if found is not None:
                break

        incoming_seen = self._pulse_time_sort_value(generated_at)
        target_side = side
        target_index: Optional[int] = None
        if found is None:
            merged = copy.deepcopy(incoming)
            merged.update({
                "first_seen_at": generated_at,
                "last_seen_at": generated_at,
                "source": source,
                "sources": [source],
                "occurrences": 1,
            })
        else:
            found_side, found_index, existing = found
            active[found_side].pop(found_index)
            existing_seen = self._pulse_time_sort_value(existing.get("last_seen_at"))
            incoming_is_newer = incoming_seen >= existing_seen
            merged = existing
            first_values = [
                value
                for value in (
                    existing.get("first_seen_at"),
                    generated_at,
                )
                if self._pulse_time_sort_value(value) > 0
            ]
            if first_values:
                merged["first_seen_at"] = min(
                    first_values,
                    key=self._pulse_time_sort_value,
                )
            last_values = [
                value
                for value in (
                    existing.get("last_seen_at"),
                    generated_at,
                )
                if self._pulse_time_sort_value(value) > 0
            ]
            if last_values:
                merged["last_seen_at"] = max(
                    last_values,
                    key=self._pulse_time_sort_value,
                )
            merged["occurrences"] = max(
                1,
                _safe_int(existing.get("occurrences"), 1),
            ) + 1
            sources = [
                str(value)
                for value in (existing.get("sources") or [])
                if str(value).strip()
            ]
            if source not in sources:
                sources.append(source)
            merged["sources"] = sources
            if incoming_is_newer:
                for key in (
                    "symbol",
                    "event_key",
                    "event_date",
                    "earnings_time_utc",
                    "session",
                ):
                    merged[key] = copy.deepcopy(incoming.get(key))
                if str(incoming.get("reason") or "").strip() or not merged.get("reason"):
                    merged["reason"] = str(incoming.get("reason") or "").strip()[:500]
                merged["source"] = source
                target_side = side
            else:
                target_side = found_side
            if target_side == found_side:
                target_index = found_index

        bucket = active.setdefault(target_side, [])
        if target_index is None or target_index >= len(bucket):
            bucket.append(merged)
        else:
            bucket.insert(target_index, merged)

    def _merge_pulse_snapshot_locked(
        self,
        pulse: Dict[str, Any],
        items: List[Dict[str, Any]],
        cfg: Dict[str, Any],
        *,
        source: str,
        persist: bool,
    ) -> Optional[Dict[str, Any]]:
        self._maybe_reload_pulse_store_locked()
        if not isinstance(pulse, dict):
            return self._pulse_from_store_locked(cfg)
        source_name = str(source or "unknown").strip()[:64] or "unknown"
        generated_at = str(pulse.get("generated_at") or "").strip()
        if _parse_iso(generated_at) is None:
            generated_at = _iso(_utc_now())
        descriptors = self._pulse_descriptors_by_symbol(items)
        normalized_long = self._normalize_pulse_picks(pulse.get("top_long"))
        normalized_short = self._normalize_pulse_picks(pulse.get("top_short"))
        # Same P0-1 guard as generation time, applied to every merge source
        # (LLM snapshot, manual import, recovery): a pick opposing the current
        # row's final label never enters the snapshot or the active union.
        # Round 2: the guard also drops a pick whose NEUTRAL row's structured
        # score clearly opposes the side (|structured| >= 15).
        labels = self._pulse_labels_by_symbol(items)
        structured_scores = self._pulse_structured_scores_by_symbol(items)
        normalized_long = [
            pick
            for pick in normalized_long
            if not self._pulse_pick_contradicts_label(pick, "top_long", labels, structured_scores)
        ]
        normalized_short = [
            pick
            for pick in normalized_short
            if not self._pulse_pick_contradicts_label(pick, "top_short", labels, structured_scores)
        ]
        snapshot_long = [
            self._pulse_snapshot_pick(
                pick,
                descriptors.get(str(pick.get("symbol") or "").upper()),
                source_name,
            )
            for pick in normalized_long
        ]
        snapshot_short = [
            self._pulse_snapshot_pick(
                pick,
                descriptors.get(str(pick.get("symbol") or "").upper()),
                source_name,
            )
            for pick in normalized_short
        ]

        self._reconcile_pulse_active_locked(items, cfg)
        for pick in snapshot_long:
            if pick.get("eligible"):
                self._upsert_active_pulse_pick_locked(
                    pick,
                    "top_long",
                    generated_at,
                    source_name,
                )
        for pick in snapshot_short:
            if pick.get("eligible"):
                self._upsert_active_pulse_pick_locked(
                    pick,
                    "top_short",
                    generated_at,
                    source_name,
                )
        self._reconcile_pulse_active_locked(items, cfg)

        snapshot_number = max(
            1,
            _safe_int(self._pulse_store.get("next_snapshot_id"), 1),
        )
        snapshot = {
            "snapshot_id": snapshot_number,
            "generated_at": generated_at,
            "first_generated_at": generated_at,
            "last_generated_at": generated_at,
            "recorded_at": _iso(_utc_now()),
            "source": source_name,
            "sources": [source_name],
            "occurrences": 1,
            "summary": str(pulse.get("summary") or "").strip(),
            "sentiment": str(pulse.get("sentiment") or "").strip().lower(),
            "top_long": snapshot_long,
            "top_short": snapshot_short,
            "catalysts": str(pulse.get("catalysts") or "").strip(),
            "risks": str(pulse.get("risks") or "").strip(),
            "model": str(pulse.get("model") or "").strip(),
            "input_fingerprint": str(pulse.get("input_fingerprint") or "").strip() or None,
        }
        active = self._pulse_store.get("active") or {}
        snapshot["active_counts_after_merge"] = {
            "top_long": len(active.get("top_long") or []),
            "top_short": len(active.get("top_short") or []),
        }
        snapshot["content_fingerprint"] = self._pulse_snapshot_fingerprint(snapshot)
        existing_snapshot: Optional[Dict[str, Any]] = None
        for candidate in self._pulse_store.setdefault("snapshots", []):
            if not isinstance(candidate, dict):
                continue
            fingerprint = str(candidate.get("content_fingerprint") or "")
            if not fingerprint:
                fingerprint = self._pulse_snapshot_fingerprint(candidate)
                candidate["content_fingerprint"] = fingerprint
            if fingerprint == snapshot["content_fingerprint"]:
                existing_snapshot = candidate
                break

        if existing_snapshot is None:
            self._pulse_store["snapshots"].append(snapshot)
            self._pulse_store["next_snapshot_id"] = snapshot_number + 1
            material_snapshot = snapshot
        else:
            previous_last_seen = self._pulse_time_sort_value(
                existing_snapshot.get("last_generated_at")
                or existing_snapshot.get("generated_at")
            )
            incoming_is_newer = (
                self._pulse_time_sort_value(generated_at) >= previous_last_seen
            )
            existing_snapshot["occurrences"] = max(
                1,
                _safe_int(existing_snapshot.get("occurrences"), 1),
            ) + 1
            first_values = [
                value
                for value in (
                    existing_snapshot.get("first_generated_at")
                    or existing_snapshot.get("generated_at"),
                    generated_at,
                )
                if self._pulse_time_sort_value(value) > 0
            ]
            last_values = [
                value
                for value in (
                    existing_snapshot.get("last_generated_at")
                    or existing_snapshot.get("generated_at"),
                    generated_at,
                )
                if self._pulse_time_sort_value(value) > 0
            ]
            if first_values:
                existing_snapshot["first_generated_at"] = min(
                    first_values,
                    key=self._pulse_time_sort_value,
                )
            if last_values:
                last_generated_at = max(
                    last_values,
                    key=self._pulse_time_sort_value,
                )
                existing_snapshot["last_generated_at"] = last_generated_at
                existing_snapshot["generated_at"] = last_generated_at
            sources = [
                str(value)
                for value in (existing_snapshot.get("sources") or [])
                if str(value).strip()
            ]
            if source_name not in sources:
                sources.append(source_name)
            existing_snapshot["sources"] = sources
            if incoming_is_newer:
                existing_snapshot["source"] = source_name
                existing_snapshot["input_fingerprint"] = snapshot.get("input_fingerprint")
            existing_snapshot["recorded_at"] = snapshot["recorded_at"]
            existing_snapshot["active_counts_after_merge"] = snapshot[
                "active_counts_after_merge"
            ]
            material_snapshot = existing_snapshot

        latest = self._pulse_store.get("latest")
        if not isinstance(latest, dict) or (
            self._pulse_time_sort_value(generated_at)
            >= self._pulse_time_sort_value(latest.get("generated_at"))
        ):
            self._pulse_store["latest"] = {
                key: copy.deepcopy(material_snapshot.get(key))
                for key in (
                    "snapshot_id",
                    "generated_at",
                    "first_generated_at",
                    "last_generated_at",
                    "source",
                    "sources",
                    "occurrences",
                    "content_fingerprint",
                    "input_fingerprint",
                    "summary",
                    "sentiment",
                    "catalysts",
                    "risks",
                    "model",
                )
            }
        self._pulse_store["limits"] = {
            "active_per_side": max(
                10,
                _safe_int(cfg.get("pulse_active_max_per_side"), 500),
            ),
            "generated_picks_per_side": 3,
            "reason_characters": 500,
            "history_snapshots": "unbounded_unique",
            "archive_entries": "unbounded",
        }
        if persist:
            self._persist_pulse_store_locked()
        return self._pulse_from_store_locked(cfg)

    def _pulse_from_store_locked(
        self,
        cfg: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        latest = self._pulse_store.get("latest")
        active = self._pulse_store.get("active") or {}
        active_long = [
            self._pulse_pick_copy_for_output(value)
            for value in (active.get("top_long") or [])
            if isinstance(value, dict)
        ]
        active_short = [
            self._pulse_pick_copy_for_output(value)
            for value in (active.get("top_short") or [])
            if isinstance(value, dict)
        ]
        if not isinstance(latest, dict) and not active_long and not active_short:
            return None

        active_long_keys = {str(value.get("event_key") or "") for value in active_long}
        active_short_keys = {str(value.get("event_key") or "") for value in active_short}

        latest_snapshot: Optional[Dict[str, Any]] = None
        if isinstance(latest, dict):
            latest_id = _safe_int(latest.get("snapshot_id"), 0)
            if latest_id:
                latest_snapshot = next(
                    (
                        snapshot
                        for snapshot in (self._pulse_store.get("snapshots") or [])
                        if isinstance(snapshot, dict)
                        and _safe_int(snapshot.get("snapshot_id"), 0) == latest_id
                    ),
                    None,
                )
        if latest_snapshot is None and self._pulse_store.get("snapshots"):
            valid_snapshots = [
                s
                for s in (self._pulse_store.get("snapshots") or [])
                if isinstance(s, dict)
            ]
            if valid_snapshots:
                latest_snapshot = max(
                    valid_snapshots,
                    key=lambda s: (
                        self._pulse_time_sort_value(s.get("generated_at")),
                        _safe_int(s.get("snapshot_id"), 0),
                    ),
                )
        latest_source = (
            latest_snapshot
            if isinstance(latest_snapshot, dict)
            else (latest if isinstance(latest, dict) else {})
        )

        # Trader-facing Pulse is the latest valid snapshot only. The cumulative
        # union remains available as explicit history metadata, never mixed into
        # today's recommendation card.
        current_top_long = [
            self._pulse_pick_copy_for_output(value)
            for value in (latest_source.get("top_long") or [])
            if isinstance(value, dict)
            and bool(value.get("eligible", True))
            and str(value.get("event_key") or "") in active_long_keys
        ]
        current_top_short = [
            self._pulse_pick_copy_for_output(value)
            for value in (latest_source.get("top_short") or [])
            if isinstance(value, dict)
            and bool(value.get("eligible", True))
            and str(value.get("event_key") or "") in active_short_keys
        ]

        response_limit = max(
            1,
            min(50, _safe_int(cfg.get("pulse_history_response_limit"), 20)),
        )
        snapshots = sorted(
            [
                value
                for value in (self._pulse_store.get("snapshots") or [])
                if isinstance(value, dict)
            ],
            key=lambda value: (
                self._pulse_time_sort_value(value.get("generated_at")),
                _safe_int(value.get("snapshot_id"), 0),
            ),
            reverse=True,
        )
        history: List[Dict[str, Any]] = []
        for snapshot in snapshots[:response_limit]:
            history.append({
                key: copy.deepcopy(snapshot.get(key))
                for key in (
                    "snapshot_id",
                    "generated_at",
                    "first_generated_at",
                    "last_generated_at",
                    "source",
                    "sources",
                    "occurrences",
                    "content_fingerprint",
                    "summary",
                    "sentiment",
                    "top_long",
                    "top_short",
                    "catalysts",
                    "risks",
                    "model",
                )
            })

        result = {
            "summary": str((latest or {}).get("summary") or "").strip(),
            "sentiment": str((latest or {}).get("sentiment") or "").strip().lower(),
            # Backward-compatible cumulative fields for API consumers. The UI
            # uses current_top_* below and never presents the union as today's call.
            "top_long": active_long,
            "top_short": active_short,
            "current_top_long": current_top_long,
            "current_top_short": current_top_short,
            "active_history": {
                "top_long": active_long,
                "top_short": active_short,
            },
            "catalysts": str((latest or {}).get("catalysts") or "").strip(),
            "risks": str((latest or {}).get("risks") or "").strip(),
            "model": str((latest or {}).get("model") or "").strip(),
            "generated_at": (latest or {}).get("generated_at"),
            "input_fingerprint": (latest or {}).get("input_fingerprint"),
            "history": history,
            "history_meta": {
                "schema_version": 1,
                "snapshot_count": len(snapshots),
                "generation_count": sum(
                    max(1, _safe_int(value.get("occurrences"), 1))
                    for value in snapshots
                ),
                "archived_pick_count": len(self._pulse_store.get("archive") or []),
                "active_long_count": len(active_long),
                "active_short_count": len(active_short),
                "display_mode": "current_top_fields_with_legacy_union",
                "response_limit": response_limit,
                "first_generated_at": (
                    (
                        snapshots[-1].get("first_generated_at")
                        or snapshots[-1].get("generated_at")
                    )
                    if snapshots
                    else None
                ),
                "last_generated_at": (
                    snapshots[0].get("generated_at") if snapshots else None
                ),
            },
            "persisted": True,
        }
        return result

    def _reconcile_and_get_persisted_pulse(
        self,
        cfg: Dict[str, Any],
        items: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        self._ensure_pulse_store_runtime()
        with self._pulse_store_lock:
            self._maybe_reload_pulse_store_locked()
            changed = self._reconcile_pulse_active_locked(items, cfg)
            if (
                changed
                or bool(getattr(self, "_pulse_store_needs_persist", False))
            ) and hasattr(self, "_pulse_store_path"):
                self._persist_pulse_store_locked()
            return self._pulse_from_store_locked(cfg)

    def import_pulse_snapshot(
        self,
        pulse: Dict[str, Any],
        items: Optional[List[Dict[str, Any]]] = None,
        *,
        source: str = "manual_import",
        generated_at: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Append/recover a historical Pulse and merge its still-active picks.

        This is intentionally provider-free. It supports recovery from an
        exported screenshot/report without inventing production fixtures. An
        older imported snapshot never overwrites a newer reason or side for the
        same ticker/event, while its unique picks are restored to the active
        union when their event is still upcoming.
        """
        if not isinstance(pulse, dict):
            return None
        self._ensure_pulse_store_runtime()
        candidate = copy.deepcopy(pulse)
        if generated_at is not None:
            candidate["generated_at"] = str(generated_at)
        if items is None:
            with self._lock:
                items = copy.deepcopy(list(self._state.get("items") or []))
        with self._pulse_store_lock:
            result = self._merge_pulse_snapshot_locked(
                candidate,
                list(items or []),
                self._get_config(),
                source=source,
                persist=hasattr(self, "_pulse_store_path"),
            )
        self._pre_earnings_pulse_cache = None
        if isinstance(result, dict) and hasattr(self, "_state"):
            with self._lock:
                self._state["pulse"] = copy.deepcopy(result)
                flags = dict(self._state.get("flags") or {})
                flags["pulse_used"] = True
                self._state["flags"] = flags
                if hasattr(self, "_pulse_store_path"):
                    self._persist_locked()
        return result

    @staticmethod
    def _pulse_input_fingerprint(items: List[Dict[str, Any]]) -> Optional[str]:
        candidates = sorted(
            [item for item in (items or []) if isinstance(item, dict)],
            key=lambda item: (
                _safe_int(item.get("trader_score"), -1),
                _safe_int(item.get("opportunity_score"), -1),
                _safe_int(item.get("gap_risk_numeric"), -1),
            ),
            reverse=True,
        )[:20]
        rows = []
        for item in candidates:
            ai = item.get("ai_decision") if isinstance(item.get("ai_decision"), dict) else {}
            digest = item.get("news_digest") if isinstance(item.get("news_digest"), dict) else {}
            rows.append(
                (
                    str(item.get("symbol") or "").upper(),
                    str(item.get("earnings_time_utc") or "")[:10],
                    str(item.get("direction_label") or "").upper(),
                    _safe_int(item.get("direction_bias_score"), 0),
                    _safe_int(item.get("trader_score"), 0),
                    str(digest.get("fingerprint") or ""),
                    str(ai.get("verdict") or ""),
                    round(float(_safe_float(ai.get("confidence"), 0.0) or 0.0), 3),
                )
            )
        if not rows:
            return None
        return hashlib.sha256(
            json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8", errors="ignore"
            )
        ).hexdigest()[:24]

    def _compute_pulse(
        self, cfg: Dict[str, Any], items: List[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        """Aggregate Pre-Earnings Pulse: one LLM call over the top gap-risk
        items producing {summary, sentiment, top_long, top_short, catalysts,
        risks, model, generated_at}. Cloud-only (local too slow), non-fatal
        (None on any error / parse failure / disabled). Mirrors
        PremarketScannerAgent._compute_premarket_pulse. Task PE-premium C3."""
        if not bool(cfg.get("pulse_enabled", True)):
            return None
        eligible_items = [
            item
            for item in (items or [])
            if (
                isinstance(item, dict)
                and not self._pulse_item_already_reported_news(item)
                and (
                    "recommendation_eligible" not in item
                    or (
                        bool(item.get("recommendation_eligible"))
                        and str(item.get("direction_label") or "").upper() in {"LONG", "SHORT"}
                    )
                )
            )
        ]
        if not eligible_items:
            return None
        try:
            if not (ollama.is_cloud_host() or ollama.check_availability()):
                return None
            model = self._analyst_model()
        except Exception:
            return None
        if not model:
            return None

        top = sorted(
            eligible_items,
            key=lambda c: (
                _safe_int(c.get("trader_score"), -1),
                _safe_int(c.get("opportunity_score"), -1),
                _safe_int(c.get("coverage_score"), -1),
                _safe_int(c.get("gap_risk_numeric"), -1),
            ),
            reverse=True,
        )[:20]
        lines: List[str] = []
        for c in top:
            sym = str(c.get("symbol") or "")
            sess = str(c.get("session") or "UNKNOWN")
            gap_numeric = _safe_int(c.get("gap_risk_numeric"), None)
            # Honest state for gap-unavailable rows: "gapN/A", not "gap0"
            # (0 reads as low risk and would anchor the model wrongly).
            gap_text = f"gap{gap_numeric}" if gap_numeric is not None else "gapN/A"
            label = str(c.get("direction_label") or "NEUTRAL")
            bias = _safe_int(c.get("direction_bias_score"), 0)
            structured = _safe_int(c.get("structured_direction_bias_score"), None)
            # When the structured lean disagrees with the blended score (an AI
            # blend can pull a structured +22 row back to NEUTRAL+14), show
            # both — the anchor rule below is phrased on the structured lean,
            # and the model cannot honor what it cannot see.
            structured_text = (
                f" (structured{structured:+d})"
                if structured is not None and structured != bias
                else ""
            )
            trader_score = _safe_int(c.get("trader_score"), 0)
            coverage_score = _safe_int(c.get("coverage_score"), 0)
            news_list = [row for row in (c.get("top_news") or []) if isinstance(row, dict)][:3]
            headline_text = " | ".join(
                str(row.get("title") or "").strip()[:100]
                for row in news_list
                if str(row.get("title") or "").strip()
            )
            lines.append(
                f"- {sym} {sess} {gap_text} {label}{bias:+d}{structured_text} "
                f"trader{trader_score} coverage{coverage_score} news=\"{headline_text}\""
            )

        system = (
            "You are a senior pre-earnings analyst. Produce a CONCISE "
            "pre-earnings pulse as STRICT JSON only with keys: summary (one or "
            "two sentences on the upcoming earnings slate: breadth, sentiment, "
            "dominant themes), sentiment (one of bullish|bearish|neutral), "
            "top_long (array of {symbol,reason} for the 1-3 best long setups), "
            "top_short (array of {symbol,reason} for the 1-3 best short setups), "
            "catalysts (one short sentence on the dominant catalysts), risks "
            "(one short sentence on the main risk to the read). Be honest and "
            "terse; do not invent symbols or data not in the list. Each list "
            "line ends with that row's current final direction label and score "
            "(e.g. LONG+43), plus its structured score when that differs from "
            "the blended one: never pick a symbol for the side opposing that "
            "label or its structured lean (no top_short for a LONG row or a "
            "row whose structured score is >= +15, no top_long for a SHORT "
            "row or a row whose structured score is <= -15. Never select a "
            "NEUTRAL row. Every supplied row already passed the calendar, "
            "fresh-news and AI eligibility gates."
        )
        prompt = (
            "Upcoming pre-earnings radar items (top gap-risk):\n"
            + "\n".join(lines)
            + "\nReturn STRICT JSON only: summary, sentiment, top_long, top_short, catalysts, risks."
        )
        timeout_sec = call_timeout_for(model, max(20, min(120, _safe_int(cfg.get("pulse_timeout_seconds"), 45))))
        try:
            # `system` is the supported kwarg on core.ollama_client.generate
            # (not `system_override`) — same call shape as
            # _compute_track_record_ai_summary.
            result = ollama.generate(prompt, model=model, system=system, timeout_override=timeout_sec)
        except Exception:
            return None
        if not isinstance(result, dict) or result.get("error"):
            return None
        raw = str(result.get("response") or "").strip()
        parsed = self._extract_json(raw)
        if not isinstance(parsed, dict):
            return None
        pulse = {
            "summary": str(parsed.get("summary") or "").strip(),
            "sentiment": str(parsed.get("sentiment") or "").strip().lower(),
            "top_long": self._normalize_pulse_picks(parsed.get("top_long")),
            "top_short": self._normalize_pulse_picks(parsed.get("top_short")),
            "catalysts": str(parsed.get("catalysts") or "").strip(),
            "risks": str(parsed.get("risks") or "").strip(),
            "model": str(result.get("_meta", {}).get("model") or model or "").strip(),
            "generated_at": _iso(_utc_now()),
            "input_fingerprint": self._pulse_input_fingerprint(items),
        }
        # Post-validate against the current rows (audit P0-1): the model picks
        # from stale headline tone and can name a symbol for the side opposing
        # its own row's final label (live: ACMR picked short while the row was
        # LONG +43) or opposing its NEUTRAL row's structured lean (live: QBTS
        # picked short while structured was +22). A contradictory pick is
        # dropped — never displayed, never merged into the active union.
        labels = self._pulse_labels_by_symbol(items)
        structured_scores = self._pulse_structured_scores_by_symbol(items)
        pulse["top_long"] = [
            pick
            for pick in pulse["top_long"]
            if not self._pulse_pick_contradicts_label(pick, "top_long", labels, structured_scores)
        ]
        pulse["top_short"] = [
            pick
            for pick in pulse["top_short"]
            if not self._pulse_pick_contradicts_label(pick, "top_short", labels, structured_scores)
        ]
        if not (pulse["summary"] or pulse["top_long"] or pulse["top_short"]):
            return None
        return pulse

    @staticmethod
    def _pulse_labels_by_symbol(items: List[Dict[str, Any]]) -> Dict[str, str]:
        labels: Dict[str, str] = {}
        for item in items or []:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol") or "").upper().strip()
            if not symbol:
                continue
            label = str(item.get("direction_label") or "").strip().upper()
            if label:
                labels[symbol] = label
        return labels

    @staticmethod
    def _pulse_structured_scores_by_symbol(items: List[Dict[str, Any]]) -> Dict[str, float]:
        """Structured (pre-AI-blend) direction score per symbol, for the pulse
        pick-eligibility guard. Falls back to the final `direction_bias_score`
        when the structured field is missing (older snapshots / bare fixtures);
        items without any finite score are simply absent from the map — a
        missing score never blocks a pick (the label rule still applies)."""
        scores: Dict[str, float] = {}
        for item in items or []:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol") or "").upper().strip()
            if not symbol:
                continue
            raw = item.get("structured_direction_bias_score")
            if raw is None:
                raw = item.get("direction_bias_score")
            val = _safe_float(raw, None)
            if val is None or not math.isfinite(val):
                continue
            scores[symbol] = float(val)
        return scores

    @staticmethod
    def _pulse_pick_contradicts_label(
        pick: Dict[str, Any],
        side: str,
        labels: Dict[str, str],
        scores: Optional[Dict[str, float]] = None,
    ) -> bool:
        """A Pulse side-pick must be defensible from the row it names
        (audit P0-1, strengthened in round 2). A pick is INELIGIBLE when:

        - side is long AND the row's final label is SHORT, or its structured
          score is <= -15; or
        - side is short AND the row's final label is LONG, or its structured
          score is >= +15.

        Current production rows gate NEUTRAL before this helper through
        recommendation_eligible. Legacy snapshots without that field retain
        the historical small-lean behavior for migration compatibility.
        """
        symbol = str((pick or {}).get("symbol") or "").upper().strip()
        label = str(labels.get(symbol) or "").upper()
        score = (scores or {}).get(symbol)
        if side == "top_long":
            return label == "SHORT" or label == "INSUFFICIENT_DATA" or (score is not None and score <= -15.0)
        if side == "top_short":
            return label == "LONG" or label == "INSUFFICIENT_DATA" or (score is not None and score >= 15.0)
        return False

    @staticmethod
    def _normalize_pulse_picks(value: Any) -> List[Dict[str, str]]:
        out: List[Dict[str, str]] = []
        seen: set = set()
        if isinstance(value, list):
            for item in value[:3]:
                if isinstance(item, dict):
                    symbol = str(item.get("symbol") or "").upper().strip()
                    reason = str(item.get("reason") or "").strip()[:500]
                elif isinstance(item, str):
                    symbol = item.upper().strip()
                    reason = ""
                else:
                    continue
                if not symbol or symbol in seen:
                    continue
                seen.add(symbol)
                out.append({"symbol": symbol, "reason": reason})
        return out

    def get_pulse(
        self,
        cfg: Dict[str, Any],
        items: List[Dict[str, Any]],
        ttl_seconds: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """Return the durable cumulative Pulse, refreshing the LLM only on TTL.

        The in-memory TTL remains the fast path. On process restart the latest
        sidecar snapshot supplies the same TTL, preventing a duplicate paid
        request. A transient LLM failure never erases active picks: persisted
        data is served while the next cycle remains free to retry.
        """
        if not bool(cfg.get("pulse_enabled", True)):
            return None
        if ttl_seconds is None:
            ttl_seconds = max(30.0, float(_safe_int(cfg.get("pulse_ttl_seconds"), 300)))
        self._ensure_pulse_store_runtime()
        now = time.monotonic()
        input_fingerprint = self._pulse_input_fingerprint(items)
        cached = getattr(self, "_pre_earnings_pulse_cache", None)
        cached_fingerprint = (
            str((cached[1] or {}).get("input_fingerprint") or "").strip()
            if cached is not None and isinstance(cached[1], dict)
            else ""
        )
        cached_matches = (
            not cached_fingerprint
            or not input_fingerprint
            or cached_fingerprint == input_fingerprint
        )
        if (
            cached is not None
            and (now - float(cached[0])) < ttl_seconds
            and cached[1] is not None
            and cached_matches
        ):
            reconciled = self._reconcile_and_get_persisted_pulse(cfg, items)
            return reconciled if isinstance(reconciled, dict) else cached[1]

        persisted = self._reconcile_and_get_persisted_pulse(cfg, items)
        if hasattr(self, "_pulse_store_path") and isinstance(persisted, dict):
            generated_dt = _parse_iso(persisted.get("generated_at"))
            if generated_dt is not None:
                age_seconds = max(
                    0.0,
                    (_utc_now() - generated_dt).total_seconds(),
                )
                persisted_fingerprint = str(persisted.get("input_fingerprint") or "").strip()
                persisted_matches = (
                    not persisted_fingerprint
                    or not input_fingerprint
                    or persisted_fingerprint == input_fingerprint
                )
                if age_seconds < ttl_seconds and persisted_matches:
                    self._pre_earnings_pulse_cache = (now, persisted)
                    return persisted

        generated = self._compute_pulse(cfg, items)
        if isinstance(generated, dict):
            with self._pulse_store_lock:
                pulse = self._merge_pulse_snapshot_locked(
                    generated,
                    list(items or []),
                    cfg,
                    source="llm",
                    persist=hasattr(self, "_pulse_store_path"),
                )
            self._pre_earnings_pulse_cache = (now, pulse)
            return pulse

        # Do not cache a failed generation; the next cycle retries. Existing
        # durable data remains visible and is never replaced by None.
        return persisted

    def _build_payload(
        self,
        cfg: Dict[str, Any],
        items: List[Dict[str, Any]],
        news_age: Optional[int],
        earnings_age: Optional[int],
        news_available: bool,
        earnings_available: bool,
        ollama_used: bool,
        calibration: Optional[Dict[str, Any]] = None,
        mismatch_details: Optional[Dict[str, Dict[str, Any]]] = None,
        calendar_refresh_attempted_symbols: Optional[List[str]] = None,
        calendar_refresh_results: Optional[Dict[str, Any]] = None,
        reported_today: Optional[List[Dict[str, Any]]] = None,
        calendar_validation_rejections: Optional[List[Dict[str, Any]]] = None,
        pulse: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        mismatch_details = mismatch_details if isinstance(mismatch_details, dict) else {}
        mismatch_symbols = list(dict.fromkeys([str(s or "").upper().strip() for s in mismatch_details.keys() if str(s or "").strip()]))
        status = {
            "last_run_time": _iso(_utc_now()),
            "last_success_time": None,
            "last_error": None,
            "last_inputs_used": {
                "news_cache_age_seconds": news_age,
                "earnings_cache_age_seconds": earnings_age,
            },
            "next_run_in": int(cfg["refresh_seconds"]),
            "last_recalibration_quarter": self._feedback.get("last_recalibration_quarter"),
            "coverage_warmup": self._default_coverage_warmup_status(),
        }
        return {
            "as_of_utc": _iso(_utc_now()),
            "lookahead_days": int(cfg["lookahead_days"]),
            "refresh_seconds": int(cfg["refresh_seconds"]),
            "inputs_used": {
                "news_cache_age_seconds": news_age,
                "earnings_cache_age_seconds": earnings_age,
            },
            "flags": {
                "news_available": bool(news_available),
                "earnings_available": bool(earnings_available),
                # `ollama_used` means FRESH LLM enrichment ran in this cycle.
                # Verdicts restored from the durable per-event registry do not
                # set it, so `ollama_used=false` with `ai_verdicts_count>0`
                # reads as "serving cached AI verdicts", not "AI off"
                # (audit P1-6 — 194 cached verdicts previously looked like AI
                # never ran).
                "ollama_used": bool(ollama_used),
                "ai_verdicts_count": sum(
                    1
                    for item in items
                    if isinstance(item, dict)
                    and isinstance(item.get("ai_decision"), dict)
                    and item["ai_decision"].get("used")
                ),
                "reported_today_count": len(reported_today or []),
                "pulse_used": isinstance(pulse, dict),
            },
            "calibration": calibration if isinstance(calibration, dict) else {"sample_size": 0},
            "mismatch_symbols": mismatch_symbols,
            "mismatch_details": mismatch_details,
            "calendar_refresh_attempted_symbols": list(calendar_refresh_attempted_symbols or []),
            "calendar_refresh_results": dict(calendar_refresh_results or {}),
            "reported_today": list(reported_today or []),
            "calendar_validation_rejections": list(calendar_validation_rejections or []),
            "pulse": pulse if isinstance(pulse, dict) else None,
            "items": items,
            "status": status,
            "disabled": False,
        }

    # The old FMP cache file (fmp_cache.json) no longer exists; quotes and
    # fundamentals now come from StockDataClient (Yahoo) behind a per-symbol
    # TTL cache so the scoring path doesn't hammer the provider.
    _QUOTE_TTL_SECONDS = 300.0
    _GROWTH_TTL_SECONDS = 86400.0
    # Analyst consensus barely moves intraday; cache 6h and reuse last-good on throttle.
    _ANALYST_TTL_SECONDS = 21600.0

    def _get_cached_quote(self, symbol: str) -> Dict[str, Any]:
        sym = str(symbol or "").upper().strip()
        if not sym:
            return {}
        cache = getattr(self, "_quote_ttl_cache", None)
        if cache is None:
            cache = self._quote_ttl_cache = {}
        hit = cache.get(sym)
        now = time.time()
        if hit and (now - hit[0]) < self._QUOTE_TTL_SECONDS:
            return hit[1]
        if self._external_budget_exhausted():
            return hit[1] if hit and isinstance(hit[1], dict) else {}
        try:
            quote = StockDataClient.get_quote(sym, ttl=300) or {}
        except Exception:
            quote = {}
        if not isinstance(quote, dict):
            quote = {}
        if quote:
            cache[sym] = (now, quote)
            return quote
        # A provider/budget miss must not poison the cache with an empty value.
        return hit[1] if hit and isinstance(hit[1], dict) else {}

    def _load_state_rows_map(self) -> Dict[str, Dict[str, Any]]:
        try:
            path = Path(STATE_PATH)
            if not path.exists():
                return {}
            mtime = path.stat().st_mtime
            snap = self._state_rows_snapshot
            if snap.get("mtime") == mtime and isinstance(snap.get("data"), dict):
                return snap.get("data", {})
            with path.open("r", encoding="utf-8") as f:
                payload = json.load(f)
            rows = {}
            raw_tickers = payload.get("tickers", []) if isinstance(payload, dict) else []
            if isinstance(raw_tickers, list):
                for row in raw_tickers:
                    if not isinstance(row, dict):
                        continue
                    symbol = str(row.get("ticker") or row.get("symbol") or "").upper().strip()
                    if not symbol:
                        continue
                    rows[symbol] = row
            elif isinstance(raw_tickers, dict):
                for symbol, row in raw_tickers.items():
                    sym = str(symbol or "").upper().strip()
                    if not sym:
                        continue
                    if isinstance(row, dict):
                        merged = dict(row)
                        if "ticker" not in merged and "symbol" not in merged:
                            merged["symbol"] = sym
                        rows[sym] = merged
            self._state_rows_snapshot = {"mtime": mtime, "data": rows}
            return rows
        except Exception:
            return {}

    def _is_enabled(self) -> bool:
        return is_agent_enabled("pre_earnings", config=self.config_manager.config, default=False)

    def _analyst_model(self) -> Optional[str]:
        """The model the owner assigned to the Pre-Earnings analyst; the global model when the role has none."""
        from core.ai.role_resolution import assigned_model

        return assigned_model("pre_earnings_analyst", config=getattr(self, "config_manager", None),
                              fallback=ollama.get_best_model())

    def _get_config(self) -> Dict[str, Any]:
        cfg = dict(self.DEFAULT_CONFIG)
        merged = (self.config_manager.config or {}).get("pre_earnings", {}) or {}
        if isinstance(merged, dict):
            cfg.update(merged)

        for env_key, (cfg_key, caster) in self.ENV_MAP.items():
            raw = os.getenv(env_key)
            if raw is None or str(raw).strip() == "":
                continue
            try:
                cfg[cfg_key] = caster(raw)
            except Exception:
                continue

        cfg["refresh_seconds"] = max(30, _safe_int(cfg.get("refresh_seconds"), 600))
        cfg["startup_delay_seconds"] = max(
            0,
            min(600, _safe_int(cfg.get("startup_delay_seconds"), 20)),
        )
        # Keep the documented provider budget independent of result-table size.
        # Coverage warms incrementally across cycles; silently scaling a
        # 200-row configuration to 300 calls caused startup request storms and
        # starved interactive endpoints. Operators can still opt into a larger
        # explicit limit through config or PRE_EARNINGS_EXTERNAL_CALL_BUDGET.
        cfg["external_call_budget"] = max(
            1,
            min(300, _safe_int(cfg.get("external_call_budget"), 30)),
        )
        cfg["lookahead_days"] = max(1, min(45, _safe_int(cfg.get("lookahead_days"), 30)))
        cfg["lookback_hours"] = max(24, _safe_int(cfg.get("lookback_hours"), 336))
        # Top 10 must be selected from the complete calendar window, not from a
        # silently truncated 200-name sample. This is only the in-memory result
        # capacity; remote/provider traffic is still constrained separately by
        # external_call_budget and the cache-first enrichment controls.
        cfg["max_results"] = max(1, min(750, _safe_int(cfg.get("max_results"), 400)))
        cfg["cache_ttl_seconds"] = max(60, _safe_int(cfg.get("cache_ttl_seconds"), 600))
        cfg["mismatch_past_grace_days"] = max(1, min(7, _safe_int(cfg.get("mismatch_past_grace_days"), 3)))
        cfg["schedule_conflict_min_spacing_days"] = max(
            21,
            min(120, _safe_int(cfg.get("schedule_conflict_min_spacing_days"), 45)),
        )
        cfg["schedule_conflict_max_news_rows"] = max(
            0,
            min(8, _safe_int(cfg.get("schedule_conflict_max_news_rows"), 0)),
        )
        cfg["use_ollama"] = bool(cfg.get("use_ollama", True))
        cfg["ollama_max_tickers"] = max(1, min(50, _safe_int(cfg.get("ollama_max_tickers"), 15)))
        cfg["ollama_timeout_seconds"] = max(1, min(45, _safe_int(cfg.get("ollama_timeout_seconds"), 6)))
        cfg["ollama_decision_weight"] = float(
            _clamp(_safe_float(cfg.get("ollama_decision_weight"), 0.35) or 0.35, 0.0, 0.6)
        )
        cfg["news_evidence_limit"] = max(
            4,
            min(20, _safe_int(cfg.get("news_evidence_limit"), 12)),
        )
        cfg["news_prompt_char_budget"] = max(
            3000,
            min(16000, _safe_int(cfg.get("news_prompt_char_budget"), 9000)),
        )
        cfg["news_deep_refresh_batch_size"] = max(
            1,
            min(25, _safe_int(cfg.get("news_deep_refresh_batch_size"), 10)),
        )
        cfg["news_priority_refresh_size"] = max(
            1,
            min(
                cfg["news_deep_refresh_batch_size"],
                _safe_int(cfg.get("news_priority_refresh_size"), 6),
            ),
        )
        cfg["news_deep_refresh_seconds"] = max(
            300,
            min(6 * 3600, _safe_int(cfg.get("news_deep_refresh_seconds"), 900)),
        )
        cfg["news_change_reanalysis"] = bool(cfg.get("news_change_reanalysis", True))
        cfg["top_predictions_limit"] = max(
            1,
            min(20, _safe_int(cfg.get("top_predictions_limit"), 10)),
        )
        cfg["top_prediction_min_coverage"] = max(
            0,
            min(100, _safe_int(cfg.get("top_prediction_min_coverage"), 45)),
        )
        cfg["top_prediction_min_trader_score"] = max(
            0, min(100, _safe_int(cfg.get("top_prediction_min_trader_score"), 60))
        )
        cfg["top_prediction_min_abs_bias"] = max(
            0,
            min(100, _safe_int(cfg.get("top_prediction_min_abs_bias"), 20)),
        )
        cfg["top_prediction_require_verified_event"] = bool(
            cfg.get("top_prediction_require_verified_event", True)
        )
        cfg["top_prediction_require_ai"] = bool(cfg.get("top_prediction_require_ai", True))
        cfg["top_prediction_min_news_articles"] = max(
            1,
            min(20, _safe_int(cfg.get("top_prediction_min_news_articles"), 3)),
        )
        cfg["top_prediction_min_news_sources"] = max(
            1,
            min(10, _safe_int(cfg.get("top_prediction_min_news_sources"), 1)),
        )
        cfg["top_prediction_max_news_age_hours"] = max(
            6,
            min(336, _safe_int(cfg.get("top_prediction_max_news_age_hours"), 96)),
        )
        cfg["recommendation_max_age_seconds"] = max(
            600,
            min(6 * 3600, _safe_int(cfg.get("recommendation_max_age_seconds"), 1800)),
        )
        cfg["feedback_lookback_days"] = max(30, min(1095, _safe_int(cfg.get("feedback_lookback_days"), 365)))
        cfg["feedback_min_samples"] = max(8, min(400, _safe_int(cfg.get("feedback_min_samples"), 24)))
        cfg["recalibration_min_samples"] = max(20, min(800, _safe_int(cfg.get("recalibration_min_samples"), 40)))
        cfg["high_gap_threshold_pct"] = float(
            _clamp(_safe_float(cfg.get("high_gap_threshold_pct"), 5.0) or 5.0, 2.0, 20.0)
        )
        cfg["pulse_active_max_per_side"] = max(
            10,
            min(2000, _safe_int(cfg.get("pulse_active_max_per_side"), 500)),
        )
        cfg["pulse_history_response_limit"] = max(
            1,
            min(50, _safe_int(cfg.get("pulse_history_response_limit"), 20)),
        )
        cfg["pulse_undated_retention_days"] = max(
            7,
            min(180, _safe_int(cfg.get("pulse_undated_retention_days"), 45)),
        )
        cfg["peer_readthrough_hours"] = max(
            24,
            min(336, _safe_int(cfg.get("peer_readthrough_hours"), 96)),
        )
        cfg["peer_readthrough_max_peers"] = max(
            1,
            min(20, _safe_int(cfg.get("peer_readthrough_max_peers"), 5)),
        )
        return cfg

    def _load_state(self) -> Dict[str, Any]:
        cfg = self._get_config()
        template = {
            "state_revision": 0,
            "as_of_utc": None,
            "lookahead_days": int(cfg["lookahead_days"]),
            "refresh_seconds": int(cfg["refresh_seconds"]),
            "inputs_used": {
                "news_cache_age_seconds": None,
                "earnings_cache_age_seconds": None,
            },
            "flags": {
                "news_available": False,
                "earnings_available": False,
                "ollama_used": False,
            },
            "calibration": {
                "sample_size": 0,
                "brier": None,
                "mae": None,
                "buckets": [],
            },
            "mismatch_symbols": [],
            "mismatch_details": {},
            "calendar_refresh_attempted_symbols": [],
            "calendar_refresh_results": {},
            "items": [],
            "status": {
                "last_run_time": None,
                "last_success_time": None,
                "last_error": None,
                "last_inputs_used": {
                    "news_cache_age_seconds": None,
                    "earnings_cache_age_seconds": None,
                },
                "next_run_in": int(cfg["refresh_seconds"]),
                "last_recalibration_quarter": self._feedback.get("last_recalibration_quarter") if hasattr(self, "_feedback") else None,
                "coverage_warmup": self._default_coverage_warmup_status(),
            },
            "disabled": not self._is_enabled(),
        }
        try:
            if PRE_EARNINGS_OUTPUT_PATH.exists():
                with PRE_EARNINGS_OUTPUT_PATH.open("r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    repaired_items = 0
                    recovered_ai_items = 0
                    normalized_revision = max(
                        0,
                        _safe_int(loaded.get("state_revision"), 0),
                    )
                    revision_repaired = (
                        isinstance(loaded.get("state_revision"), bool)
                        or loaded.get("state_revision") != normalized_revision
                    )
                    loaded["state_revision"] = normalized_revision
                    raw_items = loaded.get("items")
                    if isinstance(raw_items, list):
                        for item in raw_items:
                            if not isinstance(item, dict):
                                continue
                            if self._repair_loaded_direction_contract(item, cfg):
                                repaired_items += 1
                            before_ai = item.get("ai_decision")
                            before_ai_used = (
                                isinstance(before_ai, dict)
                                and bool(before_ai.get("used"))
                            )
                            before_ollama = isinstance(item.get("ollama"), dict)
                            carried = self._carry_forward_ai_decision(
                                item,
                                item,
                                cfg,
                            )
                            after_ai = item.get("ai_decision")
                            after_ai_used = (
                                isinstance(after_ai, dict)
                                and bool(after_ai.get("used"))
                            )
                            after_ollama = isinstance(item.get("ollama"), dict)
                            if carried and (
                                (not before_ai_used and after_ai_used)
                                or (not before_ollama and after_ollama)
                            ):
                                recovered_ai_items += 1
                    if repaired_items or recovered_ai_items or revision_repaired:
                        if self._persist_loaded_schema_upgrade(loaded):
                            logger.info(
                                "Upgraded pre-earnings cache: schema=%s, AI/Ollama=%s, revision=%s",
                                repaired_items,
                                recovered_ai_items,
                                normalized_revision,
                            )
                    merged = copy.deepcopy(template)
                    merged.update(loaded)
                    merged["status"] = {**template["status"], **(loaded.get("status", {}) or {})}
                    # A "cycle running" marker read from disk belongs to the
                    # process that wrote it, not to this one.
                    merged["status"]["cycle_running"] = False
                    if "items" in loaded and isinstance(loaded["items"], list):
                        merged["items"] = self._filter_schedule_conflict_items(
                            copy.deepcopy(loaded["items"]),
                            cfg,
                        )
                    return merged
        except Exception as e:
            logger.warning(f"Pre-earnings cache load failed: {e}")
        return template

    def _repair_loaded_direction_contract(
        self,
        item: Dict[str, Any],
        cfg: Dict[str, Any],
    ) -> bool:
        """Add direction fields introduced after legacy snapshots were written.

        Only absent additive fields are copied back. Existing scores, labels,
        reasons, badges and provider data remain byte-for-byte equivalent after
        JSON round-tripping; no provider is needed to derive these values.
        """
        canonical_fields = (
            "structured_direction_label",
            "llm_direction_score",
            "decision_alignment",
        )
        missing = [field for field in canonical_fields if field not in item]
        if not missing:
            return False

        derived = copy.deepcopy(item)
        self._synchronize_direction_decision(derived, cfg)
        ai_decision = item.get("ai_decision")
        ai_used = isinstance(ai_decision, dict) and bool(ai_decision.get("used"))
        fallbacks = {
            "structured_direction_label": None,
            "llm_direction_score": None,
            "decision_alignment": "MIXED" if ai_used else "NO_LLM",
        }
        for field in missing:
            item[field] = copy.deepcopy(derived.get(field, fallbacks[field]))
        return True

    @staticmethod
    def _persist_loaded_schema_upgrade(payload: Dict[str, Any]) -> bool:
        """Atomically repair the canonical cache without rebuilding its rows."""
        path = Path(PRE_EARNINGS_OUTPUT_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(
            f".{path.name}.{os.getpid()}.{threading.get_ident()}.schema-upgrade.tmp"
        )
        try:
            with temp_path.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(str(temp_path), str(path))
            return True
        except Exception as exc:
            logger.warning("Pre-earnings cache schema upgrade failed: %s", exc)
            return False
        finally:
            try:
                if temp_path.exists():
                    temp_path.unlink()
            except Exception:
                pass

    def _persist_locked(self) -> None:
        PRE_EARNINGS_OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp_path = PRE_EARNINGS_OUTPUT_PATH.with_suffix(".json.tmp")
        try:
            current_revision = max(
                _safe_int(getattr(self, "_state_revision", 0), 0),
                _safe_int(self._state.get("state_revision"), 0),
            )
            self._state_revision = max(
                current_revision + 1,
                int(time.time() * 1000),
            )
            self._state["state_revision"] = self._state_revision
            for item in self._state.get("items", []) or []:
                if isinstance(item, dict):
                    self._cache_ai_from_item_locked(item)
            # Publish the read fallback before the comparatively expensive JSON
            # serialization. GET callers that time out on this lock must see the
            # same revision being persisted, never the previous snapshot.
            self._state_read_fallback = copy.deepcopy(self._state)
            with temp_path.open("w", encoding="utf-8") as f:
                json.dump(self._state, f, indent=2, ensure_ascii=False, allow_nan=False)
            os.replace(str(temp_path), str(PRE_EARNINGS_OUTPUT_PATH))
            self._persist_ai_cache_locked()
        except Exception as e:
            logger.error(f"Pre-earnings cache persist failed: {e}")
            try:
                if temp_path.exists():
                    temp_path.unlink()
            except Exception:
                pass


_pre_earnings_agent: Optional[PreEarningsAgent] = None
_pre_earnings_lock = threading.Lock()


def get_pre_earnings_agent(config_manager: Optional[ConfigManager] = None) -> PreEarningsAgent:
    global _pre_earnings_agent
    with _pre_earnings_lock:
        if _pre_earnings_agent is None:
            _pre_earnings_agent = PreEarningsAgent(config_manager=config_manager)
    return _pre_earnings_agent


def get_pre_earnings_agent_if_ready() -> Optional[PreEarningsAgent]:
    return _pre_earnings_agent
