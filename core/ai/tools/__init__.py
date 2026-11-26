"""§39 AI tool architecture — Tool/ToolRegistry + the 28 allow-listed tools (§40)."""
from core.ai.tools.tool import Tool, ToolResult
from core.ai.tools.registry import SPEC_TOOL_NAMES, ToolRegistry, build_default_registry
from core.ai.tools.loop import run_tool_loop

__all__ = [
    "SPEC_TOOL_NAMES",
    "Tool",
    "ToolResult",
    "ToolRegistry",
    "build_default_registry",
    "run_tool_loop",
]