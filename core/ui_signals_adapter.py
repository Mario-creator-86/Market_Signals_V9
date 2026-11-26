# ui_signals_adapter.py - adapter for displaying signals in the UI

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Dict


@dataclass
class TextSpan:
    text: str
    tag: Optional[str] = None  # ex: "header", "variant_a", "buy", "win", "info", "backtest"


def build_signals_spans(signals: Optional[dict]) -> List[TextSpan]:
    """
    Transform the 'signals_state.json' structure into a list of text chunks + tags
    for the ScrolledText in the signals panel.
    """
    spans: List[TextSpan] = []

    if not signals:
        # Do NOT put the "Start engine.py" message here; UI handles it separately
        return spans

    open_sigs = signals.get("open", {}) or {}
    closed_sigs = signals.get("closed", []) or []

    # Header OPEN SIGNALS
    spans.append(TextSpan("═" * 55 + "\n", "header"))
    spans.append(TextSpan(" 🚨 OPEN SIGNALS\n", "header"))
    spans.append(TextSpan("═" * 55 + "\n\n", "header"))

    # Open signals
    if open_sigs:
        for sid, s in sorted(
            open_sigs.items(),
            key=lambda x: x[1].get("created_at", ""),
            reverse=True,
        ):
            spans.extend(_format_open_signal(s))
    else:
        spans.append(TextSpan(" (no active signals)\n\n", "info"))

    # Header CLOSED
    spans.append(TextSpan("\n" + "═" * 55 + "\n", "header"))
    spans.append(TextSpan(" 📋 CLOSED (last 10)\n", "header"))
    spans.append(TextSpan("═" * 55 + "\n\n", "header"))

    # Last 10 closed signals
    for s in reversed(closed_sigs[-10:]):
        spans.append(_format_closed_signal_line(s))

    return spans


# ========== Internal helpers ==========

