"""§45 autonomy modes + §74 kill switch + §48 no-weakening guard."""
import pytest

from core.autonomy import (
    ALLOWED_MODES, PROTECTED_TARGETS, AutonomyMode, AutonomyViolationError,
    assert_not_weakening, kill_switch_active, live_auto_owner_enabled,
    mode_allows_live_orders, mode_allows_paper_orders, resolve_mode,
)


def test_autonomy_mode_six_values_and_allowed():
    assert {m.value for m in AutonomyMode} == {
        "OFF", "ANALYZE", "RECOMMEND", "PAPER_AUTO", "LIVE_ASSISTED", "LIVE_AUTO",
    }
    assert ALLOWED_MODES == frozenset(m.value for m in AutonomyMode)


def test_resolve_mode_default_and_unknown_is_analyze_never_live_auto():
    assert resolve_mode({}) is AutonomyMode.ANALYZE
    assert resolve_mode({"autonomy": {}}) is AutonomyMode.ANALYZE
    assert resolve_mode({"autonomy": {"mode": "nonsense"}}) is AutonomyMode.ANALYZE
    assert resolve_mode({"autonomy": {"mode": "LIVE_AUTO"}}) is AutonomyMode.LIVE_AUTO
    assert resolve_mode({"autonomy": {"mode": "paper_auto"}}) is AutonomyMode.PAPER_AUTO


def test_mode_order_permissions_truth_table():
    assert mode_allows_paper_orders(AutonomyMode.OFF) is False
    assert mode_allows_paper_orders(AutonomyMode.ANALYZE) is False
    assert mode_allows_paper_orders(AutonomyMode.RECOMMEND) is False
    assert mode_allows_paper_orders(AutonomyMode.PAPER_AUTO) is True
    assert mode_allows_paper_orders(AutonomyMode.LIVE_ASSISTED) is True
    assert mode_allows_paper_orders(AutonomyMode.LIVE_AUTO) is True
    assert mode_allows_live_orders(AutonomyMode.PAPER_AUTO) is False
    assert mode_allows_live_orders(AutonomyMode.LIVE_ASSISTED) is False
    assert mode_allows_live_orders(AutonomyMode.LIVE_AUTO) is True


def test_kill_switch_and_owner_flag_default_false():
    assert kill_switch_active({}) is False
    assert kill_switch_active({"autonomy": {"kill_switch_active": True}}) is True
    assert live_auto_owner_enabled({}) is False
    assert live_auto_owner_enabled({"autonomy": {"live_auto_owner_enabled": True}}) is True


def test_assert_not_weakening_raises_for_every_protected_target():
    assert PROTECTED_TARGETS == frozenset({
        "risk_constitution", "kill_switch", "promotion_gates",
        "critical_tests", "live_permissions", "holdout_policy",
    })
    for target in PROTECTED_TARGETS:
        with pytest.raises(AutonomyViolationError):
            assert_not_weakening(target, "disable")


def test_assert_not_weakening_passes_for_non_protected_non_weaken_and_owner():
    assert_not_weakening("signal", "disable")  # not protected
    assert_not_weakening("kill_switch", "read")  # protected but not a weaken verb
    assert_not_weakening("kill_switch", "disable", owner_authorized=True)  # owner bypass


def test_config_default_autonomy_block_backfills():
    from core.config import DEFAULT_CONFIG, deep_merge
    auto = DEFAULT_CONFIG["autonomy"]
    assert auto["mode"] == "ANALYZE"
    assert auto["kill_switch_active"] is False
    assert auto["live_auto_owner_enabled"] is False
    assert auto["paper_auto_acceptance_required"] is True
    # deep_merge backfills the block when a loaded config lacks the key (§45/§46/§74)
    merged = deep_merge(DEFAULT_CONFIG, {"signal_settings": {}})
    assert merged["autonomy"]["mode"] == "ANALYZE"