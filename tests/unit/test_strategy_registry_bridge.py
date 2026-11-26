from core.strategy_registry import StrategyRegistry, StrategyStatus
from core.strategy_registry.bridge import record_from_v2_spec, seed_v2_registry
from core.strategy_v2.registry import build_default_registry


NOW = "2026-08-13T08:00:00+00:00"


def test_each_v2_spec_maps_to_a_complete_research_record():
    executable = build_default_registry()
    records = [record_from_v2_spec(spec, created_at=NOW) for spec in executable.specs]

    assert len(records) == 8
    assert {record.strategy_id for record in records} == {spec.id for spec in executable.specs}
    assert all(record.version == "2.0.0" for record in records)
    assert all(record.status is StrategyStatus.RESEARCH for record in records)
    assert all(record.features and record.entry_rules and record.exit_rules for record in records)
    assert all(record.created_at == NOW for record in records)


def test_bridge_preserves_universe_features_rules_and_parameters():
    spec = build_default_registry().specs[-1]
    record = record_from_v2_spec(spec, created_at=NOW)

    assert record.universe == tuple(sorted(spec.asset_classes))
    assert record.features == tuple(spec.features_required)
    assert record.entry_rules == spec.entry_rules
    assert record.exit_rules == spec.exit_rules
    assert record.to_dict()["parameters"] == spec.params_default_by_asset
    assert spec.notes in record.description


def test_bridge_uses_explicit_horizons_for_existing_strategies():
    records = {
        spec.id: record_from_v2_spec(spec, created_at=NOW)
        for spec in build_default_registry().specs
    }

    assert records["S1_TREND_PULLBACK"].horizon == "SWING"
    assert records["S2_BREAKOUT_RETEST"].horizon == "SHORT_TERM"
    assert records["S3_VOL_SQUEEZE"].horizon == "SWING"
    assert records["S4_MEAN_REVERSION"].horizon == "SWING"
    assert records["S5_MOMENTUM_CONTINUATION"].horizon == "SHORT_TERM"
    assert records["S6_FX_SESSION_BREAKOUT"].horizon == "SHORT_TERM"
    assert records["S7_LIQUIDITY_SWEEP_REVERSAL"].horizon == "SHORT_TERM"
    assert records["S8_SUPERTREND_BOS"].horizon == "SWING"


def test_seed_registers_all_specs_once_without_inventing_approval(tmp_path):
    lifecycle = StrategyRegistry(tmp_path / "strategy_registry.json")

    first = seed_v2_registry(lifecycle, created_at=NOW)
    second = seed_v2_registry(lifecycle, created_at=NOW)

    assert len(first) == 8
    assert second == []
    assert len(lifecycle.list_records()) == 8
    assert all(record.status is StrategyStatus.RESEARCH for record in lifecycle.list_records())
    assert len([event for event in lifecycle.history() if event.action == "REGISTER"]) == 8


def test_seed_preserves_an_existing_lifecycle_record(tmp_path):
    lifecycle = StrategyRegistry(tmp_path / "strategy_registry.json")
    spec = build_default_registry().specs[0]
    existing = lifecycle.register(record_from_v2_spec(spec, created_at=NOW))

    created = seed_v2_registry(lifecycle, created_at=NOW)

    assert len(created) == 7
    assert lifecycle.get(existing.key) == existing
