from types import SimpleNamespace

from core.strategy_registry import StrategyStatus
from core.strategy_v2.registry import StrategySpec
from core.strategy_v2.selector import StrategySelector
from core.strategy_v2.types import CandidateSignal


class _PerfCache:
    def get_symbol_entries(self, _symbol):
        return {}

    def is_fresh(self, _entry, _ttl):
        return True


class _Evaluator:
    def __init__(self):
        self.calls = 0

    def evaluate(self, context, spec, direction, regime):
        self.calls += 1
        if direction == "SHORT":
            return None
        return CandidateSignal(
            strategy_id=spec.id,
            strategy_name=spec.name,
            symbol=context["symbol"],
            asset_class=context["asset_class"],
            direction=direction,
            regime=regime,
            entry=100.0,
            sl=95.0,
            tp1=110.0,
            rr=2.0,
            trailing_rule="trail",
            invalidation_condition="close below support",
            setup_score=80.0,
            catalyst_score=60.0,
            risk_score=70.0,
        )


class _ExecutableRegistry:
    def __init__(self, evaluator):
        self.spec = StrategySpec(
            id="S2_BREAKOUT_RETEST",
            name="Breakout Retest",
            asset_classes=["US_STOCKS"],
            regimes_allowed=["RANGE"],
            features_required=["resistance"],
            entry_rules="breakout and retest",
            exit_rules="stop and target",
        )
        self.evaluator = evaluator

    def get_specs_for_asset(self, _asset_class):
        return [self.spec]

    def get_evaluator(self, _strategy_id):
        return self.evaluator


class _LifecycleRegistry:
    def __init__(self, record=None, error=None):
        self.record = record
        self.error = error
        self.calls = 0

    def active_record(self, _strategy_id):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.record


def _selector(*, enabled, lifecycle_registry=None):
    evaluator = _Evaluator()
    selector = StrategySelector(
        config={
            "lifecycle_enforcement_enabled": enabled,
            "min_rr": 1.2,
            "min_validation_score": 0.0,
        },
        perf_cache=_PerfCache(),
        lifecycle_registry=lifecycle_registry,
    )
    selector.registry = _ExecutableRegistry(evaluator)
    selector.regime_engine.detect = lambda _context: {"regime": "RANGE"}
    selector.quality_gates.evaluate = lambda context, candidate: (True, [])
    return selector, evaluator


def _select(selector):
    return selector.select("AAPL", "US_STOCKS", {"price": 100.0})


def test_lifecycle_enforcement_defaults_off_and_does_not_consult_registry():
    lifecycle = _LifecycleRegistry(error=AssertionError("must not be called"))
    selector, evaluator = _selector(enabled=False, lifecycle_registry=lifecycle)

    result = _select(selector)

    assert result["best_candidate"] is not None
    assert evaluator.calls == 2
    assert lifecycle.calls == 0


def test_enabled_enforcement_fails_closed_without_registry():
    selector, evaluator = _selector(enabled=True)

    result = _select(selector)

    assert result["best_candidate"] is None
    assert evaluator.calls == 0
    assert result["rejected"][0]["reason"] == "strategy_lifecycle_registry_unavailable"


def test_research_or_missing_strategy_is_not_executable():
    for record in (
        None,
        SimpleNamespace(version="2.0.0", status=StrategyStatus.RESEARCH),
    ):
        lifecycle = _LifecycleRegistry(record=record)
        selector, evaluator = _selector(enabled=True, lifecycle_registry=lifecycle)

        result = _select(selector)

        assert result["best_candidate"] is None
        assert evaluator.calls == 0
        assert result["rejected"][0]["reason"] == "strategy_lifecycle_unapproved"


def test_approved_strategy_is_executable_and_lifecycle_is_auditable():
    lifecycle = _LifecycleRegistry(
        record=SimpleNamespace(version="2.0.0", status=StrategyStatus.APPROVED)
    )
    selector, evaluator = _selector(enabled=True, lifecycle_registry=lifecycle)

    result = _select(selector)

    assert evaluator.calls == 2
    assert result["best_candidate"] is not None
    assert result["best_candidate"]["meta"]["lifecycle_status"] == "APPROVED"
    assert result["best_candidate"]["meta"]["lifecycle_version"] == "2.0.0"


def test_lifecycle_read_error_fails_closed_before_strategy_evaluation():
    lifecycle = _LifecycleRegistry(error=RuntimeError("corrupt registry"))
    selector, evaluator = _selector(enabled=True, lifecycle_registry=lifecycle)

    result = _select(selector)

    assert evaluator.calls == 0
    assert result["rejected"][0]["reason"] == "strategy_lifecycle_registry_error"
