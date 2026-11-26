"""OpenAICompatibleProvider — OpenAI Chat Completions shape for xAI/DeepSeek/Mistral/OpenRouter (§35).

Same wire shape as OpenAIProvider (config-driven base_url + api_key_env); the type
registry uses this class for config type "openai_compatible".
"""
from __future__ import annotations

from core.ai.openai_provider import OpenAIProvider


class OpenAICompatibleProvider(OpenAIProvider):
    """Identical wire shape to OpenAI; distinguished by config `type: openai_compatible`."""