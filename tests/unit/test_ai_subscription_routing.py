"""Claude models chosen anywhere in the app are served by the owner's Claude subscription.

Nearly every AI call goes through the legacy Ollama client. A model id that
belongs to the Claude subscription provider is routed there at that seam; when
the subscription cannot answer (limit reached, CLI missing, daily cap) the call
falls back to the configured Ollama model and says so.
"""
from __future__ import annotations

import json
import subprocess

import pytest

import core.ai.subscription_routing as routing
import core.ollama_client as oc
from core.ai.claude_code_provider import ClaudeCodeProvider


class _FakeResp:
    def __init__(self, body, status=200):
        self.status_code = status
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


class _Runner:
    def __init__(self, text="CLAUDE ANSWER", *, is_error=False):
        self.text, self.is_error, self.calls = text, is_error, []

    def __call__(self, cmd, *, input_text=None, timeout=60.0, cwd=None, env=None):
        self.calls.append({"cmd": list(cmd), "input_text": input_text, "timeout": timeout})
        payload = {"type": "result", "subtype": "success", "is_error": self.is_error, "result": self.text,
                   "usage": {"input_tokens": 10, "output_tokens": 5}}
        return subprocess.CompletedProcess(list(cmd), 0, json.dumps(payload), "")


PROVIDERS = {
    "ollama": {"enabled": True, "type": "ollama"},
    "claude_subscription": {
        "enabled": True, "type": "claude_code", "executable": "C:/bin/claude.exe",
        "default_model": "claude-opus-5-5", "models": ["claude-opus-5-5", "claude-fable-5-2"],
        "fallback_model": "deepseek-v4-pro:cloud", "daily_call_limit": 0, "max_concurrent": 2,
    },
}


@pytest.fixture()
def wired(monkeypatch):
    """Subscription provider with a fake CLI; Ollama HTTP recorded, never real."""
    runner = _Runner()
    state = {"providers": json.loads(json.dumps(PROVIDERS)), "posts": [], "preferred": "claude-opus-5-5"}
    routing.reset_for_tests()
    monkeypatch.setattr(routing, "_providers_config", lambda: state["providers"])
    monkeypatch.setattr(routing, "_runner_override", runner, raising=False)
    monkeypatch.setattr(routing, "_global_ollama_model", lambda: "")   # hermetic: no machine config
    monkeypatch.setattr(ClaudeCodeProvider, "health_check", lambda self, force=False: True)

    def fake_post(url, *, provider=None, json=None, **kwargs):
        state["posts"].append({"url": url, "model": (json or {}).get("model")})
        return _FakeResp({"message": {"content": "OLLAMA ANSWER"}, "eval_count": 3})

    monkeypatch.setattr(oc, "http_post", fake_post)
    client = oc.OllamaClient()
    monkeypatch.setattr(client, "refresh_config", lambda: None)
    monkeypatch.setattr(client, "get_preferred_model", lambda: state["preferred"])
    monkeypatch.setattr(client, "get_models", lambda force=False: ["deepseek-v4-pro:cloud", "kimi-k3:cloud"])
    state.update(runner=runner, client=client)
    yield state
    routing.reset_for_tests()


def test_claude_model_is_answered_by_the_subscription_not_by_ollama(wired):
    out = wired["client"].generate("Summarise the tape", model="claude-opus-5-5", system="Be brief.")

    assert out["response"] == "CLAUDE ANSWER" and "error" not in out
    assert out["_meta"]["provider"] == "claude_subscription" and out["_meta"]["model"] == "claude-opus-5-5"
    assert wired["posts"] == []                                   # Ollama was not called
    call = wired["runner"].calls[0]
    assert call["input_text"] == "Summarise the tape"
    assert call["cmd"][call["cmd"].index("--model") + 1] == "claude-opus-5-5"


