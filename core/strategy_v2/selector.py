from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .performance_cache import StrategyPerformanceCache, build_perf_key, calculate_validation_score
from .quality_gates import QualityGates
from .regime_engine import RegimeEngine
from .registry import StrategySpec, build_default_registry
from .types import CandidateSignal, DIRECTION_LONG, DIRECTION_SHORT


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _i(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


class StrategySelector:
    def __init__(
        self,
        config: Dict[str, Any] | None = None,
        perf_cache: Optional[StrategyPerformanceCache] = None,
        lifecycle_registry: Optional[Any] = None,
    ):
        self.config = config or {}
        self.registry = build_default_registry()
        self.regime_engine = RegimeEngine((self.config or {}).get("regime_engine", {}))
        self.quality_gates = QualityGates(self.config)
        self.perf_cache = perf_cache or StrategyPerformanceCache()
        self.lifecycle_registry = lifecycle_registry

    def _lifecycle_record_for_execution(self, strategy_id: str) -> tuple[Optional[Any], Optional[str]]:
        """Resolve an approved executable version, failing closed on read errors."""

        if self.lifecycle_registry is None:
            return None, "strategy_lifecycle_registry_unavailable"
        try:
            record = self.lifecycle_registry.active_record(strategy_id)
        except Exception:
            return None, "strategy_lifecycle_registry_error"
        if record is None:
            return None, "strategy_lifecycle_unapproved"
        status = getattr(record, "status", "")
        status_value = str(getattr(status, "value", status)).strip().upper()
        if status_value not in {"APPROVED", "CHAMPION"}:
            return None, "strategy_lifecycle_unapproved"
        return record, None

    def _enabled_ids_for_asset(self, asset_class: str) -> List[str]:
        enabled_map = (self.config or {}).get("enabled_strategies_by_asset_class", {}) or {}
        from_cfg = enabled_map.get(str(asset_class).upper(), [])
        if isinstance(from_cfg, list) and from_cfg:
            return [str(x).upper() for x in from_cfg]
        return [s.id.upper() for s in self.registry.get_specs_for_asset(asset_class)]

    def _ttl_for_asset(self, asset_class: str) -> int:
        ttl_map = (self.config or {}).get("performance_ttl_seconds", {}) or {}
        if str(asset_class).upper() == "CRYPTO_FUTURES":
            return _i(ttl_map.get("CRYPTO_FUTURES"), 6 * 3600)
        if str(asset_class).upper() == "US_STOCKS":
            return _i(ttl_map.get("US_STOCKS"), 24 * 3600)
        return _i(ttl_map.get("DEFAULT"), 12 * 3600)

    def _research_only_ids(self) -> set[str]:
        """Strategies allowed to surface before their OOS record is mature.

        This is a visibility mechanism, not a validation fallback.  Candidates
        returned through this path are explicitly marked non-executable and are
        never considered for ``best_candidate``.
        """
        configured = (self.config or {}).get("research_only_strategy_ids", []) or []
        if not isinstance(configured, list):
            return set()
        return {str(item or "").upper().strip() for item in configured if str(item or "").strip()}

    def _can_emit_research_candidate(self, spec: StrategySpec) -> bool:
        return bool((self.config or {}).get("emit_unvalidated_research_candidates", False)) and (
            str(spec.id or "").upper() in self._research_only_ids()
        )

    def _score_candidate(self, candidate: CandidateSignal, perf: Dict[str, Any]) -> CandidateSignal:
        target_wr = _f(self.config.get("target_win_rate"), 0.60)
        min_sample = max(_i(self.config.get("min_sample_size"), 30), 1)
        candidate.validation_score = calculate_validation_score(perf, target_win_rate=target_wr, min_sample=min_sample)

        # Confluence is an independently auditable OHLCV score. Preserve the
        # 25% OOS-validation weight; the remaining technical weights are
        # proportionally rebalanced so a new signal does not bypass validation.
        confluence_weight = max(0.0, min(0.20, _f(self.config.get("confluence_weight"), 0.12)))
        remaining = 0.75 - confluence_weight
        candidate.total_score = (
            ((remaining * 0.60) * _f(candidate.setup_score, 0.0))
            + ((remaining * 0.20) * _f(candidate.catalyst_score, 0.0))
            + ((remaining * 0.20) * _f(candidate.risk_score, 0.0))
            + (confluence_weight * _f(candidate.confluence_score, 50.0))
            + (0.25 * _f(candidate.validation_score, 0.0))
        )
        candidate.perf_snapshot = {
            "win_rate": _f(perf.get("win_rate"), 0.0),
            "profit_factor": _f(perf.get("profit_factor"), 0.0) if perf.get("profit_factor") is not None else None,
            "expectancy_R": _f(perf.get("expectancy_R"), 0.0),
            "avg_R": _f(perf.get("avg_R"), 0.0),
            "max_drawdown": _f(perf.get("max_drawdown"), 0.0),
            "sample_size": _i(perf.get("sample_size"), 0),
            "last_run": perf.get("last_run"),
            "costs_used": perf.get("costs_used", {}),
            "validation_status": perf.get("validation_status"),
            "validation_method": perf.get("validation_method"),
            "validation_as_of": perf.get("validation_as_of"),
        }
        return candidate

    def _should_reject_for_validation(
        self,
        perf: Dict[str, Any],
        validation_score: float,
        asset_class: str = "",
    ) -> Optional[str]:
        min_sample = max(_i(self.config.get("min_sample_size"), 30), 1)
        sample = _i(perf.get("sample_size"), 0)
        require_profitable = bool(self.config.get("require_profitable_performance", False))
        if require_profitable and sample < min_sample:
            return f"sample_too_small:{sample}<{min_sample}"
        if sample < min_sample and not bool(self.config.get("allow_low_sample_fallback", True)):
            return f"sample_too_small:{sample}<{min_sample}"

        min_val = _f(self.config.get("min_validation_score"), 60.0)
        if require_profitable:
            # The old research agent wrote an in-sample QuickBacktester result
            # into this cache and used a legacy trade-mode proxy for V2.  Those
            # numbers are useful diagnostics, but they do not validate the live
            # StrategySelector.  A profitable V2 candidate must therefore carry
            # explicit provenance from the faithful, out-of-sample validator.
            # This private flag is set only by the in-memory, point-in-time
            # walk-forward replayer.  It allows the test half of a window to
            # consume performance computed from that window's *preceding*
            # bars without pretending those metrics are persisted validation.
            # It is never written to config.json and the cache has no save API,
            # so it cannot relax a live Capital decision.
            training_window_replay = bool(self.config.get("_walk_forward_training_performance", False))
            if not training_window_replay:
                required_method = str(
                    self.config.get("required_validation_method", "faithful_v2_walk_forward")
                ).strip().lower()
                validation_status = str(perf.get("validation_status") or "").strip().lower()
                validation_method = str(perf.get("validation_method") or "").strip().lower()
                if validation_status != "validated":
                    return f"validation_unverified:{validation_status or 'missing'}"
                if validation_method != required_method:
                    return f"validation_method_mismatch:{validation_method or 'missing'}"
                # Research refreshes are periodic.  A historical validation that is
                # outside the configured asset TTL cannot continue admitting new
                # V2 candidates merely because its metrics were once positive.
                if not self.perf_cache.is_fresh(perf, self._ttl_for_asset(asset_class)):
                    return "validation_stale_or_missing_timestamp"

            if perf.get("profit_factor") is None:
                return "profit_factor_unmeasurable"
            pf = _f(perf.get("profit_factor"), 0.0)
            min_pf = _f(self.config.get("min_profit_factor", 1.2), 1.2)
            if pf < min_pf:
                return f"profit_factor_low:{pf:.2f}<{min_pf:.2f}"

            expectancy_r = _f(perf.get("expectancy_R", perf.get("expectancy_r", 0.0)), 0.0)
            min_expectancy = _f(self.config.get("min_expectancy_R", 0.0), 0.0)
            if expectancy_r <= min_expectancy:
                return f"expectancy_low:{expectancy_r:.3f}<={min_expectancy:.3f}"

            max_drawdown = _f(perf.get("max_drawdown"), 0.0)
            max_allowed_dd = _f(self.config.get("max_drawdown_pct", 25.0), 25.0)
            if max_drawdown > max_allowed_dd:
                return f"drawdown_high:{max_drawdown:.1f}>{max_allowed_dd:.1f}"

        if validation_score < min_val and not bool(self.config.get("allow_low_validation_fallback", True)):
            return f"validation_low:{validation_score:.1f}<{min_val:.1f}"
        return None

    def select(self, symbol: str, asset_class: str, context: Dict[str, Any]) -> Dict[str, Any]:
        regime_data = self.regime_engine.detect(context)
        regime = str(regime_data.get("regime") or "RANGE")
        context = dict(context or {})
        context["asset_class"] = asset_class
        context["symbol"] = symbol

        # Runda-6 D2: snapshot the perf cache ONCE per select() call. Pre-fix each
        # candidate called perf_cache.get_entry() which re-read the JSON file from
        # disk per call; a concurrent writer (feedback_loop upsert) landing between
        # the LONG and SHORT candidate reads gave them different perf snapshots,
        # non-deterministically flipping best_long-vs-best_short. The snapshot makes
        # the per-select perf view atomic regardless of concurrent writers (and
        # cuts 2N disk reads down to 1).
        perf_snapshot = self.perf_cache.get_symbol_entries(symbol)

        enabled_ids = set(self._enabled_ids_for_asset(asset_class))
        ttl_seconds = self._ttl_for_asset(asset_class)
        # Keep the same positive-edge floor as QualityGates while allowing the
        # configured admission threshold to take effect.
        min_rr = max(_f(self.config.get("min_rr"), 2.0), 1.2)
        candidates: List[CandidateSignal] = []
        research_candidates: List[CandidateSignal] = []
        rejected: List[Dict[str, Any]] = []

        for spec in self.registry.get_specs_for_asset(asset_class):
            if spec.id.upper() not in enabled_ids:
                continue
            lifecycle_record = None
            if bool(self.config.get("lifecycle_enforcement_enabled", False)):
                lifecycle_record, lifecycle_rejection = self._lifecycle_record_for_execution(spec.id)
                if lifecycle_rejection is not None:
                    rejected.append(
                        {
                            "strategy_id": spec.id,
                            "direction": "BOTH",
                            "reason": lifecycle_rejection,
                        }
                    )
                    continue
            if regime.upper() not in {x.upper() for x in spec.regimes_allowed}:
                continue

            evaluator = self.registry.get_evaluator(spec.id)
            if evaluator is None:
                continue

            for direction in [DIRECTION_LONG, DIRECTION_SHORT]:
                candidate = evaluator.evaluate(context=context, spec=spec, direction=direction, regime=regime)
                if candidate is None:
                    continue

                if lifecycle_record is not None:
                    lifecycle_status = getattr(lifecycle_record, "status", "")
                    candidate.meta = dict(candidate.meta or {})
                    candidate.meta.update(
                        {
                            "lifecycle_status": str(
                                getattr(lifecycle_status, "value", lifecycle_status)
                            ).upper(),
                            "lifecycle_version": str(getattr(lifecycle_record, "version", "")),
                        }
                    )

                if candidate.rr < min_rr:
                    rejected.append(
                        {
                            "strategy_id": spec.id,
                            "direction": direction,
                            "reason": f"rr_below_min:{candidate.rr:.2f}",
                        }
                    )
                    continue

                passes, gate_reasons = self.quality_gates.evaluate(context=context, candidate=candidate)
                if not passes:
                    rejected.append(
                        {
                            "strategy_id": spec.id,
                            "direction": direction,
                            "reason": ",".join(gate_reasons),
                        }
                    )
                    continue

                perf = perf_snapshot.get(build_perf_key(symbol, asset_class, spec.id, direction), {})
                if not perf:
                    perf = {
                        "win_rate": 0.0,
                        "profit_factor": 0.0,
                        "expectancy_R": 0.0,
                        "avg_R": 0.0,
                        "max_drawdown": 0.0,
                        "sample_size": 0,
                        "last_run": None,
                        "costs_used": {},
                    }
                # NOTE: a `perf["is_fresh"] = self.perf_cache.is_fresh(...)` field
                # was written here pre-fix but NEVER read by any code branch (the
                # candidate.perf_snapshot below doesn't even copy it) -- a dead
                # write. The real freshness gate lives in
                # agents/strategy_research_agent.py (calls StrategyPerformanceCache
                # .is_fresh() to skip re-running fresh backtests). Removed to avoid
                # implying a selector-level staleness gate that doesn't exist.

                # Skip suspended strategies
                if perf.get("suspended"):
                    rejected.append({
                        "strategy_id": spec.id,
                        "direction": direction,
                        "reason": "strategy_suspended",
                    })
                    continue

                # Apply score penalty for degraded strategies
                if perf.get("degraded"):
                    candidate.setup_score *= 0.7

                candidate = self._score_candidate(candidate, perf)
                reject_reason = self._should_reject_for_validation(
                    perf,
                    candidate.validation_score,
                    asset_class=asset_class,
                )
                if reject_reason:
                    if self._can_emit_research_candidate(spec):
                        candidate.meta = dict(candidate.meta or {})
                        candidate.meta.update(
                            {
                                "research_only": True,
                                "capital_execution_eligible": False,
                                "capital_execution_block_reason": (
                                    f"{spec.id} is visible for forward research only until faithful "
                                    "out-of-sample validation is current and profitable"
                                ),
                                "validation_rejection": reject_reason,
                            }
                        )
                        candidate.reasons = list(candidate.reasons or [])
                        candidate.reasons.append(f"research_only:{reject_reason}")
                        research_candidates.append(candidate)
                    rejected.append(
                        {
                            "strategy_id": spec.id,
                            "direction": direction,
                            "reason": reject_reason,
                        }
                    )
                    continue

                candidates.append(candidate)

        best_long = None
        best_short = None
        for c in candidates:
            if c.direction == DIRECTION_LONG and (best_long is None or c.total_score > best_long.total_score):
                best_long = c
            if c.direction == DIRECTION_SHORT and (best_short is None or c.total_score > best_short.total_score):
                best_short = c

        best = best_long
        reverse_consideration = {
            "best_long_score": _f(getattr(best_long, "total_score", 0.0), 0.0),
            "best_short_score": _f(getattr(best_short, "total_score", 0.0), 0.0),
            "selected_direction": None,
            "reversed": False,
        }
        if best_short is not None and (best is None or best_short.total_score > best.total_score):
            best = best_short
        if best is not None:
            reverse_consideration["selected_direction"] = best.direction
            if best_long is not None and best_short is not None:
                reverse_consideration["reversed"] = best.direction == DIRECTION_SHORT and best_short.total_score > best_long.total_score

        return {
            "symbol": symbol,
            "asset_class": asset_class,
            "regime": regime_data,
            "candidates_considered": len(candidates),
            "research_candidates": [candidate.to_dict() for candidate in research_candidates],
            "research_candidates_considered": len(research_candidates),
            "rejected": rejected[:30],
            "best_candidate": best.to_dict() if best else None,
            "best_long": best_long.to_dict() if best_long else None,
            "best_short": best_short.to_dict() if best_short else None,
            "reverse_consideration": reverse_consideration,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
