"""
Entity extractor — single async function with tool-assisted dedup.
ponytail: class deleted, one function. prompt loaded from .md file.
"""

from __future__ import annotations

import logging

from .llm_client import LLMClient
from .models import ExtractorOutput
from .prompt_service import load_prompt

logger = logging.getLogger(__name__)


async def extract_chunk(
    client: LLMClient,
    chunk_text: str,
    tools: list[dict] | None = None,
    tool_executor=None,
    progress_cb=None,
) -> ExtractorOutput:
    """Extract entities & events from a text chunk.

    If tools + tool_executor provided, uses tool-assisted extraction
    (LLM calls search_entities before emitting JSON). Otherwise falls
    back to plain JSON-schema extraction.
    """
    system_prompt = load_prompt("extraction")

    if tools and tool_executor:
        return await client.extract_with_tools(
            system_prompt=system_prompt,
            user_prompt=chunk_text,
            output_model=ExtractorOutput,
            tools=tools,
            tool_executor=tool_executor,
            max_turns=5,
            max_tokens=4096,
            progress_cb=progress_cb,
        )
    else:
        return await client.extract_structured(
            system_prompt=system_prompt,
            user_prompt=chunk_text,
            output_model=ExtractorOutput,
            max_tokens=4096,
        )
