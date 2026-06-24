"""
Tool: create_relationship — lets the LLM create typed relationships
between entities in Neo4j during extraction.
"""

from __future__ import annotations

from typing import Any

_driver = None


def set_driver(driver):
    global _driver
    _driver = driver


async def create_relationship(
    from_id: str,
    to_id: str,
    type: str,
    confidence: float = 0.8,
    volume: str = "",
    chapter: str = "",
    role: str = "",
    relation: str = "",
) -> dict[str, Any]:
    """Create a typed relationship between two entities."""
    if _driver is None:
        return {"success": False, "error": "No Neo4j driver"}

    # Build properties dict — only include non-empty values
    props: dict[str, Any] = {"confidence": confidence}
    if volume:
        props["volume"] = volume
    if chapter:
        props["chapter"] = chapter
    if role:
        props["role"] = role
    if relation:
        props["relation"] = relation

    async with _driver.session() as session:
        result = await session.run(
            f"""
            MATCH (a:Entity {{id: $from_id}})
            MATCH (b:Entity {{id: $to_id}})
            MERGE (a)-[r:{type}]->(b)
            SET r = $props
            RETURN type(r) AS rel_type
            """,
            from_id=from_id,
            to_id=to_id,
            props=props,
        )
        record = await result.single()
        if record:
            return {"success": True, "relationship": f"({from_id})-[:{type}]->({to_id})"}
        return {"success": False, "error": "One or both entities not found"}


def get_tool() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "create_relationship",
            "description": (
                "Create a typed relationship between two entities that already exist "
                "or will be created. Use this to connect characters, locations, factions, "
                "items, and concepts with meaningful relationships like ALLIED_WITH, "
                "ENEMIES_WITH, MEMBER_OF, HAS_ABILITY, PARTICIPATED_IN, etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "from_id": {
                        "type": "string",
                        "description": "ID of the source entity (lowercase, e.g. 'sora')",
                    },
                    "to_id": {
                        "type": "string",
                        "description": "ID of the target entity (lowercase, e.g. 'shiro')",
                    },
                    "type": {
                        "type": "string",
                        "description": "Relationship type: ALLIED_WITH, ENEMIES_WITH, MEMBER_OF, HAS_ABILITY, PARTICIPATED_IN, OWNS, RULES_OVER, RESIDES_IN, CONTROLS, FAMILY_OF, MENTORS, CONTRACTED_WITH, BOUND_BY, CAUSED, TOOK_PLACE_IN, etc.",
                    },
                    "confidence": {
                        "type": "number",
                        "description": "Confidence 0.0-1.0 (default 0.8). Use lower for speculation.",
                    },
                    "volume": {
                        "type": "string",
                        "description": "Volume where this relationship is established",
                    },
                    "chapter": {
                        "type": "string",
                        "description": "Chapter where this relationship is established",
                    },
                    "role": {
                        "type": "string",
                        "description": "For MEMBER_OF: leader/member/former. For PARTICIPATED_IN: protagonist/antagonist/witness.",
                    },
                    "relation": {
                        "type": "string",
                        "description": "For FAMILY_OF: sibling/parent/child. For other types as needed.",
                    },
                },
                "required": ["from_id", "to_id", "type"],
            },
        },
        "handler": create_relationship,
    }
