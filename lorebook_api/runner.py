"""
Pipeline runner — wraps the pipeline with progress queue, pause/resume, and tool injection.
ponytail: unified extract_all with per-chunk progress + tool-assisted dedup.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from .extractor import extract_chunk
from .pipeline import LorebookPipeline
from .tool_loader import ToolLoader
from tools.search_entities import set_driver

logger = logging.getLogger(__name__)


class PipelineRunner:
    """Async wrapper with SSE progress queue + pause control."""

    def __init__(self, pipeline: LorebookPipeline | None = None) -> None:
        self._pipeline = pipeline or LorebookPipeline()
        self._progress: asyncio.Queue[dict] = asyncio.Queue()
        self._pause_event = asyncio.Event()
        self._pause_event.set()  # not paused
        self._running = False

        # Tool loader
        self._tool_loader = ToolLoader()
        self._tools_loaded = False

    @property
    def progress(self) -> asyncio.Queue[dict]:
        return self._progress

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_paused(self) -> bool:
        return not self._pause_event.is_set()

    async def start(self) -> None:
        await self._pipeline.start()
        self._ensure_tools()  # ponytail: load tools on startup so badge is green

    async def close(self) -> None:
        await self._pipeline.close()

    # -- Pause / Resume --

    def pause(self) -> None:
        self._pause_event.clear()
        self._progress.put_nowait({"type": "status", "status": "paused"})

    def resume(self) -> None:
        self._pause_event.set()
        self._progress.put_nowait({"type": "status", "status": "running"})

    # -- Tools --

    def _ensure_tools(self) -> None:
        """Load tools and inject Neo4j driver."""
        if not self._tools_loaded:
            self._tool_loader.load_all()
            set_driver(self._pipeline.graph.driver)
            self._tools_loaded = True

    def _push_progress(self, event: dict) -> None:
        """Push an event to the SSE progress queue (non-async, queue-safe)."""
        self._progress.put_nowait(event)

    # ------------------------------------------------------------------
    # Full pipeline with progress
    # ------------------------------------------------------------------

    async def run_full_pipeline(self, input_dir: str) -> dict:
        self._running = True
        self._progress.put_nowait({"type": "status", "status": "running", "phase": "ingest"})
        start_time = time.time()

        try:
            # Phase 1 — Ingest
            chunks = await self._pipeline.ingest_directory(input_dir)
            self._progress.put_nowait({
                "type": "phase", "phase": "ingest", "chunks": len(chunks),
            })

            # Phase 2 — Extract with tools
            self._ensure_tools()
            self._progress.put_nowait({"type": "phase", "phase": "extract", "total": len(chunks)})

            tools_def = self._tool_loader.get_definitions() if self._tool_loader.names else None
            tool_executor = self._tool_loader.execute if self._tool_loader.names else None

            entity_count = 0
            event_count = 0

            for i, chunk in enumerate(chunks):
                # Pause gate
                await self._pause_event.wait()

                try:
                    output = await extract_chunk(
                        self._pipeline.llm,
                        chunk.text,
                        tools=tools_def,
                        tool_executor=tool_executor,
                        progress_cb=self._push_progress,
                    )

                    # Store entities
                    entity_names = []
                    for entity in output.entities:
                        await self._pipeline.graph.upsert_entity(entity, chunk_id=chunk.id)
                        entity_names.append(entity.name)

                    # Store events
                    for event in output.events:
                        await self._pipeline.graph.upsert_event(event, chunk_id=chunk.id)

                    entity_count += len(output.entities)
                    event_count += len(output.events)

                    # Progress message
                    elapsed = time.time() - start_time
                    eta = (elapsed / (i + 1)) * (len(chunks) - i - 1) if i > 0 else 0
                    self._progress.put_nowait({
                        "type": "chunk",
                        "chunk_index": i + 1,
                        "total_chunks": len(chunks),
                        "volume": chunk.volume,
                        "chapter": chunk.chapter,
                        "entities_found": len(output.entities),
                        "events_found": len(output.events),
                        "entity_names": entity_names[:5],
                        "total_entities": entity_count,
                        "total_events": event_count,
                        "eta_seconds": int(eta),
                    })

                except Exception:
                    logger.exception("Chunk %d failed", i + 1)
                    self._progress.put_nowait({
                        "type": "chunk_error",
                        "chunk_index": i + 1,
                        "total_chunks": len(chunks),
                        "volume": chunk.volume,
                        "chapter": chunk.chapter,
                    })

            # Done
            elapsed = time.time() - start_time
            self._progress.put_nowait({
                "type": "done",
                "chunks": len(chunks),
                "entities": entity_count,
                "events": event_count,
                "elapsed_seconds": int(elapsed),
            })

            return {
                "chunks": len(chunks),
                "entities": entity_count,
                "events": event_count,
                "elapsed_seconds": int(elapsed),
            }

        except Exception:
            logger.exception("Pipeline failed")
            self._progress.put_nowait({"type": "error", "message": "Pipeline failed"})
            raise
        finally:
            self._running = False
