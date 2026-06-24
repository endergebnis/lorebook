"""
Game export — converts graph entities to Minecraft NBT/JSON format.
ponytail: pure function, no class, ~30 lines.
"""

from __future__ import annotations


def to_minecraft_item(entity: dict, item_type: str = "lore_item") -> dict:
    """Convert a graph entity to a Minecraft-compatible item dict."""
    return {
        "id": entity.get("id", ""),
        "name": entity.get("name", "Unknown"),
        "type": item_type,
        "lore": entity.get("description", ""),
        "entity_type": entity.get("type", ""),
        "nbt": {
            "display": {
                "Name": f'{{"text":"{entity.get("name", "Unknown")}"}}',
                "Lore": [f'{{"text":"{entity.get("description", "")}"}}'],
            },
            "CustomTags": {
                "source": "lorebook",
                "entity_id": entity.get("id", ""),
                "aliases": entity.get("aliases", []),
            },
        },
    }


def export_entities(entities: list[dict], item_type: str = "lore_item") -> list[dict]:
    """Batch-convert entities to Minecraft items."""
    return [to_minecraft_item(e, item_type) for e in entities]
