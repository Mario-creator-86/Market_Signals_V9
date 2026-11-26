"""Phase 4B config + re-export contract tests."""
from core.config import DEFAULT_CONFIG
import core.ai as ai


def test_ai_decision_config_block_present_with_spec_defaults():
    decision = DEFAULT_CONFIG["ai"]["decision"]
    # §42 strict_schema (reject AI authoritative numbers, not silently strip)
    assert decision["strict_schema"] is False
    # §44 critic (wired in 4C; config lands here so the pipeline can read it)
    assert decision["critic_enabled"] is True
    assert decision["critic_force_no_trade_on_fail"] is True


def test_ai_module_reexports_4b_symbols():
    for name in [
        "Action", "Decision", "parse_decision", "AUTHORITATIVE_FIELDS",
        "ALLOWED_ACTIONS", "DecisionSchemaError",
        "ValidationResult", "StageResult", "run_pipeline",
        "PARSE", "SCHEMA", "SEMANTIC", "AUTHORITATIVE_DATA_CHECK", "POLICY", "RISK_GATE",
        "STAGE_ORDER", "default_services",
    ]:
        assert hasattr(ai, name), f"core.ai must re-export {name}"