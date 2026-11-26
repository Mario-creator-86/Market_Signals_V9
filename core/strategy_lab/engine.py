"""Strategy Lab engine: describe a rule as data, backtest it honestly, search a rule space.

A rule is a small JSON-able spec, never code, so that it can be proposed by a
model, stored, compared and executed live without running anything a model
wrote:

    {"family": "xs_rank", "metric": "return", "lookback": 14, "skip": 0,
     "direction": "follow", "fraction": 5, "hold": 7, "sides": "both",
     "max_names": 0, "gate": {"kind": "none"}}

Every ``hold`` days the liquid instruments are ranked by ``metric`` over
``lookback`` days (ignoring the last ``skip`` days). ``direction`` "follow"
buys the top group and shorts the bottom group, "against" does the reverse.
``sides`` keeps both legs or one. ``max_names`` (0 = no cap) keeps only the most
extreme names of each group, so that the backtest holds what a small paper
account can really hold. ``gate`` can switch the rule off, or to one side,
depending on the benchmark's own trend.

Honesty rules of the backtest:
- the ranking of day i uses closes up to day i; the members are entered at that
  close (crypto trades around the clock) or, where the market is closed
  overnight, one close later (``entry_lag``), so no overnight gap is captured;
- the backtest holds what the live rule holds: every member is bought or
  shorted once, in equal amounts, kept at a fixed quantity (no daily
  re-balancing, so a short that keeps rising costs its full compounded move)
  and closed at the next ranking, when the new members are entered;
- costs are charged for entering every member and again for closing it;
- an instrument can be picked at a close only on what was known at that close:
  traded value of the seven days ending there, the price as it really traded
  (not a price restated by later splits) and enough history (a fresh listing
  is not a normal market); the live list is chosen with exactly the same test;
- on perpetuals the funding payments are part of the result: a long pays a
  positive rate, a short receives it; a stock short pays a borrow fee every day;
- a rule is judged on what it earns beyond the average instrument (alpha), so
  that "short everything" does not pass just because the market fell;
- a rule must hold in the first part of the history AND in the last part. Both
  parts are used to choose among many rules, so the last part is a second
  screen, not an untouched test: the only untouched test is paper trading;
- the edge (alpha) is measured on the average over every possible re-ranking
  day, so that one lucky weekday does not carry it; Sharpe, total return and
  drawdown are those of a single schedule, because that is what is traded
  (median of the schedules, and the worst drawdown among them);
- days on which a rule holds nothing count as days without return.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

METRICS = ("return", "volatility", "high_proximity", "volume_trend")
DIRECTIONS = ("follow", "against")
SIDES = ("both", "long", "short")
GATES = ("none", "bench_above_sma", "bench_below_sma", "bench_trend_sides")
LOOKBACKS = (3, 5, 7, 10, 14, 21, 28, 42, 63, 126)
HOLDS = (1, 3, 5, 7, 14)
FRACTIONS = (3, 5, 10)
GATE_WINDOWS = (20, 50, 100)

PASS_SHARPE = 1.0
PASS_T = 2.0
PASS_HOLDOUT_SHARPE = 0.7
PASS_HOLDOUT_T = 1.0
PASS_QUARTERS = 0.6
PASS_MAX_DRAWDOWN_PCT = -40.0
TRAIN_SHARE = 0.6


@dataclass
class Panel:
    """Daily closes and traded value of one market. Rows = days, columns = instruments."""

    market: str
    days: np.ndarray            # days since 1970-01-01
    symbols: List[str]
    close: np.ndarray
    dollar_volume: np.ndarray
    benchmark: str              # symbol whose trend the gates read
    cost: float                 # per unit of turnover
    min_dollar_volume: float
    min_price: float = 0.0
    min_history_days: int = 0
    carry: Optional[np.ndarray] = None      # daily cost paid by a long and received by a short (funding)
    entry_lag: int = 0                      # closes between the ranking and the entry (1 = the market is closed overnight)
    borrow_daily: float = 0.0               # fee a short pays per day on the position's value (stock borrow)
    price_floor: Optional[np.ndarray] = None    # the close as it really traded (not restated by later splits)
    rank_benchmark: bool = True             # False when the benchmark is not an instrument of the market (an index ETF)

    def __post_init__(self) -> None:
        self.close = np.asarray(self.close, dtype=float)
        self.dollar_volume = np.asarray(self.dollar_volume, dtype=float)
        n, m = self.close.shape
        if self.carry is not None:
            self.carry = np.nan_to_num(np.asarray(self.carry, dtype=float))
        # A restated price says nothing about what the instrument cost on the day: the price floor needs the real one.
        traded = self.close if self.price_floor is None else np.asarray(self.price_floor, dtype=float)
        traded = np.where(np.isfinite(traded), traded, self.close)
        self.bench = self.symbols.index(self.benchmark) if self.benchmark in self.symbols else -1
        with np.errstate(invalid="ignore", divide="ignore"):
            self.ret = np.full((n, m), np.nan)
            self.ret[1:] = self.close[1:] / self.close[:-1] - 1
            csum = np.nancumsum(np.nan_to_num(self.dollar_volume), axis=0)
            through = np.full((n, m), np.nan)             # average traded value of the 7 days ending at row i
            if n >= 7:
                through[6:] = (csum[6:] - np.vstack([np.zeros((1, m)), csum[:-7]])) / 7.0
        # May be picked at the close of row i: everything here is known at that close.
        eligible = (through >= self.min_dollar_volume) & np.isfinite(self.close)
        if self.min_price > 0:
            eligible &= traded >= self.min_price
        if self.min_history_days > 0:
            k = int(self.min_history_days)
            seasoned = np.zeros((n, m), dtype=bool)
            if n >= k:
                seasoned[k - 1:] = np.isfinite(self.close[:n - k + 1])
            eligible &= seasoned
        if not self.rank_benchmark and self.bench >= 0:
            eligible[:, self.bench] = False
        self.eligible = eligible
        # Counted in the market's average return of day i: eligible at the close before, and it has a bar on day i.
        self.liquid = np.zeros((n, m), dtype=bool)
        self.liquid[1:] = eligible[:-1] & np.isfinite(self.close[1:])
        # Last known price, for the value of a position on a day without a bar.
        filled = self.close.copy()
        for i in range(1, n):
            gap = ~np.isfinite(filled[i])
            filled[i, gap] = filled[i - 1, gap]
        self.filled = filled

    @property
    def shape(self) -> Tuple[int, int]:
        return self.close.shape

    def truncated(self, back: int) -> "Panel":
        """The same panel as it stood ``back`` bars ago."""
        back = int(back)
        if back <= 0:
            return self
        return Panel(self.market, self.days[:-back], self.symbols, self.close[:-back], self.dollar_volume[:-back],
                     self.benchmark, self.cost, self.min_dollar_volume, self.min_price, self.min_history_days,
                     None if self.carry is None else self.carry[:-back], self.entry_lag, self.borrow_daily,
                     None if self.price_floor is None else np.asarray(self.price_floor)[:-back], self.rank_benchmark)


# ------------------------------------------------------------------ specs
def normalize_spec(raw: Any) -> Optional[Dict[str, Any]]:
    """A validated copy of a rule spec, or None when it is not a rule this engine can run."""
    if not isinstance(raw, dict) or raw.get("family", "xs_rank") != "xs_rank":
        return None
    try:
        metric = str(raw.get("metric", "return"))
        direction = str(raw.get("direction", "follow"))
        sides = str(raw.get("sides", "both"))
        lookback, skip = int(raw.get("lookback", 14)), int(raw.get("skip", 0))
        fraction, hold = int(raw.get("fraction", 5)), int(raw.get("hold", 7))
        max_names = int(raw.get("max_names", 0) or 0)
        gate_raw = raw.get("gate") if isinstance(raw.get("gate"), dict) else {}
        gate_kind = str(gate_raw.get("kind", "none"))
        gate_n = int(gate_raw.get("n", 50))
    except (TypeError, ValueError):
        return None
    if metric not in METRICS or direction not in DIRECTIONS or sides not in SIDES or gate_kind not in GATES:
        return None
    if not (2 <= lookback <= 260 and 0 <= skip <= 10 and skip < lookback and 2 <= fraction <= 20 and 1 <= hold <= 30):
        return None
    if not 5 <= gate_n <= 260 or not 0 <= max_names <= 50:
        return None
    spec = {"family": "xs_rank", "metric": metric, "lookback": lookback, "skip": skip, "direction": direction,
            "fraction": fraction, "hold": hold, "sides": sides, "max_names": max_names,
            "gate": {"kind": gate_kind, "n": gate_n} if gate_kind != "none" else {"kind": "none"}}
    return spec


def spec_id(spec: Dict[str, Any]) -> str:
    return hashlib.sha1(json.dumps(spec, sort_keys=True).encode("utf-8")).hexdigest()[:10]


def describe_spec(spec: Dict[str, Any], market: str = "") -> str:
    what = {"return": "return", "volatility": "volatility", "high_proximity": "closeness to the high",
            "volume_trend": "rise in traded value"}[spec["metric"]]
    top, bottom = ("highest", "lowest") if spec["direction"] == "follow" else ("lowest", "highest")
    legs = {"both": f"long the {top} 1/{spec['fraction']}, short the {bottom} 1/{spec['fraction']}",
            "long": f"long the {top} 1/{spec['fraction']}",
            "short": f"short the {bottom} 1/{spec['fraction']}"}[spec["sides"]]
    if spec.get("max_names"):
        legs += f" (at most {spec['max_names']} names a side)"
    skip = f", skipping the last {spec['skip']} day(s)" if spec["skip"] else ""
    gate = spec["gate"]
    gated = {"none": "", "bench_above_sma": f"; only while the benchmark is above its {gate.get('n')}-day average",
             "bench_below_sma": f"; only while the benchmark is below its {gate.get('n')}-day average",
             "bench_trend_sides": f"; longs only above, shorts only below the benchmark's {gate.get('n')}-day average"}[gate["kind"]]
    prefix = f"{market}: " if market else ""
    return f"{prefix}rank by {spec['lookback']}-day {what}{skip}; {legs}; re-rank every {spec['hold']} day(s){gated}"


# ------------------------------------------------------------------ signals
def metric_matrix(panel: Panel, spec: Dict[str, Any]) -> np.ndarray:
    """The ranking value of every instrument at the close of every day."""
    close, n = panel.close, int(spec["lookback"])
    rows, cols = close.shape
    skip = int(spec["skip"])
    out = np.full((rows, cols), np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        if spec["metric"] == "return":
            if rows > n:
                out[n:] = close[n - skip:rows - skip] / close[:rows - n] - 1 if skip else close[n:] / close[:-n] - 1
        elif spec["metric"] == "volatility":
            for i in range(n, rows):
                out[i] = np.nanstd(panel.ret[i - n + 1:i + 1], axis=0, ddof=1)
        elif spec["metric"] == "high_proximity":
            for i in range(n, rows):
                out[i] = close[i] / np.nanmax(close[i - n:i + 1], axis=0)
        elif spec["metric"] == "volume_trend":
            half = max(1, n // 2)
            for i in range(n, rows):
                recent = np.nanmean(panel.dollar_volume[i - half + 1:i + 1], axis=0)
                before = np.nanmean(panel.dollar_volume[i - n + 1:i - half + 1], axis=0)
                out[i] = recent / before - 1
    return out


def gate_matrix(panel: Panel, spec: Dict[str, Any]) -> Tuple[np.ndarray, np.ndarray]:
    """(longs allowed, shorts allowed) for every day, known at that day's close."""
    rows = panel.shape[0]
    allow_long, allow_short = np.ones(rows, dtype=bool), np.ones(rows, dtype=bool)
    gate = spec["gate"]
    if gate["kind"] == "none":
        return allow_long, allow_short
    if panel.bench < 0:
        return np.zeros(rows, dtype=bool), np.zeros(rows, dtype=bool)
    series, n = panel.close[:, panel.bench], int(gate["n"])
    above = np.zeros(rows, dtype=bool); known = np.zeros(rows, dtype=bool)
    for i in range(n - 1, rows):
        window = series[i - n + 1:i + 1]
        if np.isfinite(window).all():
            known[i] = True
            above[i] = series[i] > window.mean()
    if gate["kind"] == "bench_above_sma":
        allow_long = allow_short = known & above
    elif gate["kind"] == "bench_below_sma":
        allow_long = allow_short = known & ~above
    else:
        allow_long, allow_short = known & above, known & ~above
    return allow_long, allow_short


