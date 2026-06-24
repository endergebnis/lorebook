"""
Pipeline runner — worker pool pulling chunks from a queue, one LLM client per instance.
ponytail: workers are independent, queue naturally load-balances across llama.cpp instances.
"""

from __future__ import annotations

import asyncio
import logging
import time
from itertools import cycle

from .extractor import extract_chunk
from .llm_client import LLMClient
from .models import LorebookConfig
from .pipeline import LorebookPipeline
from .tool_loader import ToolLoader
from tools.search_entities import set_driver

logger = logging.getLogger(__name__)


class PipelineRunner:
    """Async wrapper with SSE progress queue + pause control + worker pool."""

    def __init__(self, pipeline: LorebookPipeline | None = None) -> None:
        self._pipeline = pipeline or LorebookPipeline()
        self._progress: asyncio.Queue[dict] = asyncio.Queue()
        self._pause_event = asyncio.Event()
        self._pause_event.set()
        self._running = False
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
        self._ensure_tools()

    async def close(self) -> None:
        await self._pipeline.close()

    def pause(self) -> None:
        self._pause_event.clear()
        self._progress.put_nowait({"type": "status", "status": "paused"})

    def resume(self) -> None:
        self._pause_event.set()
        self._progress.put_nowait({"type": "status", "status": "running"})

    def _ensure_tools(self) -> None:
        if not self._tools_loaded:
            self._tool_loader.load_all()
            set_driver(self._pipeline.graph.driver)
            self._tools_loaded = True

    def _push_progress(self, event: dict) -> None:
        self._progress.put_nowait(event)

    # ------------------------------------------------------------------
    # Worker Pool
    # ------------------------------------------------------------------

    async def run_full_pipeline(self, input_dir: str) -> dict:
        self._running = True
        self._progress.put_nowait({"type": "status", "status": "running", "phase": "ingest"})
        start_time = time.time()

        try:
            # Phase 1 — Ingest
            chunks = await self._pipeline.ingest_directory(input_dir)
            self._progress.put_nowait({"type": "phase", "phase": "ingest", "chunks": len(chunks)})

            # Phase 2 — Extract with worker pool
            self._ensure_tools()

            # Resume
            processed = await self._pipeline.graph.get_processed_chunk_ids()
            pending = [c for c in chunks if c.id not in processed]
            if processed:
                self._progress.put_nowait({
                    "type": "status", "status": "running",
                    "message": f"Resuming: {len(pending)}/{len(chunks)} chunks remaining"
                })
                logger.info("Skipping %d already-processed chunks", len(processed))

            if not pending:
                elapsed = time.time() - start_time
                self._progress.put_nowait({
                    "type": "done", "chunks": 0, "entities": 0, "events": 0,
                    "elapsed_seconds": int(elapsed),
                    "message": "All chunks already processed — nothing to do",
                })
                self._running = False
                return {"chunks": 0, "entities": 0, "events": 0, "elapsed_seconds": int(elapsed)}

            # -- Worker setup --
            cfg = self._pipeline.config
            worker_urls = cfg.llm_base_urls or [cfg.llm_base_url]
            worker_count = min(cfg.concurrency, len(worker_urls)) if cfg.concurrency > 1 else len(worker_urls)
            url_cycle = cycle(worker_urls)

            tools_def = self._tool_loader.get_definitions() if self._tool_loader.names else None

            # Shared state
            chunk_queue: asyncio.Queue = asyncio.Queue()
            for c in pending:
                chunk_queue.put_nowait(c)
            total = len(pending)

            entity_count = 0
            event_count = 0
            completed = 0
            entity_lock = asyncio.Lock()

            self._progress.put_nowait({
                "type": "phase", "phase": "extract", "total": total,
                "workers": worker_count, "endpoints": worker_urls,
            })

            async def _worker(worker_id: int, base_url: str) -> None:
                """One worker = one llm endpoint, pulls chunks until queue empty."""
                nonlocal entity_count, event_count, completed

                # Each worker gets its own LLM client
                worker_cfg = LorebookConfig(
                    llm_base_url=base_url,
                    model_name=cfg.model_name,
                    temperature=cfg.temperature,
                    max_tokens=cfg.max_tokens,
                    neo4j_uri=cfg.neo4j_uri,
                    neo4j_user=cfg.neo4j_user,
                    neo4j_password=cfg.neo4j_password,
                )
                client = LLMClient(worker_cfg)
                await client.start()

                # Worker-local tool executor (reuses injected driver)
                tool_executor = self._tool_loader.execute if self._tool_loader.names else None

                logger.info("Worker %d started → %s", worker_id, base_url)

                try:
                    while True:
                        await self._pause_event.wait()

                        try:
                            chunk = chunk_queue.get_nowait()
                        except asyncio.QueueEmpty:
                            return  # all done

                        try:
                            output = await extract_chunk(
                                client, chunk.text,
                                tools=tools_def,
                                tool_executor=tool_executor,
                                progress_cb=self._push_progress,
                            )

                            entity_names = []
                            for entity in output.entities:
                                await self._pipeline.graph.upsert_entity(entity, chunk_id=chunk.id)
                                entity_names.append(entity.name)
                            for event in output.events:
                                await self._pipeline.graph.upsert_event(event, chunk_id=chunk.id)

                            async with entity_lock:
                                entity_count += len(output.entities)
                                event_count += len(output.events)
                                completed += 1

                            elapsed = time.time() - start_time
                            eta = (elapsed / completed) * (total - completed) if completed > 0 else 0
                            self._progress.put_nowait({
                                "type": "chunk",
                                "chunk_index": completed,
                                "total_chunks": total,
                                "volume": chunk.volume,
                                "chapter": chunk.chapter,
                                "entities_found": len(output.entities),
                                "events_found": len(output.events),
                                "entity_names": entity_names[:5],
                                "total_entities": entity_count,
                                "total_events": event_count,
                                "eta_seconds": int(eta),
                                "worker": worker_id,
                            })

                        except Exception:
                            logger.exception("Worker %d chunk %s failed", worker_id, chunk.id[:8])
                            async with entity_lock:
                                completed += 1
                            self._progress.put_nowait({
                                "type": "chunk_error",
                                "chunk_index": completed,
                                "total_chunks": total,
                                "volume": chunk.volume,
                                "chapter": chunk.chapter,
                                "worker": worker_id,
                            })

                finally:
                    await client.close()
                    logger.info("Worker %d stopped", worker_id)

            # Spawn workers
            workers = [
                _worker(i, next(url_cycle))
                for i in range(worker_count)
            ]
            await asyncio.gather(*workers)

            # Done
            elapsed = time.time() - start_time
            self._progress.put_nowait({
                "type": "done",
                "chunks": completed,
                "entities": entity_count,
                "events": event_count,
                "elapsed_seconds": int(elapsed),
            })

            return {
                "chunks": completed,
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
