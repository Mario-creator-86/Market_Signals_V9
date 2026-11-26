"""tests/unit/test_ticker_learning.py
================================================
Pure-function unit tests for ``core/ticker_learning.py::get_ticker_adjustment``.

The module is the source of truth for expected values; the spec
(``docs/superpowers/specs/2026-07-04-ticker-learning-safety-design.md``)
Decision rules tables match the module's _DEFAULTS and branch logic.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
__import__("os").environ.setdefault("TEST_MODE", "1")

from core.ticker_learning import get_ticker_adjustment  # noqa: E402


def _closed_pos(ticker: str, closed_at: datetime, direction: str = "LONG", pnl: float = -1.0) -> dict:
    return {
        "ticker": ticker,
        "status": "CLOSED",
        "closed_at": closed_at.isoformat(),
        "direction": direction,
        "pnl": pnl,
    }


NOW = lambda: datetime.now(timezone.utc)
TKR = "ETH-USD"
CFG = {"scanner_min_score": 78.0, "ticker_learning": {"enabled": True}}


# ─────────────────────────────────────────────────────────────────────────────
# 1. No history -> baseline ALLOW
# ─────────────────────────────────────────────────────────────────────────────

def test_no_history_baseline_allow():
    now = NOW()
    adj = get_ticker_adjustment(TKR, "LONG", [], CFG, now=now)
    assert adj["action"] == "ALLOW"
    assert adj["size_mult"] == 1.0
    assert adj["min_score_required"] == 78.0
    assert adj["leverage_bonus"] == 0
    assert adj["rr_mult"] == 1.0
    assert adj["streak_type"] == "NONE"
    assert adj["streak_len"] == 0
    assert adj["cooldown_until"] is None


# ─────────────────────────────────────────────────────────────────────────────
# 2. Three consecutive losses -> REDUCE size 0.5, bar 90
# ─────────────────────────────────────────────────────────────────────────────

def test_three_losses_reduce():
    now = NOW()
    positions = [
        _closed_pos(TKR, now - timedelta(hours=3), pnl=-1.0),
        _closed_pos(TKR, now - timedelta(hours=2), pnl=-2.0),
        _closed_pos(TKR, now - timedelta(hours=1), pnl=-1.0),
    ]
    adj = get_ticker_adjustment(TKR, "LONG", positions, CFG, now=now)
    assert adj["action"] == "REDUCE"
    assert adj["size_mult"] == 0.5
    assert adj["min_score_required"] == 90.0
    assert adj["streak_type"] == "LOSS"
    assert adj["streak_len"] == 3


# ─────────────────────────────────────────────────────────────────────────────
# 3. Four consecutive losses -> REDUCE size 0.25, bar 93
# ─────────────────────────────────────────────────────────────────────────────

def test_four_losses_tighten():
    now = NOW()
    positions = [
        _closed_pos(TKR, now - timedelta(hours=4), pnl=-1.0),
        _closed_pos(TKR, now - timedelta(hours=3), pnl=-1.0),
        _closed_pos(TKR, now - timedelta(hours=2), pnl=-1.0),
        _closed_pos(TKR, now - timedelta(hours=1), pnl=-1.0),
    ]
    adj = get_ticker_adjustment(TKR, "LONG", positions, CFG, now=now)
    assert adj["action"] == "REDUCE"
    assert adj["size_mult"] == 0.25
    assert adj["min_score_required"] == 93.0
    assert adj["streak_len"] == 4


# ─────────────────────────────────────────────────────────────────────────────
# 4. Five losses, last loss 1h ago -> BLOCK within cooldown
# ─────────────────────────────────────────────────────────────────────────────

def test_five_losses_block_within_cooldown():
    now = NOW()
    last = now - timedelta(hours=1)
    positions = [
        _closed_pos(TKR, now - timedelta(hours=5), pnl=-1.0),
        _closed_pos(TKR, now - timedelta(hours=4), pnl=-1.0),
        _closed_pos(TKR, now - timedelta(hours=3), pnl=-1.0),
        _closed_pos(TKR, now - timedelta(hours=2), pnl=-1.0),
        _closed_pos(TKR, last, pnl=-1.0),
    ]
    adj = get_ticker_adjustment(TKR, "LONG", positions, CFG, now=now)
    assert adj["action"] == "BLOCK"
    assert adj["streak_len"] == 5
    assert adj["cooldown_until"] is not None
    assert "blocked" in adj["reason"].lower()


# ─────────────────────────────────────────────────────────────────────────────
# 5. Five losses, last loss 7h ago (cooldown 6h expired) -> probation REDUCE
# ─────────────────────────────────────────────────────────────────────────────

def test_five_losses_probation_after_cooldown():
    now = NOW()
    last = now - timedelta(hours=7)
    positions = [
        _closed_pos(TKR, now - timedelta(hours=11), pnl=-1.0),
        _closed_pos(TKR, now - timedelta(hours=10), pnl=-1.0),
        _closed_pos(TKR, now - timedelta(hours=9), pnl=-1.0),
        _closed_pos(TKR, now - timedelta(hours=8), pnl=-1.0),
        _closed_pos(TKR, last, pnl=-1.0),
    ]
    adj = get_ticker_adjustment(TKR, "LONG", positions, CFG, now=now)
    assert adj["action"] == "REDUCE"
    assert adj["size_mult"] == 0.25
    assert adj["min_score_required"] == 93.0
    # cooldown_until is set but in the past
    assert adj["cooldown_until"] is not None
    cd = datetime.fromisoformat(adj["cooldown_until"])
    assert cd <= now


# ─────────────────────────────────────────────────────────────────────────────
# 6. Three consecutive wins -> rr 1.25, leverage_bonus 1
# ─────────────────────────────────────────────────────────────────────────────

def test_three_wins_boost():
    now = NOW()
    positions = [
        _closed_pos(TKR, now - timedelta(hours=3), pnl=1.5),
        _closed_pos(TKR, now - timedelta(hours=2), pnl=2.0),
        _closed_pos(TKR, now - timedelta(hours=1), pnl=1.0),
    ]
    adj = get_ticker_adjustment(TKR, "LONG", positions, CFG, now=now)
    assert adj["action"] == "ALLOW"
    assert adj["rr_mult"] == 1.25
    assert adj["leverage_bonus"] == 1
    assert adj["streak_type"] == "WIN"
    assert adj["streak_len"] == 3


# ─────────────────────────────────────────────────────────────────────────────
# 7. Five consecutive wins -> rr 1.5, leverage_bonus 2
# ─────────────────────────────────────────────────────────────────────────────

def test_five_wins_boost2():
    now = NOW()
    positions = [
        _closed_pos(TKR, now - timedelta(hours=5), pnl=1.0),
        _closed_pos(TKR, now - timedelta(hours=4), pnl=1.0),
        _closed_pos(TKR, now - timedelta(hours=3), pnl=1.0),
        _closed_pos(TKR, now - timedelta(hours=2), pnl=1.0),
        _closed_pos(TKR, now - timedelta(hours=1), pnl=1.0),
    ]
    adj = get_ticker_adjustment(TKR, "LONG", positions, CFG, now=now)
    assert adj["rr_mult"] == 1.5
    assert adj["leverage_bonus"] == 2


# ─────────────────────────────────────────────────────────────────────────────
# 8. A win after losses resets the streak -> streak WIN len 1
# ─────────────────────────────────────────────────────────────────────────────

def test_win_after_losses_resets_streak():
    now = NOW()
    # Oldest -> newest: L, L, L, W  => most-recent-first: W, L, L, L
    positions = [
        _closed_pos(TKR, now - timedelta(hours=4), pnl=-1.0),
        _closed_pos(TKR, now - timedelta(hours=3), pnl=-1.0),
        _closed_pos(TKR, now - timedelta(hours=2), pnl=-1.0),
        _closed_pos(TKR, now - timedelta(hours=1), pnl=1.0),
    ]
    adj = get_ticker_adjustment(TKR, "LONG", positions, CFG, now=now)
    assert adj["streak_type"] == "WIN"
    assert adj["streak_len"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# 9. SHORT is symmetric to LONG (3 losses -> REDUCE)
# ─────────────────────────────────────────────────────────────────────────────

def test_short_symmetric():
    now = NOW()
    positions = [
        _closed_pos(TKR, now - timedelta(hours=3), direction="SHORT", pnl=-1.0),
        _closed_pos(TKR, now - timedelta(hours=2), direction="SHORT", pnl=-1.0),
        _closed_pos(TKR, now - timedelta(hours=1), direction="SHORT", pnl=-1.0),
    ]
    adj = get_ticker_adjustment(TKR, "SHORT", positions, CFG, now=now)
    assert adj["action"] == "REDUCE"
    assert adj["size_mult"] == 0.5
    assert adj["min_score_required"] == 90.0
    assert adj["streak_len"] == 3


# ─────────────────────────────────────────────────────────────────────────────
# 10. enabled: False -> baseline ALLOW regardless of history
# ─────────────────────────────────────────────────────────────────────────────

def test_enabled_false_baseline():
    now = NOW()
    positions = [
        _closed_pos(TKR, now - timedelta(hours=5), pnl=-1.0),
        _closed_pos(TKR, now - timedelta(hours=4), pnl=-1.0),
        _closed_pos(TKR, now - timedelta(hours=3), pnl=-1.0),
        _closed_pos(TKR, now - timedelta(hours=2), pnl=-1.0),
        _closed_pos(TKR, now - timedelta(hours=1), pnl=-1.0),
    ]
    cfg = {"scanner_min_score": 78.0, "ticker_learning": {"enabled": False}}
    adj = get_ticker_adjustment(TKR, "LONG", positions, cfg, now=now)
    assert adj["action"] == "ALLOW"
    assert adj["size_mult"] == 1.0
    assert adj["streak_type"] == "NONE"
    assert adj["streak_len"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# 11. Unknown direction -> baseline ALLOW (no crash)
# ─────────────────────────────────────────────────────────────────────────────

def test_unknown_direction_baseline():
    now = NOW()
    positions = [
        _closed_pos(TKR, now - timedelta(hours=3), pnl=-1.0),
        _closed_pos(TKR, now - timedelta(hours=2), pnl=-1.0),
        _closed_pos(TKR, now - timedelta(hours=1), pnl=-1.0),
    ]
    adj = get_ticker_adjustment(TKR, "HOLD", positions, CFG, now=now)
    assert adj["action"] == "ALLOW"
    assert adj["streak_type"] == "NONE"
    assert adj["streak_len"] == 0

    # Empty string also baseline
    adj2 = get_ticker_adjustment(TKR, "", positions, CFG, now=now)
    assert adj2["action"] == "ALLOW"


# ─────────────────────────────────────────────────────────────────────────────
# 12. BUY normalised to LONG (3 losses on BUY direction -> REDUCE)
# ─────────────────────────────────────────────────────────────────────────────

def test_normalisation_buy_long():
    now = NOW()
    positions = [
        _closed_pos(TKR, now - timedelta(hours=3), direction="LONG", pnl=-1.0),
        _closed_pos(TKR, now - timedelta(hours=2), direction="LONG", pnl=-1.0),
        _closed_pos(TKR, now - timedelta(hours=1), direction="LONG", pnl=-1.0),
    ]
    adj = get_ticker_adjustment(TKR, "BUY", positions, CFG, now=now)
    assert adj["action"] == "REDUCE"
    assert adj["size_mult"] == 0.5
    assert adj["min_score_required"] == 90.0


# ─────────────────────────────────────────────────────────────────────────────
# 13. Breakeven (pnl == 0.0) is ignored -- not counted in the streak
# ─────────────────────────────────────────────────────────────────────────────

def test_breakeven_ignored():
    now = NOW()
    # Oldest -> newest: L, L, BE, L  => BE skipped; streak from most-recent: L, L, L -> len 3
    positions = [
        _closed_pos(TKR, now - timedelta(hours=4), pnl=-1.0),
        _closed_pos(TKR, now - timedelta(hours=3), pnl=-1.0),
        _closed_pos(TKR, now - timedelta(hours=2), pnl=0.0),
        _closed_pos(TKR, now - timedelta(hours=1), pnl=-1.0),
    ]
    adj = get_ticker_adjustment(TKR, "LONG", positions, CFG, now=now)
    assert adj["streak_type"] == "LOSS"
    assert adj["streak_len"] == 3
    assert adj["action"] == "REDUCE"
    assert adj["size_mult"] == 0.5
    assert adj["min_score_required"] == 90.0