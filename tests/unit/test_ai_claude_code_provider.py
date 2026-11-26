"""Claude through the owner's Claude subscription (Claude Code CLI, headless)."""
from __future__ import annotations

import json
import subprocess

from core.ai.claude_code_provider import ClaudeCodeProvider
from core.bounded_process import BoundedProcessTimeout


class _Runner:
    """Stands in for the CLI: records the call, returns a canned result."""

    def __init__(self, payload=None, *, returncode=0, stdout=None, stderr="", raises=None):
        self.payload = payload
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.raises = raises
        self.calls = []

    def __call__(self, cmd, *, input_text=None, timeout=60.0, cwd=None, env=None):
        self.calls.append({"cmd": list(cmd), "input_text": input_text, "timeout": timeout,
                           "cwd": cwd, "env": env})
        if self.raises is not None:
            raise self.raises
        out = self.stdout if self.stdout is not None else json.dumps(self.payload)
        return subprocess.CompletedProcess(list(cmd), self.returncode, out, self.stderr)


def _ok(text="--- a/x.py\n+++ b/x.py\n", model="claude-opus-5-5"):
    return {
        "type": "result", "subtype": "success", "is_error": False, "result": text,
        "usage": {"input_tokens": 400, "output_tokens": 90},
        "modelUsage": {model: {}},
    }


def _provider(runner, **kwargs):
    return ClaudeCodeProvider(executable="C:/bin/claude.exe", default_model="claude-opus-5-5",
                              runner=runner, **kwargs)


def test_generate_returns_the_cli_result_and_usage():
    runner = _Runner(_ok())
    provider = _provider(runner)

    resp = provider.generate("Fix the overflow \u2192 card", model="claude-opus-5-5",
                             system="Answer with the unified diff ONLY.")

    assert resp.error is None
    assert resp.text == "--- a/x.py\n+++ b/x.py\n"
    assert resp.model == "claude-opus-5-5"
    assert (resp.usage.prompt_tokens, resp.usage.completion_tokens, resp.usage.total_tokens) == (400, 90, 490)
    assert provider.usage().total_tokens == 490


def test_cli_call_is_headless_tool_less_and_isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "would-bill-the-api-instead-of-the-subscription")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "x")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:9")
    runner = _Runner(_ok())
    provider = _provider(runner, workdir=tmp_path / "cli")

    provider.generate("PROMPT BODY", model="claude-opus-5-5", system="SYSTEM TEXT", timeout_override=300)

    call = runner.calls[0]
    cmd = call["cmd"]
    assert cmd[0] == "C:/bin/claude.exe" and "-p" in cmd
    assert cmd[cmd.index("--model") + 1] == "claude-opus-5-5"
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert cmd[cmd.index("--tools") + 1] == ""                # no tools: text in, text out
    assert cmd[cmd.index("--setting-sources") + 1] == ""      # no user/project settings or hooks
    assert "--strict-mcp-config" in cmd and "--no-session-persistence" in cmd
    assert cmd[cmd.index("--system-prompt") + 1] == "SYSTEM TEXT"
    assert call["input_text"] == "PROMPT BODY"                # prompt goes over stdin, never argv
    assert "PROMPT BODY" not in cmd
    assert call["timeout"] == 300
    assert call["cwd"] == str(tmp_path / "cli") and (tmp_path / "cli").is_dir()
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        assert name not in call["env"]


def test_default_model_is_used_when_none_is_given():
    runner = _Runner(_ok())
    _provider(runner).generate("hi")
    cmd = runner.calls[0]["cmd"]
    assert cmd[cmd.index("--model") + 1] == "claude-opus-5-5"


def test_cli_error_result_becomes_an_error_response():
    runner = _Runner({"type": "result", "subtype": "success", "is_error": True,
                      "result": "Claude AI usage limit reached|1795400000"})
    resp = _provider(runner).generate("hi")
    assert resp.is_error and "usage limit" in resp.error and resp.text == ""


def test_timeout_missing_cli_and_garbage_output_are_errors_not_exceptions():
    timed_out = _provider(_Runner(raises=BoundedProcessTimeout("claude timed out after 300s"))).generate("hi")
    assert timed_out.is_error and "timed out" in timed_out.error

    missing = ClaudeCodeProvider(executable="C:/nope/claude.exe", runner=_Runner(raises=FileNotFoundError("x")))
    assert missing.generate("hi").is_error

    garbage = _provider(_Runner(stdout="not json", returncode=1, stderr="boom")).generate("hi")
    assert garbage.is_error and "boom" in garbage.error


def test_serves_only_claude_models():
    provider = _provider(_Runner(_ok()))
    assert provider.serves_model("claude-opus-5-5") and provider.serves_model("opus")
    assert not provider.serves_model("kimi-k3:cloud")
    assert not provider.serves_model("") and not provider.serves_model(None)