def _select(panel: Panel, spec: Dict[str, Any], row: int, metric: np.ndarray,
            allow_long: np.ndarray, allow_short: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """(long columns, short columns) chosen at the close of ``row``, most extreme first.

    The one place that picks members: the backtest and the live list both use it.
    """
    empty = np.array([], dtype=int)
    ok = np.isfinite(metric[row]) & panel.eligible[row]
    count, frac = int(ok.sum()), int(spec["fraction"])
    if count < 3 * frac:
        return empty, empty
    k = max(1, count // frac)
    cap = int(spec.get("max_names") or 0)
    if cap > 0:
        k = min(k, cap)
    order = np.argsort(np.where(ok, metric[row], np.nan), kind="stable")[:count]
    top, bottom = order[-k:][::-1], order[:k]
    longs, shorts = (top, bottom) if spec["direction"] == "follow" else (bottom, top)
    if spec["sides"] == "short" or not allow_long[row]:
        longs = empty
    if spec["sides"] == "long" or not allow_short[row]:
        shorts = empty
    return longs, shorts


def members_at(panel: Panel, spec: Dict[str, Any], row: int, metric: Optional[np.ndarray] = None) -> Tuple[List[int], List[int]]:
    """(long columns, short columns) chosen at the close of ``row``, most extreme first."""
    metric = metric_matrix(panel, spec) if metric is None else metric
    allow_long, allow_short = gate_matrix(panel, spec)
    longs, shorts = _select(panel, spec, row, metric, allow_long, allow_short)
    return [int(j) for j in longs], [int(j) for j in shorts]


def signal_matrix(panel: Panel, spec: Dict[str, Any], offset: int = 0, *,
                  metric: Optional[np.ndarray] = None,
                  gates: Optional[Tuple[np.ndarray, np.ndarray]] = None) -> np.ndarray:
    """+1 / -1 / 0 for every instrument at the close of every day."""
    rows, cols = panel.shape
    metric = metric_matrix(panel, spec) if metric is None else metric
    allow_long, allow_short = gate_matrix(panel, spec) if gates is None else gates
    hold = int(spec["hold"])
    sig = np.zeros((rows, cols)); last = np.zeros(cols)
    for i in range(rows):
        if i % hold == offset % hold:
            last = np.zeros(cols)
            longs, shorts = _select(panel, spec, i, metric, allow_long, allow_short)
            last[longs] = 1.0
            last[shorts] = -1.0
        sig[i] = last
    return sig


# ----------------------------------------------------------------- backtest
def daily_returns(panel: Panel, sig: np.ndarray, hold: int = 1, offset: int = 0) -> Tuple[np.ndarray, np.ndarray]:
    """(net return of every day, True where a position was held).

    The members ranked on row r (``sig[r]``) are entered at the close of row
    r + entry_lag in equal amounts, kept at a fixed quantity and closed at the
    close of the next ranking row. The return of a day is the profit of that
    day divided by the capital put in at the entry, so a short that doubles
    loses the whole amount it was opened with. One unit of cost for entering,
    one for closing.
    """
    rows = panel.shape[0]
    net, live = np.zeros(rows), np.zeros(rows, dtype=bool)
    price = panel.filled
    ranking_rows = [i for i in range(rows) if i % hold == offset % hold]
    for k, row in enumerate(ranking_rows):
        start = row + int(panel.entry_lag)
        end = ranking_rows[k + 1] if k + 1 < len(ranking_rows) else rows - 1
        if start >= end:
            continue
        entry = price[start]
        side = np.where(np.isfinite(entry) & (entry > 0), sig[row], 0.0)
        names = int((side != 0).sum())
        if names == 0:
            continue
        with np.errstate(invalid="ignore", divide="ignore"):
            path = np.nan_to_num(price[start:end + 1] / entry)      # value of one unit put in at the entry
        steps = np.diff(path, axis=0)
        profit = (steps * side).sum(axis=1) / names
        if panel.carry is not None:
            # Funding is paid on the position's current value.
            profit -= (panel.carry[start + 1:end + 1] * path[:-1] * side).sum(axis=1) / names
        if panel.borrow_daily > 0:
            profit -= (path[:-1] * (side < 0)).sum(axis=1) / names * panel.borrow_daily
        net[start + 1:end + 1] += profit
        net[start + 1] -= panel.cost
        net[end] -= panel.cost
        live[start + 1:end + 1] = True
    return net, live


def market_return(panel: Panel) -> np.ndarray:
    with np.errstate(invalid="ignore"):
        masked = np.where(panel.liquid, panel.ret, np.nan)
        counts = np.isfinite(masked).sum(axis=1)
        return np.where(counts > 0, np.nansum(masked, axis=1) / np.maximum(1, counts), 0.0)


def quarter_of(day: int) -> str:
    date = dt.date.fromordinal(dt.date(1970, 1, 1).toordinal() + int(day))
    return f"{date.year % 100}Q{(date.month - 1) // 3 + 1}"


def summarize(net: np.ndarray, live: np.ndarray, market: np.ndarray, days: np.ndarray,
              part: Optional[np.ndarray] = None) -> Dict[str, Any]:
    """Statistics of one return series inside ``part`` (default: the whole history).

    Counted from the first day a position was held to the last one, including
    the days in between on which the rule held nothing: they are days without
    return, and leaving them out would flatter a rule that is rarely in the market.
    """
    held = live if part is None else (live & part)
    index = np.flatnonzero(held)
    if len(index) < 40:
        return {"days": int(len(index)), "ok": False}
    span = np.zeros(len(net), dtype=bool)
    span[index[0]:index[-1] + 1] = True
    if part is not None:
        span &= part
    x, mkt, when = net[span], market[span], days[span]
    if float(np.std(x)) == 0.0:
        return {"days": int(len(x)), "ok": False}
    sd = float(x.std(ddof=1))
    sharpe = float(x.mean() / sd * np.sqrt(365 if len(set(when % 7)) == 7 else 252))
    t_stat = float(x.mean() / (sd / np.sqrt(len(x))))
    # What the rule earns beyond its exposure to the average instrument.
    design = np.column_stack([np.ones(len(x)), mkt])
    coef, *_ = np.linalg.lstsq(design, x, rcond=None)
    resid = x - design @ coef
    dof = max(1, len(x) - 2)
    cov = float(resid @ resid) / dof * np.linalg.pinv(design.T @ design)
    alpha_t = float(coef[0] / np.sqrt(cov[0, 0])) if cov[0, 0] > 0 else 0.0
    equity = np.cumprod(1 + x)
    drawdown = float((equity / np.maximum.accumulate(equity) - 1).min())
    quarters: Dict[str, float] = {}
    for day, value in zip(when, x):
        key = quarter_of(int(day)); quarters[key] = quarters.get(key, 0.0) + float(value)
    good = sum(1 for value in quarters.values() if value > 0)
    return {
        "ok": True, "days": int(len(x)), "days_in_market": int(len(index)),
        "total_pct": round(100 * float(equity[-1] - 1), 1),
        "mean_daily_pct": round(100 * float(x.mean()), 4), "sharpe": round(sharpe, 2), "t": round(t_stat, 2),
        "alpha_daily_pct": round(100 * float(coef[0]), 4), "alpha_t": round(alpha_t, 2), "beta": round(float(coef[1]), 2),
        "max_drawdown_pct": round(100 * drawdown, 1), "quarters_positive": good, "quarters": len(quarters),
    }


def evaluate(panel: Panel, spec: Dict[str, Any], *, train_share: float = TRAIN_SHARE) -> Dict[str, Any]:
    """Full result of one rule: train part, holdout part and the whole history.

    The edge (mean, alpha, alpha_t, beta, quarters) is measured on the average
    of every re-ranking schedule, so it does not depend on which weekday the
    ranking falls on. Live trades ONE schedule, so the figures that describe
    the ride come from the single schedules: Sharpe and total return are their
    median, the drawdown is the worst of them.
    """
    rows = panel.shape[0]
    hold = int(spec["hold"])
    metric, gates = metric_matrix(panel, spec), gate_matrix(panel, spec)
    series = [daily_returns(panel, signal_matrix(panel, spec, offset, metric=metric, gates=gates), hold, offset)
              for offset in range(hold)]
    net = np.mean([s[0] for s in series], axis=0)
    live = np.any([s[1] for s in series], axis=0)
    market = market_return(panel)
    cut = int(rows * train_share)
    train = np.zeros(rows, dtype=bool); train[:cut] = True

    def part_stats(part: Optional[np.ndarray]) -> Dict[str, Any]:
        blended = summarize(net, live, market, panel.days, part)
        if not blended.get("ok") or hold == 1:
            return blended
        singles = [summarize(s_net, s_live, market, panel.days, part) for s_net, s_live in series]
        if not all(single.get("ok") for single in singles):
            return {**blended, "ok": False, "reason": "a single re-ranking schedule has too few days"}
        return {
            **blended,
            "sharpe_all_schedules": blended["sharpe"], "total_pct_all_schedules": blended["total_pct"],
            "sharpe": round(float(np.median([single["sharpe"] for single in singles])), 2),
            "total_pct": round(float(np.median([single["total_pct"] for single in singles])), 1),
            "max_drawdown_pct": round(float(min(single["max_drawdown_pct"] for single in singles)), 1),
            "schedules": len(singles),
        }

    result = {
        "spec": spec, "id": spec_id(spec),
        "train": part_stats(train),
        "holdout": part_stats(~train),
        "full": part_stats(None),
        "train_until": int(panel.days[cut - 1]) if cut > 0 else None,
    }
    result["passes"] = passes(result)
    return result


def passes(result: Dict[str, Any]) -> bool:
    """The bar a rule must clear before it may be traded on paper."""
    train, holdout, full = result["train"], result["holdout"], result["full"]
    if not (train.get("ok") and holdout.get("ok") and full.get("ok")):
        return False
    return bool(
        train["sharpe"] >= PASS_SHARPE and train["alpha_t"] >= PASS_T
        and holdout["sharpe"] >= PASS_HOLDOUT_SHARPE and holdout["alpha_t"] >= PASS_HOLDOUT_T
        and holdout["mean_daily_pct"] > 0
        and full["alpha_t"] >= PASS_T
        and full["quarters_positive"] >= PASS_QUARTERS * full["quarters"]
        and full["max_drawdown_pct"] >= PASS_MAX_DRAWDOWN_PCT
    )


def rank_key(result: Dict[str, Any]) -> float:
    """Order rules by their weaker half: a rule is as good as its worse period."""
    train, holdout = result["train"], result["holdout"]
    if not (train.get("ok") and holdout.get("ok")):
        return -99.0
    return float(min(train["alpha_t"], 1.5 * holdout["alpha_t"]))


# ------------------------------------------------------------------- search
def default_space(max_names: int = 0, min_hold: int = 1) -> List[Dict[str, Any]]:
    """The rule space the lab searches by itself. Deliberately small: every extra rule is another chance to be fooled."""
    specs: List[Dict[str, Any]] = []
    for metric in METRICS:
        lookbacks = LOOKBACKS if metric == "return" else (10, 21, 42, 63)
        for lookback in lookbacks:
            for hold in HOLDS:
                if hold > lookback or hold < min_hold:
                    continue
                for direction in DIRECTIONS:
                    for sides in SIDES:
                        skips = (0, 1) if metric == "return" and lookback >= 7 else (0,)
                        for skip in skips:
                            specs.append({"family": "xs_rank", "metric": metric, "lookback": lookback, "skip": skip,
                                          "direction": direction, "fraction": 5, "hold": hold, "sides": sides,
                                          "gate": {"kind": "none"}})
    gated: List[Dict[str, Any]] = []
    for spec in specs:
        if spec["metric"] == "return" and spec["skip"] == 0 and spec["hold"] in (max(1, min_hold), 7) and spec["sides"] == "both":
            for n in GATE_WINDOWS:
                gated.append({**spec, "gate": {"kind": "bench_trend_sides", "n": n}})
    return [s for s in (normalize_spec({**spec, "max_names": max_names}) for spec in specs + gated) if s]


def run_search(panel: Panel, specs: Optional[Iterable[Dict[str, Any]]] = None, *, max_names: int = 0,
               min_hold: int = 1) -> List[Dict[str, Any]]:
    """Evaluate every rule; best first."""
    seen, results = set(), []
    for raw in (default_space(max_names, min_hold) if specs is None else specs):
        spec = normalize_spec(raw)
        if spec is None or spec_id(spec) in seen:
            continue
        seen.add(spec_id(spec))
        results.append(evaluate(panel, spec))
    results.sort(key=rank_key, reverse=True)
    return results


def distinct(results: Sequence[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    """The best rules that are really different from each other (one per metric/direction/sides)."""
    picked, kinds = [], set()
    for result in results:
        spec = result["spec"]
        kind = (spec["metric"], spec["direction"], spec["sides"], spec["gate"]["kind"])
        if kind in kinds:
            continue
        kinds.add(kind); picked.append(result)
        if len(picked) >= limit:
            break
    return picked
