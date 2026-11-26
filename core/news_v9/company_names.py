"""Company names for news relevance matching — local data only.

Headlines usually name the company ("Nvidia", "Atlassian"), not the ticker.
The news agent's name cache was never filled, so such headlines were dropped
for the ticker and lexically ambiguous tickers could not be confirmed.

Names come from files the app already keeps on disk (no network):
  1. the earnings calendar cache   (``calendar_events_cache.json``)
  2. the scanner's shared state    (``scanner_state.json``)
  3. the eToro instrument map      (``etoro_instruments_map.json``)
  4. the SEC filings cache         (``sec_filings_cache.json``)
with a small bundled map for the largest tickers as the fallback.

Matching uses whole words only (the same boundary discipline as ticker
symbols): "Apple" matches, "Snapple" and "pineapple" do not, and a company
whose distinguishing word is an ordinary English word ("Strategy", "Target",
"Unity") is matched only by its full name or a bundled alias.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger("NewsCompanyNames")

# ticker -> aliases as written in headlines (first entry = display name).
BUNDLED_COMPANY_ALIASES: Dict[str, Tuple[str, ...]] = {
    "AAPL": ("Apple",),
    "MSFT": ("Microsoft",),
    "NVDA": ("Nvidia",),
    "AMZN": ("Amazon",),
    "GOOGL": ("Alphabet", "Google"),
    "GOOG": ("Alphabet", "Google"),
    "META": ("Meta Platforms", "Facebook"),
    "TSLA": ("Tesla",),
    "BRK.B": ("Berkshire Hathaway", "Berkshire"),
    "BRK-B": ("Berkshire Hathaway", "Berkshire"),
    "AVGO": ("Broadcom",),
    "TSM": ("Taiwan Semiconductor", "TSMC"),
    "LLY": ("Eli Lilly",),
    "JPM": ("JPMorgan", "JP Morgan", "J.P. Morgan"),
    "V": ("Visa Inc",),
    "MA": ("Mastercard",),
    "WMT": ("Walmart",),
    "XOM": ("Exxon Mobil", "ExxonMobil", "Exxon"),
    "UNH": ("UnitedHealth",),
    "ORCL": ("Oracle",),
    "COST": ("Costco",),
    "HD": ("Home Depot",),
    "PG": ("Procter & Gamble", "Procter and Gamble"),
    "JNJ": ("Johnson & Johnson",),
    "NFLX": ("Netflix",),
    "BAC": ("Bank of America",),
    "KO": ("Coca-Cola",),
    "PEP": ("PepsiCo", "Pepsi"),
    "AMD": ("Advanced Micro Devices",),
    "CRM": ("Salesforce",),
    "ADBE": ("Adobe",),
    "CSCO": ("Cisco",),
    "INTC": ("Intel",),
    "QCOM": ("Qualcomm",),
    "IBM": ("International Business Machines",),
    "DIS": ("Walt Disney", "Disney"),
    "BA": ("Boeing",),
    "GS": ("Goldman Sachs",),
    "MS": ("Morgan Stanley",),
    "PFE": ("Pfizer",),
    "MRK": ("Merck",),
    "ABBV": ("AbbVie",),
    "NKE": ("Nike",),
    "MCD": ("McDonald's", "McDonalds"),
    "UBER": ("Uber",),
    "PLTR": ("Palantir",),
    "COIN": ("Coinbase",),
    "MSTR": ("MicroStrategy",),
    "PYPL": ("PayPal",),
    "SHOP": ("Shopify",),
    "BABA": ("Alibaba",),
    "BIDU": ("Baidu",),
    "NIO": ("Nio Inc",),
    "SMCI": ("Super Micro Computer", "Supermicro", "Super Micro"),
    "DELL": ("Dell Technologies", "Dell"),
    "MU": ("Micron",),
    "ARM": ("Arm Holdings",),
    "ASML": ("ASML",),
    "SPCX": ("SpaceX",),
    "MRNA": ("Moderna",),
    "T": ("AT&T",),
    "GM": ("General Motors",),
    "F": ("Ford Motor",),
    "AI": ("C3.ai", "C3 AI"),
    "TEAM": ("Atlassian",),
    "SNOW": ("Snowflake",),
    "PANW": ("Palo Alto Networks",),
    "CRWD": ("CrowdStrike",),
    "SOFI": ("SoFi",),
    "HOOD": ("Robinhood",),
    "MARA": ("MARA Holdings", "Marathon Digital"),
    "RIOT": ("Riot Platforms",),
    "INTU": ("Intuit",),
    "PDD": ("PDD Holdings", "Pinduoduo"),
    "SNAP": ("Snap Inc", "Snapchat"),
    "S": ("SentinelOne",),
    "DJT": ("Trump Media",),
    "POWL": ("Powell Industries",),
    "RCAT": ("Red Cat Holdings",),
}

# Companies whose news moves the whole tape. A headline naming one of them is
# scored on the covered-company base even when the symbol is not on the
# watchlist (see core.news_v9.impact_rules).
BELLWETHER_TICKERS: Tuple[str, ...] = (
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "BRK.B", "AVGO",
    "TSM", "JPM", "WMT", "XOM", "NFLX", "AMD", "INTC", "BA", "GS", "DIS",
)

# Trailing legal/structural words that are not part of how a headline names
# the company.
_LEGAL_SUFFIXES = frozenset({
    "INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY", "LTD",
    "LIMITED", "PLC", "LLC", "LP", "NV", "SA", "AG", "SE", "AB", "ASA", "OYJ",
    "SPA", "BHD", "ADR", "ADS", "THE", "CLASS", "CL", "A", "B", "C", "COM",
    "COMMON", "STOCK", "ORDINARY", "SHARES", "SHARE", "SPONSORED",
    "UNSPONSORED", "DEPOSITARY", "AMERICAN", "REIT", "TRUST", "FUND", "ETF",
    "NEW", "DE", "DEL",
})
# Trailing descriptors dropped to get the short name ("Cerebras Systems" ->
# "Cerebras", "Dell Technologies" -> "Dell").
_DESCRIPTOR_SUFFIXES = frozenset({
    "HOLDINGS", "HOLDING", "GROUP", "TECHNOLOGIES", "TECHNOLOGY", "SYSTEMS",
    "INDUSTRIES", "INTERNATIONAL", "ENTERPRISES", "SOLUTIONS", "PLATFORMS",
    "NETWORKS", "COMMUNICATIONS", "PHARMACEUTICALS", "THERAPEUTICS",
    "BIOSCIENCES", "BIOTECHNOLOGY", "SOFTWARE", "MOTORS", "LABS",
    "LABORATORIES", "ENTERTAINMENT", "FINANCIAL", "BANCORP", "RESOURCES",
    "ENERGY", "MANUFACTURING", "WORLDWIDE", "GLOBAL", "DIGITAL", "COMPUTER",
    "COMPUTING", "SEMICONDUCTOR", "SEMICONDUCTORS", "DEVICES", "BRANDS",
    "PARTNERS", "CAPITAL", "MEDIA", "HEALTHCARE", "HEALTH", "MOBILE",
    "INTERACTIVE", "RENEWABLES", "MINING", "LITHIUM", "POWER",
})
# A single word that is ordinary English (or a place/common name) never
# identifies a company on its own.
_COMMON_SINGLE_WORDS = frozenset({
    "STRATEGY", "TARGET", "UNITY", "BLOCK", "SNAP", "MATCH", "OPEN", "CORE",
    "APPLIED", "ADVANCED", "NATIONAL", "AMERICAN", "FIRST", "UNITED",
    "GENERAL", "GLOBAL", "INTERNATIONAL", "BANK", "CAPITAL", "ENERGY",
    "DIGITAL", "CREATIVE", "OUTDOOR", "SUPER", "HOME", "BEST", "DOLLAR",
    "LIBERTY", "PUBLIC", "SELECT", "SIMPLY", "SMART", "SOUTHERN", "STANDARD",
    "STATE", "TRADE", "TRUE", "UNIVERSAL", "VALUE", "WORLD", "PREMIER",
    "PROGRESSIVE", "SQUARE", "ROOT", "PATH", "SOUND", "VERTICAL", "RANGE",
    "TITAN", "SPIRIT", "FRONTIER", "ALLY", "ARROW", "GUESS", "LIFE", "WESTERN",
    "EASTERN", "NORTHERN", "CENTRAL", "PACIFIC", "ATLANTIC", "MODERN",
    "CLEAR", "FUTURE", "RELIANCE", "PIONEER", "SUMMIT", "TRIUMPH", "KEY",
    "ENTERPRISE", "INNOVATIVE", "INTEGRATED", "INDEPENDENT", "MOBILE",
    "RAPID", "REPUBLIC", "REGIONAL", "ROYAL", "SIGNATURE", "SILVER", "GOLD",
    "STAR", "STERLING", "THRESHOLD", "SONIC", "TERRA", "TRUMP", "POWELL",
    "WOLVERINE", "COHERENT", "CIPHER", "LIFTOFF", "ALPHA", "OMEGA", "DELTA",
    "PRIME", "ELEMENT", "COMPASS", "CHAMPION", "EVOLUTION", "FOCUS",
    "GENESIS", "HARMONY", "HORIZON", "IMPACT", "LEGACY", "MOMENTUM", "ORIGIN",
    "PLANET", "PULSE", "QUANTUM", "RADIUS", "SUMMIT", "VISION", "VITAL",
    "WAVE", "ZOOM", "CHINA", "INDIA", "CANADA", "AMERICA", "TEXAS",
    "NATIONAL", "HUT", "RED", "NU", "MARA", "RIOT", "SIGMA", "VIKING",
    "FOSSIL", "CAPRI", "CREDO", "TOAST", "AFFIRM", "ELASTIC", "OPERA",
    "CHEWY", "CARNIVAL", "DOMINION", "REALTY", "PROGRESS", "CREATIVE",
    "TAKE", "SMITH", "JAMES", "WALT", "DUTCH", "SPACE", "PALO",
})
# Short names that are places or generic phrases.
_AMBIGUOUS_SHORT_NAMES = frozenset({"PALO ALTO", "SPACE EXPLORATION", "CREATIVE MEDIA"})

_WORD_RE = re.compile(r"[A-Z0-9][A-Z0-9&'.\-]*")

# Where the app already keeps company names. Paths are resolved against the
# data directory at call time so the test sandbox (MS_DATA_DIR) is respected.
LOCAL_NAME_FILES: Tuple[str, ...] = (
    "calendar_events_cache.json",
    "scanner_state.json",
    "etoro_instruments_map.json",
    "sec_filings_cache.json",
)


def _clean_name(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text or len(text) > 120:
        return ""
    return re.sub(r"\s+", " ", text)


def _name_words(name: str) -> List[str]:
    upper = str(name or "").upper().replace(",", " ")
    words: List[str] = []
    for raw in _WORD_RE.findall(upper):
        word = raw.strip(".")
        # "Baidu, Inc.-ADR" / "Alibaba-ADR": the listing type is not the name.
        for suffix in ("-ADR", "-ADS"):
            if word.endswith(suffix):
                word = word[: -len(suffix)].strip(".")
        if word:
            words.append(word)
    return words


def _is_legal_suffix(word: str) -> bool:
    return word in _LEGAL_SUFFIXES or word.replace(".", "") in _LEGAL_SUFFIXES


def _is_distinctive_word(word: str) -> bool:
    """A single word that can identify a company on its own."""
    return len(word) >= 4 and word not in _COMMON_SINGLE_WORDS and word not in _DESCRIPTOR_SUFFIXES


def derive_aliases(ticker: str, name: str) -> Tuple[str, ...]:
    """Aliases (upper case) under which a headline may name ``ticker``.

    The full legal-suffix-free name always counts. The short name (without a
    trailing descriptor such as "Holdings"/"Systems") counts when it is still
    distinctive: at least four characters and not an ordinary English word.
    Bundled aliases for the ticker are always included. A one-word alias may
    equal the ticker ("IonQ", "Uber", "SoFi"): the ticker rules only accept
    the all-capitals form, the name is written in mixed case.
    """
    tk = str(ticker or "").upper().strip()
    aliases: List[str] = []

    def _add(value: str) -> None:
        candidate = re.sub(r"\s+", " ", str(value or "").upper()).strip(" .,-")
        if not candidate or candidate in aliases:
            return
        if " " not in candidate and not _is_distinctive_word(candidate):
            return
        if candidate in _AMBIGUOUS_SHORT_NAMES:
            return
        aliases.append(candidate)

    for bundled in BUNDLED_COMPANY_ALIASES.get(tk, ()):
        _add(bundled)

    words = _name_words(name)
    while words and _is_legal_suffix(words[-1]):
        words.pop()
    while words and words[0] == "THE":
        words.pop(0)
    if words:
        _add(" ".join(words))
        short = list(words)
        # Drop trailing descriptors and one/two-character tails ("Cellebrite DI").
        while len(short) > 1 and (short[-1] in _DESCRIPTOR_SUFFIXES or len(short[-1]) <= 2):
            short.pop()
        if short != words:
            _add(" ".join(short))
    return tuple(aliases)


def compile_alias_matcher(aliases: Iterable[str]) -> Optional[re.Pattern]:
    """Whole-word matcher for a set of aliases.

    A multi-word alias is matched case-insensitively. A one-word alias must
    start with a capital letter in the text ("Apple", "NVIDIA", "IonQ" - not
    "apple", not "uber-cheap"): company names are proper nouns.
    """
    parts = sorted({str(a or "").strip() for a in aliases if str(a or "").strip()}, key=len, reverse=True)
    if not parts:
        return None
    alternatives: List[str] = []
    for part in parts:
        if " " in part:
            alternatives.append("(?i:" + re.escape(part).replace(r"\ ", r"\s+") + ")")
        else:
            head, tail = part[0], part[1:]
            alternatives.append(re.escape(head.upper()) + ("(?i:" + re.escape(tail) + ")" if tail else ""))
    body = "|".join(alternatives)
    # The possessive ("Nvidia's") is part of the word.
    return re.compile(rf"(?<![A-Za-z0-9])(?:{body})(?:['’]s)?(?![A-Za-z0-9])")


def _read_json(path: Path) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return None
    except Exception as exc:
        logger.debug("Company-name source unreadable (%s): %s", path.name, exc)
        return None


def _names_from_calendar(payload: Any) -> Dict[str, str]:
    out: Dict[str, str] = {}
    earnings = payload.get("earnings") if isinstance(payload, dict) else None
    if not isinstance(earnings, dict):
        return out
    for symbol, rows in earnings.items():
        for row in rows if isinstance(rows, list) else [rows]:
            if not isinstance(row, dict):
                continue
            name = _clean_name(row.get("company_name") or row.get("name"))
            tk = str(row.get("symbol") or symbol or "").upper().strip()
            if tk and name and name.upper() != tk:
                out.setdefault(tk, name)
                break
    return out


def _names_from_scanner(payload: Any) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not isinstance(payload, dict):
        return out
    for key in ("watchlist_stock_tickers", "hot_tickers"):
        for row in payload.get(key) or []:
            if not isinstance(row, dict):
                continue
            tk = str(row.get("symbol") or "").upper().strip()
            name = _clean_name(row.get("name"))
            if tk and name and name.upper() != tk:
                out.setdefault(tk, name)
    return out


def _names_from_etoro(payload: Any) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not isinstance(payload, dict):
        return out
    for symbol, row in payload.items():
        if not isinstance(row, dict):
            continue
        # The map also lists crypto under symbols that are US tickers
        # ("T" = Threshold Network, "S" = Sonic). Only equities name a company.
        if str(row.get("asset_class") or "").strip().lower() not in {"stocks", "stock", "etf", "etfs"}:
            continue
        tk = str(row.get("symbol") or symbol or "").upper().strip()
        name = _clean_name(row.get("name"))
        if tk and name and name.upper() != tk:
            out.setdefault(tk, name)
    return out


def _names_from_sec(payload: Any) -> Dict[str, str]:
    out: Dict[str, str] = {}

    def _walk(node: Any, depth: int = 0) -> None:
        if depth > 4:
            return
        if isinstance(node, dict):
            tk = str(node.get("ticker") or "").upper().strip()
            name = _clean_name(node.get("company_name"))
            if tk and name and name.upper() != tk:
                out.setdefault(tk, name)
            for value in node.values():
                if isinstance(value, (dict, list)):
                    _walk(value, depth + 1)
        elif isinstance(node, list):
            for value in node[:2000]:
                _walk(value, depth + 1)

    _walk(payload)
    return out


_SOURCE_PARSERS = {
    "calendar_events_cache.json": _names_from_calendar,
    "scanner_state.json": _names_from_scanner,
    "etoro_instruments_map.json": _names_from_etoro,
    "sec_filings_cache.json": _names_from_sec,
}

_LOCAL_CACHE_LOCK = threading.RLock()
_LOCAL_CACHE: Dict[str, Tuple[float, int, Dict[str, str]]] = {}


def load_local_company_names(data_dir: Any) -> Dict[str, str]:
    """ticker -> company name from the app's own on-disk caches.

    Each file is parsed once and re-read only when its mtime/size changes.
    Earlier sources win (earnings calendar first: it lists equities only).
    Never performs a network call.
    """
    merged: Dict[str, str] = {}
    base = Path(data_dir)
    for file_name in LOCAL_NAME_FILES:
        path = base / file_name
        try:
            stat = path.stat()
        except OSError:
            continue
        key = str(path)
        with _LOCAL_CACHE_LOCK:
            cached = _LOCAL_CACHE.get(key)
        if cached and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
            names = cached[2]
        else:
            payload = _read_json(path)
            try:
                names = _SOURCE_PARSERS[file_name](payload) if payload is not None else {}
            except Exception as exc:
                logger.debug("Company-name source %s not usable: %s", file_name, exc)
                names = {}
            with _LOCAL_CACHE_LOCK:
                _LOCAL_CACHE[key] = (stat.st_mtime, stat.st_size, names)
        for tk, name in names.items():
            merged.setdefault(tk, name)
    return merged


def resolve_company_name(ticker: str, local_names: Optional[Dict[str, str]] = None) -> str:
    """Display name for ``ticker`` (local data first, bundled map second)."""
    tk = str(ticker or "").upper().strip()
    if not tk:
        return ""
    name = (local_names or {}).get(tk) or (local_names or {}).get(tk.replace("-", "."))
    if name:
        return str(name)
    bundled = BUNDLED_COMPANY_ALIASES.get(tk) or BUNDLED_COMPANY_ALIASES.get(tk.replace("-", "."))
    return bundled[0] if bundled else ""


_BELLWETHER_MATCHER = compile_alias_matcher(
    alias for tk in BELLWETHER_TICKERS for alias in BUNDLED_COMPANY_ALIASES.get(tk, ())
)


def names_bellwether_company(headline: str) -> bool:
    """True when a headline names one of the largest companies by name."""
    return bool(_BELLWETHER_MATCHER and _BELLWETHER_MATCHER.search(str(headline or "")))
