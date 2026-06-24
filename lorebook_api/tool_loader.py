"""Dynamic tool loader — loads Python tool modules, supports async handlers."""

from __future__ import annotations

import importlib.util
import inspect
import logging
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)


class ToolLoader:
    """Loads tool .py files from tools/ directory."""

    def __init__(self, tools_dir: str = "tools") -> None:
        self._dir = Path(__file__).parent.parent / tools_dir
        self._tools: dict[str, dict] = {}
        self._handlers: dict[str, Callable] = {}

    @property
    def names(self) -> list[str]:
        return list(self._tools.keys())

    def load_all(self) -> list[dict]:
        if not self._dir.exists():
            logger.warning("Tools dir not found: %s", self._dir)
            return []
        for f in sorted(self._dir.glob("*.py")):
            if f.name.startswith("_"):
                continue
            try:
                self._load_one(f)
            except Exception:
                logger.exception("Failed loading tool %s", f.name)
        logger.info("Loaded %d tools: %s", len(self._tools), list(self._tools))
        return list(self._tools.values())

    def _load_one(self, path: Path) -> None:
        spec = importlib.util.spec_from_file_location(f"tools.{path.stem}", path)
        if not spec or not spec.loader:
            return
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if hasattr(mod, "get_tool"):
            tool_def = mod.get_tool()
            name = tool_def["function"]["name"]
            self._handlers[name] = tool_def.pop("handler", None)
            self._tools[name] = tool_def

    async def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Execute a tool, awaiting if handler is async. Always returns {success, ...}."""
        handler = self._handlers.get(name)
        if not handler:
            return {"success": False, "error": f"Tool '{name}' not found"}
        try:
            result = handler(**arguments)
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, dict):
                result.setdefault("success", "error" not in result)
                return result
            return {"success": True, "result": result}
        except Exception as e:
            return {"success": False, "error": f"{type(e).__name__}: {e}"}

    def get_definitions(self) -> list[dict]:
        """Ollama-format tool definitions (no handler)."""
        return [{"type": "function", "function": t["function"]} for t in self._tools.values()]