def test_structured_generation_parses_the_json_object():
    runner = _Runner(_ok(text='Here you go:\n{"verdict": "PASSED", "findings": []}'))
    resp = _provider(runner).generate_structured("review", {"type": "object"})
    assert resp.error is None and resp.raw["json"] == {"verdict": "PASSED", "findings": []}

    bad = _provider(_Runner(_ok(text="no json here"))).generate_structured("review", {})
    assert bad.is_error


def test_tool_calls_are_refused_and_capabilities_say_so():
    provider = _provider(_Runner(_ok()))
    assert provider.tool_call("x", [{"name": "t"}]).is_error
    caps = provider.capabilities()
    assert caps.text and caps.structured_output and not caps.tool_calling and not caps.streaming
    assert provider.list_models() == ["claude-opus-5-5"]


def test_build_provider_constructs_the_subscription_provider():
    from api.ai_config_service import build_provider

    cfg = {"ai": {"providers": {"claude_subscription": {
        "enabled": True, "type": "claude_code", "default_model": "claude-opus-5-5", "timeout": 240,
    }}}}
    provider = build_provider(cfg, "claude_subscription")
    assert isinstance(provider, ClaudeCodeProvider)
    assert provider.default_model == "claude-opus-5-5" and provider.timeout == 240

    cfg["ai"]["providers"]["claude_subscription"]["enabled"] = False
    assert build_provider(cfg, "claude_subscription") is None


def test_default_config_ships_the_provider_disabled():
    from core.config import DEFAULT_CONFIG

    entry = DEFAULT_CONFIG["ai"]["providers"]["claude_subscription"]
    assert entry["type"] == "claude_code" and entry["enabled"] is False
    assert entry["default_model"] == "claude-opus-5-5"


def test_repair_role_with_a_claude_model_uses_the_subscription_provider():
    from core.development_factory import auto_repair as ar

    cfg = {"ai": {
        "providers": {
            "ollama": {"enabled": True, "type": "ollama"},
            "claude_subscription": {"enabled": True, "type": "claude_code",
                                    "default_model": "claude-opus-5-5"},
        },
        "assignment": {"coding_agent": "claude-opus-5-5", "code_reviewer": "kimi-k3:cloud",
                       "security_reviewer": "kimi-k3:cloud"},
    }}

    roles = ar._role_services(cfg, None)

    assert isinstance(roles["coding"][0], ClaudeCodeProvider) and roles["coding"][1] == "claude-opus-5-5"
    # the independent reviewers stay on the other provider
    assert not isinstance(roles["code_review"][0], ClaudeCodeProvider)
    assert roles["code_review"][1] == "kimi-k3:cloud"


def test_repair_role_with_another_model_never_lands_on_the_subscription_provider():
    from core.development_factory import auto_repair as ar

    cfg = {"ai": {
        "providers": {
            "claude_subscription": {"enabled": True, "type": "claude_code",
                                    "default_model": "claude-opus-5-5"},
            "ollama": {"enabled": True, "type": "ollama"},
        },
        "assignment": {"coding_agent": "kimi-k3:cloud"},
    }}

    provider, model = ar._role_services(cfg, None)["coding"]

    assert model == "kimi-k3:cloud" and not isinstance(provider, ClaudeCodeProvider)


# --- the subscription never becomes a generic primary or fallback ---------------

_MIXED = {"ai": {
    "providers": {
        "claude_subscription": {"enabled": True, "type": "claude_code",
                                "default_model": "claude-opus-5-5"},
        "ollama": {"enabled": True, "type": "ollama"},
    },
    "assignment": {"all": "deepseek-v4-pro:cloud", "risk_critic": "kimi-k3:cloud"},
}}


def test_general_provider_names_skip_model_bound_providers():
    from core.ai.provider import general_provider_names

    assert general_provider_names(_MIXED) == ["ollama"]
    assert general_provider_names({}) == [] and general_provider_names(None) == []


def test_trading_decisions_never_use_or_fall_back_to_the_subscription():
    from core.ai.decision_engine import _build_providers

    primary, model, fallback = _build_providers(_MIXED, {})

    assert not isinstance(primary, ClaudeCodeProvider) and model == "deepseek-v4-pro:cloud"
    assert not any(isinstance(p, ClaudeCodeProvider) for p in fallback)


def test_an_explicit_fallback_chain_may_still_name_the_subscription():
    from core.ai.decision_engine import _build_providers

    cfg = {"ai": dict(_MIXED["ai"], fallback={"enabled": True, "chain": ["claude_subscription"]})}
    _primary, _model, fallback = _build_providers(cfg, {})
    assert [type(p) for p in fallback] == [ClaudeCodeProvider]


def test_trade_critic_never_lands_on_the_subscription():
    from core.ai.critic import _resolve_critic_provider

    provider, model = _resolve_critic_provider(_MIXED, {})
    assert model == "kimi-k3:cloud" and not isinstance(provider, ClaudeCodeProvider)


def test_subscription_cannot_be_selected_as_the_global_trading_ai():
    import pytest
    from api.ai_config_service import set_global_ai_provider

    with pytest.raises(ValueError, match="per role"):
        set_global_ai_provider(_MIXED, "claude_subscription", "claude-opus-5-5")
