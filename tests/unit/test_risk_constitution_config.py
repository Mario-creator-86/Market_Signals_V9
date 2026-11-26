import json

from core.config import DEFAULT_CONFIG
from core.portfolio.constraints import RiskConstitution


def test_default_and_example_configs_preserve_unconfirmed_null_owner_template():
    expected_fields = set(RiskConstitution.__dataclass_fields__)
    default = DEFAULT_CONFIG["risk_constitution"]
    with open("config.example.json", "r", encoding="utf-8") as handle:
        example = json.load(handle)["risk_constitution"]

    for template in (default, example):
        assert set(template) == expected_fields
        assert template["owner_confirmed"] is False
        assert all(
            value is None
            for name, value in template.items()
            if name != "owner_confirmed"
        )
