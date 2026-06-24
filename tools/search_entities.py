"""Tool: search_entities — query Neo4j for existing entities before creating."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from neo4j import AsyncDriver

_driver: "AsyncDriver | None" = None


def set_driver(driver: "AsyncDriver") -> None:
    global _driver
    _driver = driver


async def search_entities(
    queries: list[dict] | None = None,
    name: str | None = None,
    limit: int = 5,
) -> dict:
    """Search Neo4j for existing entities by name (batch or single)."""
    if _driver is None:
        return {"success": False, "error": "Neo4j driver not injected", "results": []}

    limit = max(1, min(int(limit or 5), 10))

    if queries and isinstance(queries, list):
        if len(queries) > 10:
            return {"success": False, "error": "Max 10 queries per call", "results": []}
        results = []
        for q in queries[:10]:
            qname = q["name"] if isinstance(q, dict) else str(q)
            results.append(await _search_one(qname.strip(), limit))
        return {"success": True, "results": results, "any_matches": any(r.get("matches") for r in results)}

    if name and isinstance(name, str) and len(name.strip()) >= 2:
        result = await _search_one(name.strip(), limit)
        return {"success": True, "results": [result], "any_matches": bool(result.get("matches"))}

    return {"success": False, "error": "Provide 'queries' or 'name'", "results": []}


async def _search_one(name: str, limit: int) -> dict:
    try:
        async with _driver.session() as session:
            result = await session.run(
                """
                MATCH (e:Entity)
                WHERE toLower(e.name) CONTAINS toLower($name)
                   OR toLower($name) CONTAINS toLower(e.name)
                RETURN e {.id, .name, .type, .description} AS entity
                LIMIT $limit
                """,
                name=name,
                limit=limit,
            )
            matches = [r["entity"] async for r in result]
            return {
                "query": name,
                "matches": [
                    {"id": m["id"], "name": m["name"], "type": m["type"],
                     "description": (m.get("description") or "")[:120]}
                    for m in matches
                ],
            }
    except Exception as e:
        return {"query": name, "error": str(e), "matches": []}


def get_tool() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "search_entities",
            "description": (
                "Search the knowledge graph for existing entities BEFORE creating new ones. "
                "Use batch queries for efficiency. Use short names (2-3 words) for best matching."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "queries": {
                        "type": "array",
                        "description": "Entities to check: [{'name': 'Sora'}, {'name': 'Elchea', 'type': 'Location'}]",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string", "description": "Entity name (2-3 words)"},
                                "type": {"type": "string", "enum": ["Character", "Location", "Item", "Faction", "Concept"]},
                            },
                            "required": ["name"],
                        },
                    },
                    "limit": {"type": "integer", "description": "Max results per query (1-10, default 5)"},
                },
                "required": ["queries"],
            },
        },
        "handler": search_entities,
    }
