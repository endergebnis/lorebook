"""
Pipeline orchestrator — v2: no resolver, tool-assisted extraction.
ponytail: resolve_duplicates deleted (tools handle dedup during extraction).
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from .chunker import split_markdown
from .extractor import extract_chunk
from .graph import GraphStore
from .llm_client import LLMClient
from .models import Chunk, LorebookConfig, LorebookOutput
from .prompt_service import load_prompt

logger = logging.getLogger(__name__)


class LorebookPipeline:
    """End-to-end pipeline: markdown → chunks → LLM extract → Neo4j → generate."""

    def __init__(self, config: LorebookConfig | None = None) -> None:
        self._config = config or LorebookConfig()
        self._llm = LLMClient(self._config)
        self._graph = GraphStore(self._config)
        self._semaphore = asyncio.Semaphore(self._config.concurrency)

    # -- expose for tool injection --
    @property
    def llm(self) -> LLMClient:
        return self._llm

    @property
    def graph(self) -> GraphStore:
        return self._graph

    @property
    def config(self) -> LorebookConfig:
        return self._config

    async def start(self) -> None:
        await self._llm.start()

    async def close(self) -> None:
        await self._llm.close()
        await self._graph.close()

    # ------------------------------------------------------------------
    # Phase 1 – Ingest
    # ------------------------------------------------------------------

    async def ingest_file(self, md_path: str | Path) -> list[Chunk]:
        chunks = split_markdown(
            md_path,
            chunk_tokens=self._config.chunk_size_tokens,
            overlap_tokens=self._config.chunk_overlap_tokens,
        )
        logger.info("Split %s → %d chunks", Path(md_path).name, len(chunks))

        async def _store(c: Chunk) -> None:
            await self._graph.store_chunk(c)

        await asyncio.gather(*[_store(c) for c in chunks])
        return chunks

    async def ingest_directory(self, directory: str | Path) -> list[Chunk]:
        dir_path = Path(directory)
        md_files = sorted(dir_path.glob("*.md"))
        logger.info("Ingesting %d markdown files from %s", len(md_files), dir_path)
        all_chunks: list[Chunk] = []
        for md_file in md_files:
            all_chunks.extend(await self.ingest_file(md_file))
        logger.info("Total chunks: %d", len(all_chunks))
        return all_chunks

    # ------------------------------------------------------------------
    # Phase 2 – Extract (delegated to runner for progress/pause)
    # ------------------------------------------------------------------

    async def _extract_chunk(self, chunk: Chunk, tools=None, tool_executor=None) -> None:
        async with self._semaphore:
            try:
                output = await extract_chunk(self._llm, chunk.text, tools, tool_executor)
                for entity in output.entities:
                    await self._graph.upsert_entity(entity, chunk_id=chunk.id)
                for event in output.events:
                    await self._graph.upsert_event(event, chunk_id=chunk.id)
            except Exception:
                logger.exception("Failed chunk %s", chunk.id[:8])

    async def extract_all(self, chunks: list[Chunk], tools=None, tool_executor=None) -> None:
        logger.info("Extracting from %d chunks", len(chunks))
        tasks = [self._extract_chunk(c, tools, tool_executor) for c in chunks]
        await asyncio.gather(*tasks)
        logger.info("Extraction complete")

    # ------------------------------------------------------------------
    # Phase 3 – Generate
    # ------------------------------------------------------------------

    async def generate_lorebook(self, entity_id: str | None = None) -> LorebookOutput:
        from .models import LorebookOutput as LO

        if entity_id:
            graph_data = await self._graph.get_entity_subgraph(entity_id)
        else:
            graph_data = await self._graph.get_all_entities()

        user_prompt = json.dumps(graph_data, ensure_ascii=False, indent=2)
        if len(user_prompt) > 24000:
            user_prompt = user_prompt[:24000] + "\n...[truncated]"

        system_prompt = load_prompt("generation")
        return await self._llm.extract_structured(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            output_model=LO,
            max_tokens=4096,
        )
