"""Strategy Lab read model for the UI and the run-now action."""
from __future__ import annotations

from typing import Any, Dict, List

from core.strategy_lab.lab import TRADING, lab_config, load_registry, paper_results
from core.strategy_lab.live import get_lab_baskets
from core.strategy_lab.runner import get_lab_runner

STATUS_ORDER = {"paper": 0, "explore": 1, "watch": 2, "rejected": 3, "retired": 4}


def _config() -> Dict[str, Any]:
    from api.state_service import get_shared_config

    shared = get_shared_config()
    return lab_config(getattr(shared, "config", shared))


def strategy_lab_state() -> Dict[str, Any]:
    registry = load_registry()
    results = paper_results()
    strategies: List[Dict[str, Any]] = []
    for sid, strategy in (registry.get("strategies") or {}).items():
        row = {key: strategy.get(key) for key in (
            "id", "market", "description", "status", "origin", "created_at", "status_changed_at", "decision",
            "review", "stats", "fail_streak", "strategy_types", "last_evaluated_at", "history",
        )}
        live = strategy.get("live") if isinstance(strategy.get("live"), dict) else None
        if live:
            row["live"] = {key: live.get(key) for key in ("as_of_date", "enter_from", "enter_until", "exit_at", "long", "short")}
            # A ranking that was never entered is not what the rule holds.
            row["live"]["not_entered"] = (
                "the entry window had passed when it was ranked; it is ranked again on the next run" if live.get("missed_window")
                else "the newest day's data was incomplete; it is ranked again on the next run" if live.get("incomplete_data")
                else ""
            )
        row["paper_results"] = results.get(sid)
        strategies.append(row)
    strategies.sort(key=lambda s: (STATUS_ORDER.get(str(s.get("status")), 9), str(s.get("market")), str(s.get("id"))))
    markets = {}
    for market, board in (registry.get("markets") or {}).items():
        markets[market] = {key: board.get(key) for key in (
            "updated_at", "data", "rules_tested", "passed", "leaderboard", "commentary", "commentary_at",
            "review_model", "review_error",
        )}
        markets[market]["proposals_for_next_run"] = len(board.get("proposals") or [])
        markets[market]["trading"] = sum(1 for s in strategies if s.get("market") == market and s.get("status") in TRADING)
    return {
        "config": _config(),
        "runner": get_lab_runner().status(),
        "last_run_at": registry.get("last_run_at"),
        "runs": list(reversed((registry.get("runs") or [])[-10:])),
        "markets": markets,
        "strategies": strategies,
        "live": get_lab_baskets().status(),
        "how_it_decides": (
            "A rule is paper-traded only when it holds in both parts of the history (first 60% and last 40%), "
            "kept passing as the history stood 5, 10 and 21 bars ago, AND the research lead (Claude Opus) approves "
            "it. Both parts are used to choose among hundreds of rules, so neither is an untouched test: the only "
            "untouched test is paper trading. A rule the research lead does not call a strategy can run as one "
            "small labelled exploration experiment when it asks for that. A trading rule whose evidence fades for "
            "several runs, or which the research lead retires, stops opening positions."
        ),
    }


def strategy_lab_run_now() -> Dict[str, Any]:
    return get_lab_runner().run_now()