def test_global_claude_model_is_the_best_model_and_default_for_calls(wired):
    assert wired["client"].get_best_model() == "claude-opus-5-5"   # not "first model Ollama lists"

    out = wired["client"].generate("hello")                        # no model given: the global one

    assert out["_meta"]["provider"] == "claude_subscription" and wired["posts"] == []


def test_other_models_still_go_to_ollama(wired):
    out = wired["client"].generate("hello", model="kimi-k3:cloud")

    assert out["response"] == "OLLAMA ANSWER"
    assert wired["posts"][0]["model"] == "kimi-k3:cloud" and wired["runner"].calls == []


def test_disabled_provider_routes_nothing(wired):
    wired["providers"]["claude_subscription"]["enabled"] = False
    wired["preferred"] = "deepseek-v4-pro:cloud"
    routing.reset_for_tests()

    assert routing.subscription_provider_for("claude-opus-5-5") is None
    assert wired["client"].get_best_model() == "deepseek-v4-pro:cloud"


def test_subscription_error_falls_back_to_the_configured_ollama_model(wired):
    wired["runner"].is_error = True
    wired["runner"].text = "Claude AI usage limit reached|1795400000"

    out = wired["client"].generate("hello", model="claude-fable-5-2")

    assert out["response"] == "OLLAMA ANSWER"
    assert wired["posts"][0]["model"] == "deepseek-v4-pro:cloud"
    meta = out["_meta"]
    assert meta["requested_model"] == "claude-fable-5-2" and meta["model"] == "deepseek-v4-pro:cloud"
    assert "usage limit" in meta["subscription_fallback_reason"]


def test_without_a_fallback_model_the_error_is_returned(wired):
    wired["providers"]["claude_subscription"]["fallback_model"] = ""
    routing.reset_for_tests()
    wired["runner"].is_error = True
    wired["runner"].text = "not logged in"

    out = wired["client"].generate("hello", model="claude-opus-5-5")

    assert "not logged in" in out["error"] and wired["posts"] == []


def test_daily_call_limit_sends_the_rest_of_the_day_to_the_fallback(wired):
    wired["providers"]["claude_subscription"]["daily_call_limit"] = 2
    routing.reset_for_tests()

    providers = [wired["client"].generate("q", model="claude-opus-5-5")["_meta"].get("provider") for _ in range(4)]

    assert providers[:2] == ["claude_subscription", "claude_subscription"]
    assert len(wired["runner"].calls) == 2 and len(wired["posts"]) == 2
    usage = routing.subscription_usage()
    assert usage["claude_subscription"]["calls_today"] == 2 and usage["claude_subscription"]["daily_call_limit"] == 2


def test_structured_generation_is_routed_too(wired):
    from core.ai.ollama_provider import OllamaProvider

    wired["runner"].text = '{"verdict": "PASSED"}'
    resp = OllamaProvider(client=wired["client"]).generate_structured("review", {"type": "object"}, model="claude-opus-5-5")

    assert resp.error is None and resp.raw["json"] == {"verdict": "PASSED"} and wired["posts"] == []


def test_tool_calls_use_the_fallback_model_because_the_subscription_has_no_tools(wired):
    from core.ai.ollama_provider import OllamaProvider

    OllamaProvider(client=wired["client"], http_post=lambda url, **kw: (wired["posts"].append({"model": kw["json"]["model"]})
                                                                        or _FakeResp({"message": {"content": "x"}}))
                   ).tool_call("do it", [{"type": "function", "function": {"name": "t"}}], model="claude-opus-5-5")

    assert wired["posts"][-1]["model"] == "deepseek-v4-pro:cloud"


def test_subscription_models_listed_for_the_settings_page(wired):
    assert routing.subscription_models() == ["claude-opus-5-5", "claude-fable-5-2"]
    wired["providers"]["claude_subscription"]["enabled"] = False
    routing.reset_for_tests()
    assert routing.subscription_models() == []


# --- settings endpoints -------------------------------------------------------

