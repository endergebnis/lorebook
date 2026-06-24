"""
Pydantic models for the Lorebook pipeline (v2 — dead models removed).
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Optional
from uuid import uuid4

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class EntityType(str, Enum):
    Character = "Character"
    Location = "Location"
    Item = "Item"
    Faction = "Faction"
    Concept = "Concept"


# ---------------------------------------------------------------------------
# Chunk
# ---------------------------------------------------------------------------

class Chunk(BaseModel):
    id: str = Field(default="")  # set via make_id

    # ponytail: deterministic ID so resume survives across runs
    @classmethod
    def make_id(cls, volume: str, chapter: str, index: int) -> str:
        import hashlib
        return hashlib.md5(f"{volume}|{chapter}|{index}".encode()).hexdigest()[:12]

    volume: str
    chapter: str
    text: str
    token_count: int = 0
    index: int = 0


# ---------------------------------------------------------------------------
# Extraction output
# ---------------------------------------------------------------------------

class ExtractedEntity(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    name: str
    type: EntityType
    description: str = ""
    aliases: list[str] = []


class ExtractedEvent(BaseModel):
    description: str
    location_id: Optional[str] = None
    involved_entity_ids: list[str] = []
    importance: int = Field(default=3, ge=1, le=5)


class ExtractorOutput(BaseModel):
    entities: list[ExtractedEntity]
    events: list[ExtractedEvent]


# ---------------------------------------------------------------------------
# Generation output
# ---------------------------------------------------------------------------

class NPCDialogue(BaseModel):
    name: str
    personality: str = ""
    dialogue_lines: list[str] = []


class QuestItem(BaseModel):
    name: str
    lore: str = ""
    nbt_tags: dict = {}


class LorebookOutput(BaseModel):
    npc_dialogues: list[NPCDialogue] = []
    quest_items: list[QuestItem] = []


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class LorebookConfig(BaseModel):
    neo4j_uri: str = "bolt://localhost:7688"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "lorebook_secure_pass"

    llm_base_url: str = "http://localhost:8080/v1"
    llm_base_urls: list[str] = []  # ponytail: for multi-instance parallelism
    llm_api_key: str = "not-needed"

    model_name: str = "gemma-4-26B-A4B-it-uncensored-Q4_K_M.gguf"
    temperature: float = 0.25
    max_tokens: int = 4096

    chunk_size_tokens: int = 12000
    chunk_overlap_tokens: int = 200
    concurrency: int = 1

    input_dir: str = "/mnt/data/lightnovel/output_clean"
