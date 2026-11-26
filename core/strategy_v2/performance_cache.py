from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from core.config import DATA_DIR


STRATEGY_PERF_CACHE_PATH = DATA_DIR / "strategy_performance_cache.json"
# Research/proxy measurements must never overwrite the cache consumed by the
# live V2 selector and Capital gate.  The two stores deliberately share a
# schema for diagnostics but have different trust levels.
STRATEGY_RESEARCH_CACHE_PATH = DATA_DIR / "strategy_research_cache.json"

# Runda-6 D4: process-wide lock for the read-modify-save writers. Pre-fix
# upsert_entry / upsert_from_trade_result did load() -> mutate -> save() with NO
# lock, so two concurrent trade closes both read the same `existing`, both
# compute new rolling metrics, and the second save clobbered the first (lost
# update -- a closed trade silently dropped from win_rate / profit_factor /
# the equity curve). RLock (reentrant) so upsert_from_trade_result can hold the
# lock across its get_entry read AND the nested upsert_entry write without
# deadlocking. Reads (get_entry / get_symbol_entries) are not locked -- atomic
# os.replace writes mean no torn reads, and the selector's per-select snapshot
# (D2) already makes a single select() atomic.
_CACHE_LOCK = threading.RLock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json_atomic(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Optional[Path] = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=str(path.parent))
        temp_path = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        import time
        for attempt in range(5):
            try:
                os.replace(str(temp_path), str(path))
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.1)
    finally:
        if temp_path and temp_path.exists():
            try:
                temp_path.unlink()
            except Exception:
                pass


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def build_perf_key(symbol: str, asset_class: str, strategy_id: str, direction: str) -> str:
    return "|".join(
        [
            str(symbol or "").upper().strip(),
            str(asset_class or "").upper().strip(),
            str(strategy_id or "").upper().strip(),
            str(direction or "").upper().strip(),
        ]
    )


