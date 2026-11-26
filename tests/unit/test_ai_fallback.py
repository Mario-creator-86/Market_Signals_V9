from core.ai.fallback import all_unavailable, generate_with_fallback, no_new_autonomous_trade_guard
from core.ai.provider import AIResponse, UNAVAILABLE


class _FakeProvider:
    def __init__(self, name, *, fail=False, text="ok", model="m"):
        self.name = name
        self._fail = fail
        self._text = text
        self.default_model = model
        self.calls = 0

    def generate(self, prompt, *, model=None, system="", **opts):
        self.calls += 1
        if self._fail:
            return AIResponse(text="", error=f"{self.name} unavailable")
        return AIResponse(text=self._text, model=model or self.default_model)


def test_primary_serves_when_healthy():
    primary = _FakeProvider("openai", text="primary-answer")
    resp = generate_with_fallback(primary, "hello", model="gpt-4o")
    assert resp.text == "primary-answer"
    assert resp.is_error is False
    assert resp.model == "gpt-4o"


def test_fallback_chain_serves_when_primary_fails_gold013():
    primary = _FakeProvider("openai", fail=True)
    fb1 = _FakeProvider("anthropic", fail=True)
    fb2 = _FakeProvider("gemini", text="gemini-answer")
    resp = generate_with_fallback(primary, "hello", model="gpt-4o", fallback_chain=[fb1, fb2])
    assert resp.text == "gemini-answer"
    assert resp.is_error is False
    assert primary.calls == 1 and fb1.calls == 1 and fb2.calls == 1


def test_all_fail_returns_unavailable_sentinel_gold013():
    primary = _FakeProvider("openai", fail=True)
    fb = _FakeProvider("anthropic", fail=True)
    resp = generate_with_fallback(primary, "hello", model="gpt-4o", fallback_chain=[fb])
    assert all_unavailable(resp) is True
    assert resp is UNAVAILABLE


def test_no_fallback_chain_primary_fail_returns_unavailable():
    primary = _FakeProvider("openai", fail=True)
    resp = generate_with_fallback(primary, "hello", model="gpt-4o", fallback_chain=[])
    assert all_unavailable(resp) is True


def test_all_unavailable_recognizes_sentinel_and_equivalent_error():
    assert all_unavailable(UNAVAILABLE) is True
    assert all_unavailable(AIResponse(text="", error="all providers unavailable")) is True
    assert all_unavailable(AIResponse(text="ok")) is False
    assert all_unavailable(AIResponse(text="", error="some other error")) is False


def test_guard_blocks_when_all_unavailable_and_flag_set_gold014():
    config = {"ai": {"all_unavailable_no_trade": True}}
    assert no_new_autonomous_trade_guard(UNAVAILABLE, config) is True


def test_guard_does_not_block_when_ai_available_gold014():
    config = {"ai": {"all_unavailable_no_trade": True}}
    assert no_new_autonomous_trade_guard(AIResponse(text="ok"), config) is False


def test_guard_does_not_block_when_flag_disabled():
    config = {"ai": {"all_unavailable_no_trade": False}}
    assert no_new_autonomous_trade_guard(UNAVAILABLE, config) is False


def test_guard_defaults_to_block_when_flag_missing_is_treated_true():
    # Spec default (3A config) is True; missing key must not silently allow trades.
    config = {"ai": {}}
    assert no_new_autonomous_trade_guard(UNAVAILABLE, config) is True


def test_fallback_chain_empty_and_primary_ok_no_extra_calls():
    primary = _FakeProvider("openai", text="ok")
    generate_with_fallback(primary, "hello", model="m", fallback_chain=[])
    assert primary.calls == 1