@pytest.fixture()
def api_wired(client, monkeypatch):
    from core import config as cfgmod

    shared = {
        "ollama": {"model": "deepseek-v4-pro:cloud", "cloud_model": "deepseek-v4-pro:cloud"},
        "ai": {
            "providers": json.loads(json.dumps(PROVIDERS)),
            "assignment": {"all": "deepseek-v4-pro:cloud", "news_analyst": "deepseek-v4-pro:cloud",
                           "risk_critic": "kimi-k3:cloud"},
        },
    }
    shared["ai"]["providers"]["claude_subscription"]["fallback_model"] = ""

    class FakeCM:
        def __init__(self):
            self.config = shared

        def save(self):
            return True

    class _FakeStat:
        st_mtime_ns = 1

    class _FakePath:
        def stat(self):
            return _FakeStat()

    routing.reset_for_tests()
    monkeypatch.setattr(cfgmod, "ConfigManager", FakeCM)
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", _FakePath())
    monkeypatch.setattr(oc, "_CONFIG_CACHE", {"mtime_ns": None, "data": {}})
    monkeypatch.setattr(routing, "_providers_config", lambda: shared["ai"]["providers"])
    monkeypatch.setattr(ClaudeCodeProvider, "health_check", lambda self, force=False: True)
    monkeypatch.setattr(oc.ollama, "check_availability", lambda *a, **kw: True)
    monkeypatch.setattr(oc.ollama, "get_models", lambda *a, **kw: ["deepseek-v4-pro:cloud", "kimi-k3:cloud"])
    monkeypatch.setattr(oc.ollama, "get_selectable_cloud_models", lambda *a, **kw: ["deepseek-v4-pro:cloud"])
    monkeypatch.setattr(oc.ollama, "is_cloud_host", lambda *a, **kw: False)
    monkeypatch.setattr(oc.ollama, "refresh_config", lambda *a, **kw: None)
    monkeypatch.setattr(oc.ollama, "pull_model", lambda *a, **kw: pytest.fail("a Claude model must not be pulled from Ollama"))
    yield client, shared
    routing.reset_for_tests()


def test_status_offers_the_subscription_models(api_wired):
    client, _shared = api_wired

    body = client.get("/api/ollama/status").json()

    assert body["subscription_models"] == ["claude-opus-5-5", "claude-fable-5-2"]
    assert "claude-fable-5-2" in body["models"]


def test_setting_a_claude_model_as_global_keeps_the_previous_model_as_fallback(api_wired):
    client, shared = api_wired

    resp = client.post("/api/ollama/set-model", json={"model": "claude-fable-5-2"})

    assert resp.status_code == 200, resp.text
    assert shared["ollama"]["model"] == "claude-fable-5-2"
    assert shared["ai"]["assignment"]["all"] == "claude-fable-5-2"
    assert shared["ai"]["assignment"]["news_analyst"] == "claude-fable-5-2"      # followed the global model
    assert shared["ai"]["assignment"]["risk_critic"] == "kimi-k3:cloud"          # the owner's own choice stays
    assert shared["ai"]["providers"]["claude_subscription"]["fallback_model"] == "deepseek-v4-pro:cloud"
    assert client.get("/api/ollama/status").json()["active_model"] == "claude-fable-5-2"


def test_switching_between_claude_models_does_not_overwrite_the_fallback(api_wired):
    client, shared = api_wired
    client.post("/api/ollama/set-model", json={"model": "claude-fable-5-2"})

    client.post("/api/ollama/set-model", json={"model": "claude-opus-5-5"})

    assert shared["ai"]["providers"]["claude_subscription"]["fallback_model"] == "deepseek-v4-pro:cloud"