class StrategyPerformanceCache:
    def __init__(self, path: Path = STRATEGY_PERF_CACHE_PATH):
        self.path = path

    def load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {"updated_at": None, "entries": {}}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                payload = json.load(f) or {}
            entries = payload.get("entries", {})
            if not isinstance(entries, dict):
                entries = {}
            return {"updated_at": payload.get("updated_at"), "entries": entries}
        except Exception:
            return {"updated_at": None, "entries": {}}

    def save(self, payload: Dict[str, Any]) -> None:
        data = {
            "updated_at": _now_iso(),
            "entries": payload.get("entries", {}) if isinstance(payload.get("entries"), dict) else {},
        }
        _write_json_atomic(self.path, data)

    def get_entry(self, symbol: str, asset_class: str, strategy_id: str, direction: str) -> Dict[str, Any]:
        payload = self.load()
        entries = payload.get("entries", {})
        key = build_perf_key(symbol, asset_class, strategy_id, direction)
        raw = entries.get(key, {})
        if not isinstance(raw, dict):
            return {}
        return dict(raw)

    def get_symbol_entries(self, symbol: str) -> Dict[str, Dict[str, Any]]:
        symbol_up = str(symbol or "").upper().strip()
        payload = self.load()
        entries = payload.get("entries", {})
        out: Dict[str, Dict[str, Any]] = {}
        for key, value in entries.items():
            if not isinstance(value, dict):
                continue
            if str(key).upper().startswith(f"{symbol_up}|"):
                out[str(key)] = value
        return out

    def upsert_entry(
        self,
        symbol: str,
        asset_class: str,
        strategy_id: str,
        direction: str,
        metrics: Dict[str, Any],
    ) -> None:
        with _CACHE_LOCK:
            payload = self.load()
            entries = payload.get("entries", {})
            if not isinstance(entries, dict):
                entries = {}
            key = build_perf_key(symbol, asset_class, strategy_id, direction)
            existing = entries.get(key, {})
            existing = existing if isinstance(existing, dict) else {}

            def value(name: str, default: Any = None) -> Any:
                return metrics[name] if name in metrics else existing.get(name, default)

            raw_profit_factor = value("profit_factor")
            clean = {
                "win_rate": _safe_float(value("win_rate"), 0.0),
                "profit_factor": _safe_float(raw_profit_factor, 0.0) if raw_profit_factor is not None else None,
                "profit_factor_available": raw_profit_factor is not None,
                "expectancy_R": _safe_float(value("expectancy_R"), 0.0),
                "avg_R": _safe_float(value("avg_R"), 0.0),
                "max_drawdown": _safe_float(value("max_drawdown"), 0.0),
                "sample_size": _safe_int(value("sample_size"), 0),
                "last_run": metrics.get("last_run") or _now_iso(),
                "costs_used": value("costs_used", {}),
                "walk_forward": value("walk_forward", {}),
                # Validation provenance is deliberately persisted separately from
                # rolling live-trade metrics.  A positive number without a
                # reproducible out-of-sample method is not evidence that the V2
                # selector may execute a strategy.
                "validation_status": str(value("validation_status") or "").strip(),
                "validation_method": str(value("validation_method") or "").strip(),
                "validation_reason": str(value("validation_reason") or "").strip(),
                "validation_as_of": value("validation_as_of"),
                # These selector controls were previously stripped by the
                # allowlist, making feedback_loop's degradation/suspension
                # decisions disappear immediately after they were written.
                "degraded": bool(value("degraded", False)),
                "suspended": bool(value("suspended", False)),
            }
            # Whether expectancy_R/avg_R reflect costs ("net") or only the raw
            # price move ("gross_price_move"). Absent on entries that never
            # recorded it, so nothing is claimed for them.
            if "expectancy_basis" in metrics or "expectancy_basis" in existing:
                clean["expectancy_basis"] = str(value("expectancy_basis") or "")
            if "_gross_profit_accum" in metrics or "_gross_profit_accum" in existing:
                clean["_gross_profit_accum"] = _safe_float(value("_gross_profit_accum"), 0.0)
            if "_gross_loss_accum" in metrics or "_gross_loss_accum" in existing:
                clean["_gross_loss_accum"] = _safe_float(value("_gross_loss_accum"), 0.0)
            if "_wins_accum" in metrics or "_wins_accum" in existing:
                clean["_wins_accum"] = _safe_int(value("_wins_accum"), 0)
            if "_losses_accum" in metrics or "_losses_accum" in existing:
                clean["_losses_accum"] = _safe_int(value("_losses_accum"), 0)
            # Equity-curve accumulators for the peak-to-trough max_drawdown (M7).
            # Without these persisted, old_equity/old_peak always read 0 and the
            # drawdown collapses back to worst-single-loss across calls.
            if "_equity_accum" in metrics or "_equity_accum" in existing:
                clean["_equity_accum"] = _safe_float(value("_equity_accum"), 0.0)
            if "_peak_accum" in metrics or "_peak_accum" in existing:
                clean["_peak_accum"] = _safe_float(value("_peak_accum"), 0.0)
            entries[key] = clean
            self.save({"entries": entries})

    @staticmethod
    def is_fresh(entry: Dict[str, Any], ttl_seconds: int) -> bool:
        if not isinstance(entry, dict):
            return False
        if ttl_seconds <= 0:
            return False
        validation_status = str(entry.get("validation_status") or "").strip().lower()
        timestamp = (
            entry.get("validation_as_of")
            if validation_status == "validated" and entry.get("validation_as_of")
            else entry.get("last_run")
        )
        timestamp = str(timestamp or "").strip()
        if not timestamp:
            return False
        try:
            dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - dt).total_seconds()
            return age <= ttl_seconds
        except Exception:
            return False

    @staticmethod
    def _net_pct_of_notional(closed_trade: dict, net_pnl: float, gross_pct: float) -> tuple:
        """Return (percent of notional, basis) for one closed trade.

        ``basis`` is "net" when costs are reflected and "gross_price_move" when
        the caller supplied nothing but the raw price move.
        """
        explicit = closed_trade.get("net_pnl_pct_notional")
        if explicit is not None:
            return _safe_float(explicit, gross_pct), "net"
        notional = _safe_float(closed_trade.get("notional"), 0.0)
        if notional <= 0:
            entry_price = _safe_float(closed_trade.get("entry_price"), 0.0)
            quantity = abs(_safe_float(closed_trade.get("quantity", closed_trade.get("qty")), 0.0))
            notional = entry_price * quantity
        if notional > 0:
            return (net_pnl / notional) * 100.0, "net"
        gross_pnl = _safe_float(closed_trade.get("gross_pnl"), 0.0)
        # gross_pnl = notional * gross_pct / 100, so the notional can be
        # recovered when both are non-zero and agree in sign.
        if gross_pnl != 0.0 and gross_pct != 0.0 and (gross_pnl > 0) == (gross_pct > 0):
            return net_pnl * gross_pct / gross_pnl, "net"
        return gross_pct, "gross_price_move"

    def upsert_from_trade_result(self, closed_trade: dict) -> None:
        """Update performance cache from a closed trade's outcome.

        Computes rolling win_rate, profit_factor, expectancy_R, avg_R,
        max_drawdown, and sample_size, then calls upsert_entry().
        """
        ticker = str(closed_trade.get("ticker") or "").upper().strip()
        strategy_type = str(closed_trade.get("strategy_type") or "").upper().strip()
        direction = str(closed_trade.get("direction") or "LONG").upper().strip()
        asset_class = str(closed_trade.get("asset_class") or "US_STOCKS").upper().strip()

        if not ticker or not strategy_type:
            return

        pnl = _safe_float(closed_trade.get("net_pnl") or closed_trade.get("pnl"), 0.0)
        pnl_pct = _safe_float(closed_trade.get("pnl_pct_notional"), 0.0)

        # Runda-6 D4: hold _CACHE_LOCK across the WHOLE read-modify-write so two
        # concurrent closes can't both read the same `existing` and clobber each
        # other's save (lost update -- a trade silently dropped from win_rate /
        # profit_factor / the equity curve). RLock reentrant -> the nested
        # upsert_entry (which also acquires _CACHE_LOCK) does not deadlock.
        with _CACHE_LOCK:
            # Get existing entry
            existing = self.get_entry(ticker, asset_class, strategy_type, direction)
            n = _safe_int(existing.get("sample_size"), 0)
            old_wr = _safe_float(existing.get("win_rate"), 0.0)
            old_avg_r = _safe_float(existing.get("avg_R"), 0.0)
            old_dd = _safe_float(existing.get("max_drawdown"), 0.0)
            # Running equity curve for a true peak-to-trough drawdown (mirror
            # strategy_backtester.py:394-406 which sums net_pct into cumul/peak).
            # Pre-fix the rolling path set max_drawdown = max(old_dd, |single-loss
            # pnl_pct|) -- the WORST SINGLE TRADE, not a drawdown -- so clustered
            # losses understated risk, inflated validation_score (dd_penalty too
            # small) and dodged the drawdown_high rejection (selector.py:98-101).
            # Absent on old cache entries -> 0.0 migration fallback (fresh curve).
            old_equity = _safe_float(existing.get("_equity_accum"), 0.0)
            old_peak = _safe_float(existing.get("_peak_accum"), 0.0)
            old_gross_profit = _safe_float(existing.get("_gross_profit_accum"), 0.0)
            old_gross_loss = _safe_float(existing.get("_gross_loss_accum"), 0.0)
            old_wins = _safe_int(existing.get("_wins_accum"), int(round(old_wr * n)))
            old_losses = _safe_int(existing.get("_losses_accum"), max(0, n - old_wins))

            # Win/loss classification (L1): a breakeven trade (pnl == 0) is
            # NEITHER a win nor a loss. Pre-fix `is_win = pnl > 0` made breakeven
            # count as a loss (losses += 1), deflating win_rate while
            # profit_factor was unaffected (gross_loss += abs(0) = 0) -- an
            # inconsistency between the two metrics. Now win_rate is over DECIDED
            # trades (wins + losses); n still counts every closed trade for
            # sample_size / avg_r rolling.
            is_win = pnl > 0
            is_loss = pnl < 0
            n += 1
            wins = old_wins + (1 if is_win else 0)
            losses = old_losses + (1 if is_loss else 0)

            # Rolling win_rate (over decided trades; breakeven excluded).
            decided = wins + losses
            new_wr = wins / decided if decided > 0 else 0.0

            gross_profit = old_gross_profit + (pnl if is_win else 0.0)
            gross_loss = old_gross_loss + (abs(pnl) if is_loss else 0.0)
            new_pf = gross_profit / gross_loss if gross_loss > 0 else None

            # Rolling avg_R (approximate from pnl_pct / risk%).
            # `pnl_pct_notional` is the raw price move (before commissions),
            # while win/loss and profit factor above are decided on NET pnl. A
            # trade with a small gross gain and a net loss was therefore booked
            # as a loss with a positive R, inflating expectancy_R. Restate the
            # percentage net of costs whenever the caller gives enough to do so
            # (an explicit net percentage, the notional, or the gross pnl).
            net_pct, expectancy_basis = self._net_pct_of_notional(closed_trade, pnl, pnl_pct)
            pnl_pct = net_pct
            if n > 1:
                # Earlier trades without a recorded basis were gross.
                previous_basis = str(existing.get("expectancy_basis") or "gross_price_move")
                if previous_basis != expectancy_basis:
                    expectancy_basis = "mixed"
            risk_pct = abs(_safe_float(closed_trade.get("stop_loss_pct"), 2.0))
            r_multiple = pnl_pct / risk_pct if risk_pct > 0 else 0.0
            new_avg_r = (old_avg_r * (n - 1) + r_multiple) / n

            # Rolling max_drawdown: peak-to-trough on the running equity curve (sum
            # of per-trade pnl_pct), in absolute %-points to match the backtester's
            # `abs(cumul - peak)` semantics. Pre-fix this was worst-single-loss.
            new_equity = old_equity + pnl_pct
            new_peak = max(old_peak, new_equity)
            dd_now = max(0.0, new_peak - new_equity)
            new_dd = max(old_dd, dd_now)

            metrics = {
                "win_rate": round(new_wr, 4),
                "profit_factor": round(new_pf, 4) if new_pf is not None else None,
                "expectancy_R": round(new_avg_r, 4),
                "avg_R": round(new_avg_r, 4),
                "expectancy_basis": expectancy_basis,
                "max_drawdown": round(new_dd, 4),
                "sample_size": n,
                "_gross_profit_accum": round(gross_profit, 6),
                "_gross_loss_accum": round(gross_loss, 6),
                "_wins_accum": wins,
                "_losses_accum": losses,
                "_equity_accum": round(new_equity, 6),
                "_peak_accum": round(new_peak, 6),
            }
            self.upsert_entry(ticker, asset_class, strategy_type, direction, metrics)


