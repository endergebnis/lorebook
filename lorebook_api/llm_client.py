"""
Async llama.cpp client — v2 with tool calling support.
ponytail: single endpoint per client, worker pool owns multi-instance routing.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

import aiohttp
from pydantic import BaseModel, ValidationError

from .models import LorebookConfig

logger = logging.getLogger(__name__)


class LLMClient:
    """Thin async wrapper around a single llama.cpp / OpenAI-compatible endpoint."""

    def __init__(self, config: LorebookConfig) -> None:
        self._completions = config.llm_base_url.rstrip("/") + "/chat/completions"
        self._temperature = config.temperature
        self._model = config.model_name
        self._session: Optional[aiohttp.ClientSession] = None

    async def start(self) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300))

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    @property
    def model_name(self) -> str:
        return self._model

    @model_name.setter
    def model_name(self, value: str) -> None:
        self._model = value

    # ------------------------------------------------------------------
    # Low-level chat
    # ------------------------------------------------------------------

    async def _chat_raw(
        self,
        messages: list[dict[str, Any]],
        *,
        json_schema: Optional[dict] = None,
        tools: Optional[list[dict]] = None,
        tool_choice: Optional[str] = None,
        max_tokens: int = 4096,
        temperature: Optional[float] = None,
    ) -> dict[str, Any]:
        """Send a chat request, return the assistant message dict."""
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature if temperature is not None else self._temperature,
            "stream": False,
        }

        if json_schema:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "response", "strict": True, "schema": json_schema},
            }
        if tools:
            payload["tools"] = tools
        if tool_choice:
            payload["tool_choice"] = tool_choice

        async with self._session.post(self._completions, json=payload) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"llama.cpp {resp.status}: {body[:500]}")
            data = await resp.json()
            return data["choices"][0]["message"]

    # ------------------------------------------------------------------
    # Structured extraction (no tools)
    # ------------------------------------------------------------------

    async def extract_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        output_model: type[BaseModel],
        *,
        max_tokens: int = 4096,
    ) -> BaseModel:
        """Plain JSON-schema extraction — no tool loop."""
        json_schema = output_model.model_json_schema()
        msg = await self._chat_raw(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            json_schema=json_schema,
            max_tokens=max_tokens,
        )
        try:
            return output_model.model_validate(json.loads(msg["content"]))
        except (json.JSONDecodeError, ValidationError):
            logger.warning("JSON parse failed, retrying with raw parse")
            return output_model.model_validate(json.loads(msg["content"]))

    # ------------------------------------------------------------------
    # Tool-assisted extraction (with tool loop)
    # ------------------------------------------------------------------

    async def extract_with_tools(
        self,
        system_prompt: str,
        user_prompt: str,
        output_model: type[BaseModel],
        tools: list[dict],
        tool_executor,  # async callable: (name, args) -> dict
        *,
        max_turns: int = 5,
        max_tokens: int = 4096,
        progress_cb=None,  # async callable(dict) for SSE streaming
    ) -> BaseModel:
        """Extract entities with tool calling loop.

        The LLM can call search_entities before emitting JSON output.
        Tool results are fed back as 'tool' role messages.
        """
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        json_schema = output_model.model_json_schema()

        for turn in range(max_turns):
            msg = await self._chat_raw(
                messages=messages,
                tools=tools,
                json_schema=json_schema,
                max_tokens=max_tokens,
            )

            # If LLM emitted content (final answer), parse it
            if msg.get("content"):
                try:
                    parsed = output_model.model_validate(json.loads(msg["content"]))
                    if progress_cb:
                        progress_cb({
                            "type": "llm_output",
                            "turn": turn + 1,
                            "entities": [e.name for e in parsed.entities],
                            "events": len(parsed.events),
                        })
                    return parsed
                except (json.JSONDecodeError, ValidationError):
                    logger.warning("Tool-extraction JSON parse failed on turn %d", turn)
                    if progress_cb:
                        progress_cb({"type": "llm_retry", "turn": turn + 1, "reason": "Invalid JSON"})
                    messages.append(msg)
                    messages.append({
                        "role": "user",
                        "content": "Output was not valid JSON. Please retry with correct format.",
                    })
                    continue

            # If LLM wants to call a tool
            tool_calls = msg.get("tool_calls", [])
            if not tool_calls:
                logger.warning("Empty response on turn %d, retrying", turn)
                messages.append({"role": "user", "content": "Please continue extraction."})
                continue

            # Notify about tool calls
            tool_names = [tc["function"]["name"] for tc in tool_calls]
            if progress_cb:
                progress_cb({"type": "llm_tool_call", "turn": turn + 1, "tools": tool_names})

            messages.append(msg)

            # Execute each tool call
            for tc in tool_calls:
                func = tc["function"]
                tool_name = func["name"]
                try:
                    args = json.loads(func["arguments"])
                except json.JSONDecodeError:
                    args = {}
                result = await tool_executor(tool_name, args)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": json.dumps(result, ensure_ascii=False),
                })
                if progress_cb:
                    matches = result.get("results", [{}])[0].get("matches", []) if result.get("results") else []
                    progress_cb({
                        "type": "llm_tool_result",
                        "tool": tool_name,
                        "found": len(matches),
                        "names": [m["name"] for m in matches[:3]],
                    })
                logger.debug("Tool %s → %s", tool_name, result.get("any_matches", "ok"))

        raise RuntimeError(f"Tool extraction exceeded {max_turns} turns")