def test_role_level_claude_model_falls_back_to_the_global_ollama_model(wired, monkeypatch):
    wired["providers"]["claude_subscription"]["fallback_model"] = ""
    routing.reset_for_tests()
    monkeypatch.setattr(routing, "_global_ollama_model", lambda: "deepseek-v4-pro:cloud")
    wired["runner"].is_error = True
    wired["runner"].text = "Claude AI usage limit reached"

    out = wired["client"].generate("hello", model="claude-opus-5-5")

    assert out["response"] == "OLLAMA ANSWER" and wired["posts"][0]["model"] == "deepseek-v4-pro:cloud"


def test_a_claude_global_model_is_never_its_own_fallback(wired, monkeypatch):
    wired["providers"]["claude_subscription"]["fallback_model"] = ""
    routing.reset_for_tests()
    monkeypatch.setattr(routing, "_global_ollama_model", lambda: "claude-fable-5-2")

    assert routing.fallback_model_for("claude-opus-5-5") == ""


def test_newest_installed_cli_is_chosen():
    from core.ai.claude_code_provider import newest_cli

    assert newest_cli([("C:/old/claude.exe", "2.1.289"), ("C:/new/claude.exe", "2.1.330"),
                       ("C:/mid/claude.exe", "2.1.328")]) == "C:/new/claude.exe"
    assert newest_cli([("C:/a/claude.exe", "2.1.9"), ("C:/b/claude.exe", "2.1.10")]) == "C:/b/claude.exe"
    assert newest_cli([("C:/only/claude.exe", "")]) == "C:/only/claude.exe"      # unknown version still usable
    assert newest_cli([]) == ""


def test_a_restart_does_not_hand_out_a_fresh_daily_allowance(wired, tmp_path):
    """Seen live on 2026-11-24: 2860 of 3000 calls used, the app was restarted, and it read 0 of 3000."""
    wired["providers"]["claude_subscription"]["daily_call_limit"] = 3
    routing.reset_for_tests()
    store = tmp_path / "subscription_calls.json"
    routing.remember_calls_in(store)

    for _ in range(2):
        wired["client"].generate("q", model="claude-opus-5-5")
    assert routing.subscription_usage()["claude_subscription"]["calls_today"] == 2

    routing.reset_for_tests()                 # the process ends: nothing is left in memory
    assert routing.subscription_usage()["claude_subscription"]["calls_today"] == 0
    routing.remember_calls_in(store)          # the app starts again
    assert routing.subscription_usage()["claude_subscription"]["calls_today"] == 2

    providers = [wired["client"].generate("q", model="claude-opus-5-5")["_meta"].get("provider") for _ in range(2)]
    assert providers[0] == "claude_subscription"          # the third call of the day is the last one allowed
    assert providers[1] != "claude_subscription"


def test_calls_made_before_the_count_was_restored_are_added_to_it(wired, tmp_path):
    store = tmp_path / "subscription_calls.json"
    routing.remember_calls_in(store)
    wired["client"].generate("q", model="claude-opus-5-5")
    routing.reset_for_tests()

    wired["client"].generate("q", model="claude-opus-5-5")    # an agent called before the app restored the count
    routing.remember_calls_in(store)
    routing.remember_calls_in(store)                          # restoring twice does not count twice

    assert routing.subscription_usage()["claude_subscription"]["calls_today"] == 2


def test_yesterdays_count_is_not_carried_into_today(wired, tmp_path):
    store = tmp_path / "subscription_calls.json"
    store.write_text(json.dumps({"claude_subscription": {"day": "2000-01-01", "count": 999}}), encoding="utf-8")
    routing.remember_calls_in(store)

    assert routing.subscription_usage()["claude_subscription"]["calls_today"] == 0


def test_an_unreadable_count_file_starts_from_zero_and_calls_still_work(wired, tmp_path):
    store = tmp_path / "subscription_calls.json"
    store.write_text("{ broken", encoding="utf-8")
    routing.remember_calls_in(store)

    assert wired["client"].generate("q", model="claude-opus-5-5")["_meta"].get("provider") == "claude_subscription"
    assert json.loads(store.read_text(encoding="utf-8"))["claude_subscription"]["count"] == 1