def calculate_validation_score(
    perf: Dict[str, Any],
    target_win_rate: float = 0.60,
    min_sample: int = 30,
) -> float:
    # Cold-start neutrality: with no trades yet, validation must be NEUTRAL
    # (50), not punitive (0). Pre-fix, sample==0 collapsed the score to 0
    # (wr=0, pf=0, exp_score=50, sample_penalty=min(45,30*1.5)=45 ->
    # max(0, 0.30*50 - 45) = 0). validation carries 25% of the V2
    # total_score (selector.py:59-64), so a 0 caps total_score at
    # 0.45*setup + 0.15*catalyst + 0.15*risk = 75 even with EVERY other
    # component at 100 — i.e. no cold V2 candidate can clear min_edge_score
    # (default 75, engine.py:2496), so V2 NEVER fires on a cold performance
    # cache. A strategy that never fires can never accrue the trades it
    # needs to compute a real validation_score -> self-defeating deadlock
    # (V2 layer inert from a fresh start, directly contra "axează-te pe
    # semnale / integră AI în decizii").
    #
    # Neutral 50 mirrors the news-unknown->neutral fix
    # (compute_direction_bias_score / _news_catalyst_score, finding #4):
    # absence of evidence is not negative evidence. Once trades accrue
    # (sample>0), the wr/exp/pf/sample-penalty math below takes over and
    # validation becomes evidence-driven (a real 60% win-rate still scores
    # ~100 wr_score; cold neutrality does not inflate proven performance).
    #
    # Win-rate justification (CLAUDE.md §4): this changes WHICH signals fire
    # only in the cold regime — pre-fix V2 emitted nothing (deadlock), post-fix
    # V2 candidates compete with V1 on setup/catalyst/risk merit while
    # validation is neutral. It does NOT inflate any proven win-rate metric
    # (sample>0 path unchanged); it unblocks the V2 feedback loop so real
    # performance data can accumulate. Net: more signal surface, no fabricated
    # edge.
    if not isinstance(perf, dict):
        return 50.0

    sample = _safe_int(perf.get("sample_size"), 0)
    if sample == 0:
        return 50.0

    wr = _safe_float(perf.get("win_rate"), 0.0)
    if wr > 1.0:
        wr /= 100.0
    wr_score = max(0.0, min(100.0, (wr / max(target_win_rate, 0.01)) * 100.0))

    exp_r = _safe_float(perf.get("expectancy_R"), 0.0)
    exp_score = max(0.0, min(100.0, 50.0 + (exp_r * 100.0)))

    pf = _safe_float(perf.get("profit_factor"), 0.0)
    pf_score = max(0.0, min(100.0, (pf / 2.0) * 100.0))

    dd = _safe_float(perf.get("max_drawdown"), 0.0)
    dd_penalty = max(0.0, min(60.0, dd * 1.2))

    sample = _safe_int(perf.get("sample_size"), 0)
    sample_penalty = 0.0
    if sample < min_sample:
        sample_penalty = min(45.0, (min_sample - sample) * 1.5)

    score = (0.45 * wr_score) + (0.30 * exp_score) + (0.25 * pf_score)
    score = score - dd_penalty - sample_penalty
    score = max(0.0, min(100.0, score))
    # Thin-sample neutrality (H2): a strategy with 0 < sample < min_sample
    # must NOT dip below the cold-start neutral (50). Pre-fix, a single
    # losing trade (sample=1) collapsed the score to 0 (wr_score=0,
    # exp_score=0, pf_score=0, dd_penalty>0, sample_penalty=min(45,29*1.5)=43.5
    # -> max(0, 0-0-43.5)=0 < 50), permanently deadlocking the strategy
    # (validation=0 < min_edge_score) so it could never accrue the trades
    # needed to compute a real validation_score -- defeating the Runda-4
    # cold-start (sample==0 -> 50) fix. Absence of SUFFICIENT evidence is not
    # negative evidence; once sample >= min_sample the real math above
    # governs (evidence-driven). Win-rate-neutral in the proven regime
    # (sample >= min_sample path unchanged).
    if 0 < sample < min_sample:
        score = max(50.0, score)
    return score
