"""
Neo4j async graph driver — v2 (dead code removed).
ponytail: create_relation + find_similar_entities deleted.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from neo4j import AsyncGraphDatabase

from .models import Chunk, ExtractedEntity, ExtractedEvent, LorebookConfig

logger = logging.getLogger(__name__)


class GraphStore:
    """Async Neo4j wrapper."""

    def __init__(self, config: LorebookConfig) -> None:
        self._driver = AsyncGraphDatabase.driver(
            config.neo4j_uri,
            auth=(config.neo4j_user, config.neo4j_password),
        )

    @property
    def driver(self):
        return self._driver

    async def close(self) -> None:
        await self._driver.close()

    # ------------------------------------------------------------------
    # Chunks
    # ------------------------------------------------------------------

    async def store_chunk(self, chunk: Chunk) -> None:
        async with self._driver.session() as session:
            await session.run(
                """
                MERGE (c:Chunk {id: $id})
                SET c.volume = $volume, c.chapter = $chapter,
                    c.text = $text, c.token_count = $token_count, c.index = $index
                """,
                id=chunk.id,
                volume=chunk.volume,
                chapter=chunk.chapter,
                text=chunk.text,
                token_count=chunk.token_count,
                index=chunk.index,
            )

    # ------------------------------------------------------------------
    # Entities
    # ------------------------------------------------------------------

    async def upsert_entity(self, entity: ExtractedEntity, chunk_id: Optional[str] = None) -> None:
        async with self._driver.session() as session:
            await session.run(
                """
                MERGE (e:Entity {id: $id})
                SET e.name = $name, e.type = $type,
                    e.description = $description, e.aliases = $aliases
                """,
                id=entity.id,
                name=entity.name,
                type=entity.type.value,
                description=entity.description,
                aliases=entity.aliases,
            )
            if chunk_id:
                await session.run(
                    """
                    MATCH (e:Entity {id: $entity_id})
                    MATCH (c:Chunk {id: $chunk_id})
                    MERGE (e)-[:EXTRACTED_FROM]->(c)
                    """,
                    entity_id=entity.id,
                    chunk_id=chunk_id,
                )

    # ------------------------------------------------------------------
    # Resume support
    # ------------------------------------------------------------------

    async def get_processed_chunk_ids(self) -> set[str]:
        """Return chunk IDs that already have entities extracted from them."""
        async with self._driver.session() as session:
            result = await session.run(
                "MATCH (:Entity)-[:EXTRACTED_FROM]->(c:Chunk) RETURN DISTINCT c.id AS id"
            )
            return {r["id"] async for r in result}

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    async def upsert_event(self, event: ExtractedEvent, chunk_id: Optional[str] = None) -> None:
        async with self._driver.session() as session:
            await session.run(
                """
                MERGE (ev:Event {description: $description})
                SET ev.importance = $importance
                """,
                description=event.description,
                importance=event.importance,
            )
            if event.location_id:
                await session.run(
                    """
                    MATCH (loc:Entity {id: $loc_id})
                    MATCH (ev:Event {description: $desc})
                    MERGE (ev)-[:OCCURRED_AT]->(loc)
                    """,
                    loc_id=event.location_id,
                    desc=event.description,
                )
            for eid in event.involved_entity_ids:
                await session.run(
                    """
                    MATCH (ent:Entity {id: $eid})
                    MATCH (ev:Event {description: $desc})
                    MERGE (ent)-[:PARTICIPATED_IN]->(ev)
                    """,
                    eid=eid,
                    desc=event.description,
                )
            if chunk_id:
                await session.run(
                    """
                    MATCH (ev:Event {description: $desc})
                    MATCH (c:Chunk {id: $chunk_id})
                    MERGE (ev)-[:EXTRACTED_FROM]->(c)
                    """,
                    desc=event.description,
                    chunk_id=chunk_id,
                )

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    async def get_entity_subgraph(self, entity_id: str) -> dict[str, Any]:
        async with self._driver.session() as session:
            result = await session.run(
                """
                MATCH (e:Entity {id: $eid})-[r]-(connected)
                RETURN e, collect(type(r)) AS rels, collect(connected {.*}) AS neighbours
                """,
                eid=entity_id,
            )
            record = await result.single()
            if record is None:
                return {}
            return {
                "entity": dict(record["e"]),
                "relations": record["rels"],
                "neighbours": record["neighbours"],
            }

    async def get_all_entities(self) -> list[dict[str, Any]]:
        async with self._driver.session() as session:
            result = await session.run("MATCH (e:Entity) RETURN e {.*} AS entity")
            return [record["entity"] async for record in result]

    async def count_entities(self) -> int:
        async with self._driver.session() as session:
            result = await session.run("MATCH (e:Entity) RETURN count(e) AS cnt")
            record = await result.single()
            return record["cnt"] if record else 0