def _format_open_signal(s: dict) -> List[TextSpan]:
    out: List[TextSpan] = []

    created_time = s.get("created_at_time", "")
    if not created_time:
        ca = s.get("created_at", "")
        if isinstance(ca, str) and len(ca) >= 19:
            created_time = ca[11:19]
        else:
            created_time = ""

    variant = s.get("variant", "C")
    variant_mode = str(s.get("variant_mode", "STRICT")).upper()
    variant_tag = f"variant_{variant.lower()}"
    action = s.get("action", s.get("type", ""))
    action_tag = "buy" if action == "BUY" else "short"
    ticker = s.get("ticker", "")

    out.append(TextSpan("─" * 55 + "\n", "header"))
    out.append(TextSpan(f" 🚨 {created_time} - ", "header"))
    out.append(TextSpan(f"[{variant}|{variant_mode}] ", variant_tag))
    out.append(TextSpan(f"{action} {ticker}\n", action_tag))

    entry = float(s.get("entry", 0.0) or 0.0)
    tp = float(s.get("tp", 0.0) or 0.0)
    sl = float(s.get("sl", 0.0) or 0.0)
    tp_pct = float(s.get("tp_pct", 0.0) or 0.0)
    sl_pct = float(s.get("sl_pct", 0.0) or 0.0)
    out.append(
        TextSpan(
            f" 💰 Entry: ${entry:.4f} | TP: ${tp:.4f} (+{tp_pct}%) | SL: ${sl:.4f} (-{sl_pct}%)\n",
            None,
        )
    )



    exit_mode = str(s.get("exit_mode", "") or "").upper()
    if exit_mode == "ATR":
        atr_period = s.get("atr_period", 0)
        atr_tf = s.get("atr_exit_timeframe") or s.get("atr_timeframe") or ""
        atr_exit = float(s.get("atr_exit", 0.0) or 0.0)
        tp_mult = s.get("tp_atr_mult")
        sl_mult = s.get("sl_atr_mult")
        out.append(
            TextSpan(
                f"  Exit: ATR P{atr_period} {atr_tf} ATR={atr_exit:.6f} TPx{tp_mult} SLx{sl_mult}\n",
                None,
            )
        )
    elif exit_mode:
        out.append(TextSpan(f"  Exit: {exit_mode} (TP%={tp_pct} SL%={sl_pct})\n", None))

    prob = float(s.get("probability_score", 0.0) or 0.0)
    regime = s.get("market_regime", "")
    size_factor = s.get("size_factor")
    position_size_pct = s.get("position_size_pct")
    try:
        size_factor_f = float(size_factor) if size_factor is not None else None
    except Exception:
        size_factor_f = None
    try:
        position_size_f = float(position_size_pct) if position_size_pct is not None else None
    except Exception:
        position_size_f = None
    if prob or regime or size_factor_f is not None or position_size_f is not None:
        size_txt = "-" if size_factor_f is None else f"{size_factor_f:.2f}x"
        risk_txt = "-" if position_size_f is None else f"{position_size_f:.2f}%"
        out.append(TextSpan(f"  Prob: {prob:.2f} | Regime: {regime} | Size: {size_txt} ({risk_txt})\n", None))
    # Simplified data access pattern with clear fallback hierarchy:
    # 1. Try trigger_info dict first (new structure)
    # 2. Fall back to root signal dict (legacy structure)
    # 3. Default to 0.0 if neither exists
    trigger = s.get("trigger_info", {}) or {}
    rsi_value = trigger.get("rsi") or s.get("rsi") or 0.0
    macd_value = trigger.get("macd_hist") or s.get("macd_hist") or 0.0
    rsi = float(rsi_value)
    macd_h = float(macd_value)
    mtf = int(s.get("mtf_score", 0) or 0)
    qual = int(s.get("quality_score", 0) or 0)
    out.append(
        TextSpan(
            f" 📊 RSI: {rsi:.1f} | MACD_H: {macd_h:.6f} | MTF: {mtf} | Q: {qual}\n",
            None,
        )
    )

    mtf_1h = trigger.get("mtf_1h", "N/A")
    mtf_30m = trigger.get("mtf_30m", "N/A")
    mtf_5m = trigger.get("mtf_5m", "N/A")
    out.append(
        TextSpan(
            f" 📈 MTF: 1H={mtf_1h} | 30M={mtf_30m} | 5M={mtf_5m}\n",
            None,
        )
    )

    reasons = trigger.get("reasons", []) or []
    if reasons:
        out.append(
            TextSpan(f" ✅ {', '.join(reasons)}\n", "info")
        )

    if s.get("strict_backed"):
        sb_variant = s.get("strict_variant", "?")
        sb_mode = s.get("strict_signal_mode", "")
        sb_score = s.get("strict_signal_score", "")
        out.append(
            TextSpan(
                f" 🔒 Backed by STRICT [{sb_variant}] {sb_mode} | Score {sb_score}\n",
                "info",
            )
        )

    if s.get("strict_replaced"):
        rep = s.get("replaced_signal") or {}
        rep_variant = rep.get("variant", "?")
        rep_mode = rep.get("variant_mode", "")
        rep_action = rep.get("action", "")
        rep_score = rep.get("signal_score", "")
        out.append(
            TextSpan(
                f" 🔁 STRICT replaced [{rep_variant}|{rep_mode}] {rep_action} (score {rep_score})\n",
                "info",
            )
        )

    # Quick backtest: prefer 30d, then 7d; fallback to best strategy when available.
    bt_30d: Dict = s.get("quick_backtest_30d", {}) or {}
    bt_7d: Dict = s.get("quick_backtest_7d", {}) or {}
    best_strategy: Dict = s.get("best_strategy", {}) or {}
    quick_bt: Dict = bt_30d if (bt_30d.get("total", 0) or 0) > 0 else (bt_7d or bt_30d)
    bt_label = "30d" if quick_bt is bt_30d else "7d"
    if (quick_bt.get("total", 0) or 0) <= 0 and (best_strategy.get("total_signals", 0) or 0) > 0:
        quick_bt = {
            "wins": int(best_strategy.get("wins", 0) or 0),
            "losses": int(best_strategy.get("losses", 0) or 0),
            "total": int(best_strategy.get("total_signals", 0) or 0),
            "win_rate": float(best_strategy.get("win_rate", 0) or 0),
            "risk_mode": best_strategy.get("risk_mode", "N/A"),
            "trade_mode": best_strategy.get("trade_mode", ""),
            "fallback_used": True,
        }
        bt_label = "BEST"
    if quick_bt:
        total = int(quick_bt.get("total", 0) or 0)
        risk_m = quick_bt.get("risk_mode", "N/A")
        trade_m = quick_bt.get("trade_mode", "")
        mode_txt = f"{risk_m}/{trade_m}" if trade_m else str(risk_m)
        if total > 0:
            suffix = " (fallback)" if quick_bt.get("fallback_used") else ""
            out.append(
                TextSpan(
                    f" 📊 Backtest {bt_label} ({mode_txt}{suffix}): "
                    f"WIN {quick_bt.get('wins', 0)}/{total} "
                    f"({quick_bt.get('win_rate', 0)}%) | "
                    f"LOSS {quick_bt.get('losses', 0)}/{total} "
                    f"({100 - quick_bt.get('win_rate', 0):.1f}%)\n",
                    "backtest",
                )
            )
        else:
            out.append(
                TextSpan(
                    f" 📊 Backtest {bt_label} ({mode_txt}): 0 signals found (insufficient history)\n",
                    "info",
                )
            )

    out.append(TextSpan("\n", None))
    return out


def _format_closed_signal_line(s: dict) -> TextSpan:
    status = s.get("status", "")
    tag = "win" if status == "WIN" else "lost" if status == "LOST" else "info"
    emoji = "✅" if status == "WIN" else "❌" if status == "LOST" else "🔄"
    action = s.get("action", s.get("type", ""))
    ticker = s.get("ticker", "")
    variant = s.get("variant", "C")
    variant_mode = str(s.get("variant_mode", "STRICT")).upper()
    entry = float(s.get("entry", 0.0) or 0.0)
    text = f" {emoji} [{variant}|{variant_mode}] {action} {ticker} @{entry:.2f} → {status}\n"
    return TextSpan(text, tag)