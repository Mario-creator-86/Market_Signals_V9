"""Strategy Lab review: the research lead (Claude Opus on the subscription) weighs every finalist.

The statistics decide whether a rule is a finalist. The model then judges what
statistics cannot: is the rule plausible, is it a duplicate of something
already traded, does the evidence look like luck. It may also propose new rules
- as data in the lab's own rule format, never as code - which the next run
tests like any other rule.

The reply is parsed defensively: anything that is not valid is dropped, and a
missing or unusable reply leaves every finalist on "watch" (nothing is traded
on a guess).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Dict, List, Optional

from core.strategy_lab.engine import (
    DIRECTIONS, GATES, METRICS, PASS_HOLDOUT_SHARPE, PASS_HOLDOUT_T, PASS_SHARPE, PASS_T, SIDES,
    describe_spec, normalize_spec, spec_id,
)

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5-5"
VERDICTS = ("approve", "explore", "watch", "reject", "retire")
Ask = Callable[[str, str, str], Dict[str, Any]]

SYSTEM = (
    "You are the quantitative research lead of a paper-trading research application. Simulated money only. "
    "You judge rule-based strategies on evidence and you are skeptical: most backtested rules are luck, and a "
    "rule that only works because the market fell or rose in the sample is not an edge. You never invent numbers. "
    "You answer with one JSON object and nothing else."
)


def review_model(config: Any) -> str:
    """The model assigned to the research-scientist role (Opus unless the owner changed it)."""
    try:
        assignment = ((config or {}).get("ai") or {}).get("assignment") or {}
        return str(assignment.get("research_scientist") or assignment.get("quant_analyst") or DEFAULT_MODEL)
    except AttributeError:
        return DEFAULT_MODEL


def subscription_ask(prompt: str, system: str, model: str) -> Dict[str, Any]:
    """One call to the Claude subscription. {"text": ...} or {"error": ...}."""
    try:
        from core.ai.subscription_routing import generate_via_subscription

        reply = generate_via_subscription(prompt, model=model, system=system, timeout_override=420)
    except Exception as exc:  # noqa: BLE001 - provider, CLI or config trouble
        return {"error": f"{exc.__class__.__name__}: {exc}"}
    if reply is None:
        return {"error": f"model {model} is not served by an enabled Claude subscription provider"}
    if reply.get("error"):
        return {"error": str(reply["error"])}
    return {"text": str(reply.get("response") or ""), "model": str(reply.get("model") or model)}


def _row(result: Dict[str, Any], market: str) -> Dict[str, Any]:
    keep = ("days", "total_pct", "sharpe", "alpha_t", "beta", "max_drawdown_pct", "quarters_positive", "quarters")
    return {
        "id": result["id"], "rule": describe_spec(result["spec"], market), "spec": result["spec"],
        "passes_the_bar": bool(result.get("passes")),
        "passed_only_on_the_latest_data": bool(result.get("passed_only_on_the_latest_data")),
        "train": {k: result["train"].get(k) for k in keep if k in result["train"]},
        "holdout": {k: result["holdout"].get(k) for k in keep if k in result["holdout"]},
        "whole_history": {k: result["full"].get(k) for k in keep if k in result["full"]},
    }


def build_prompt(market: str, *, finalists: List[Dict[str, Any]], near_misses: List[Dict[str, Any]],
                 trading: List[Dict[str, Any]], summary: Dict[str, Any],
                 best_of_the_rest: Optional[List[Dict[str, Any]]] = None) -> str:
    payload = {
        "market": market,
        "how_rules_were_tested": {
            "data": summary.get("data"),
            "rules_tested_this_run": summary.get("rules_tested"),
            "rules_that_passed": summary.get("passed"),
            "finalists_shown": "one representative per kind of rule (same metric, direction, sides and gate); "
                               "other passing rules of the same kind are variants of the one shown",
            "method": (
                "daily bars; the members ranked at one day's close are entered at that close (crypto) or one "
                "close later (stocks), held at fixed quantity and closed at the next ranking; costs for entering "
                "and for closing every member; funding included on crypto; stock shorts pay an assumed 20% a year "
                "borrow fee; ETFs and leveraged products are not ranked; instruments need months of history "
                "before they can be picked; results averaged over every possible re-ranking day; alpha_t is the t-statistic of the "
                "return beyond exposure to the average instrument, measured on the average of all re-ranking "
                "schedules; Sharpe, total and drawdown are those of a single schedule (median; worst drawdown), "
                "because one schedule is what is traded. train = first 60% of the history, holdout = last 40%. "
                "BOTH periods were used to pick the rows shown to you (a rule had to hold in both, and the rows "
                "are the best of all rules tested), so the holdout is a second screen, not an untouched test. "
                "A finalist also had to pass as the history stood 5, 10 and 21 bars ago. The only untouched "
                "test is paper trading"
            ),
            "bar": f"train Sharpe >= {PASS_SHARPE} and alpha_t >= {PASS_T}; holdout Sharpe >= {PASS_HOLDOUT_SHARPE} and "
                   f"alpha_t >= {PASS_HOLDOUT_T}; whole-history alpha_t >= {PASS_T}; most quarters positive; drawdown limit",
            "multiple_testing_warning": "with this many rules, one or two pass by luck alone",
        },
        "finalists_passed_the_bar": [_row(r, market) for r in finalists],
        "near_misses_did_not_pass": [_row(r, market) for r in near_misses],
        # Shown when nothing came close, so that the next proposals start from what was actually measured.
        "best_tested_rules_far_from_the_bar": [_row(r, market) for r in (best_of_the_rest or [])],
        "already_trading_on_paper": trading,
        "rule_format": {
            "family": "xs_rank", "metric": list(METRICS), "lookback": "2..260 days", "skip": "0..10 days ignored at the end",
            "direction": list(DIRECTIONS), "fraction": "2..20 (top and bottom 1/fraction)", "hold": "1..30 days between re-rankings",
            "sides": list(SIDES), "gate": {"kind": list(GATES), "n": "5..260 day average of the benchmark"},
        },
    }
    return (
        "Review the rule strategies below for the " + market + " market and answer with ONE JSON object:\n"
        '{"verdicts": [{"id": "<rule id>", "verdict": "approve|explore|watch|reject|retire", "confidence": 0.0-1.0, '
        '"reason": "<two sentences, concrete>"}], '
        '"proposals": [<up to 6 new rules in rule_format that you want tested next run>], '
        '"commentary": "<three to five sentences for the owner: what the lab found, what it did not, what you would test next>"}\n\n'
        "Verdict meanings:\n"
        "- approve: only for a finalist; trade it on paper. Refuse if it duplicates a rule already trading, if the "
        "holdout is much weaker than the train period, or if it is a one-sided bet on market direction.\n"
        "- explore: a small, labelled paper experiment to gather live evidence: for a near miss you find plausible, "
        "or for a finalist you would trade but not call an approved strategy.\n"
        "- watch: keep testing as data arrives, do not trade.\n"
        "- reject: do not trade and do not bring back soon.\n"
        "- retire: only for a rule already trading on paper whose evidence no longer holds.\n"
        "Give a verdict for every finalist and for every rule already trading. For near misses a verdict is optional. "
        "Rules listed as far from the bar get no verdict; they are context for your proposals.\n"
        "Proposals must be genuinely different from what was tested (different metric, horizon, gate or side), "
        "not small parameter changes.\n\n"
        + json.dumps(payload, ensure_ascii=False)
    )


def parse_reply(text: str, known_ids: List[str]) -> Optional[Dict[str, Any]]:
    """The usable part of the model's reply, or None when it is not a JSON object."""
    match = re.search(r"\{.*\}", text or "", flags=re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    verdicts: Dict[str, Dict[str, Any]] = {}
    for item in data.get("verdicts") if isinstance(data.get("verdicts"), list) else []:
        if not isinstance(item, dict):
            continue
        rule_id, verdict = str(item.get("id") or ""), str(item.get("verdict") or "").lower().strip()
        if rule_id not in known_ids or verdict not in VERDICTS:
            continue
        try:
            confidence = min(1.0, max(0.0, float(item.get("confidence"))))
        except (TypeError, ValueError):
            confidence = 0.0
        verdicts[rule_id] = {"verdict": verdict, "confidence": round(confidence, 2),
                             "reason": str(item.get("reason") or "")[:600]}
    proposals, seen = [], set()
    for raw in data.get("proposals") if isinstance(data.get("proposals"), list) else []:
        spec = normalize_spec(raw)
        if spec and spec_id(spec) not in seen and len(proposals) < 6:
            seen.add(spec_id(spec)); proposals.append(spec)
    return {"verdicts": verdicts, "proposals": proposals, "commentary": str(data.get("commentary") or "")[:1500]}


def review_market(market: str, *, finalists: List[Dict[str, Any]], near_misses: List[Dict[str, Any]],
                  trading: List[Dict[str, Any]], summary: Dict[str, Any], model: str = DEFAULT_MODEL,
                  ask: Optional[Ask] = None, best_of_the_rest: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Verdicts, proposals and a commentary for one market. ``available`` is False when the model gave nothing usable.

    When nothing came close to the bar the research lead is still asked, with the
    best of what was tested, so that new proposals keep arriving.
    """
    known = [r["id"] for r in finalists + near_misses] + [str(t.get("id")) for t in trading]
    empty = {"available": False, "verdicts": {}, "proposals": [], "commentary": "", "model": model}
    if not known and not best_of_the_rest:
        return {**empty, "error": "nothing to review"}
    prompt = build_prompt(market, finalists=finalists, near_misses=near_misses, trading=trading, summary=summary,
                          best_of_the_rest=best_of_the_rest if not known else None)
    reply = (ask or subscription_ask)(prompt, SYSTEM, model)
    if reply.get("error") or not reply.get("text"):
        logger.warning("Strategy lab: review unavailable for %s: %s", market, reply.get("error") or "empty reply")
        return {**empty, "error": str(reply.get("error") or "empty reply")}
    parsed = parse_reply(str(reply["text"]), known)
    if parsed is None:
        return {**empty, "error": "the reply was not a JSON object", "model": str(reply.get("model") or model)}
    return {"available": True, **parsed, "model": str(reply.get("model") or model), "error": ""}
