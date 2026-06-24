# Lorebook v2

Tool-assisted entity extraction pipeline for light novels → Neo4j knowledge graph.

**Extracts** characters, locations, items, factions, and concepts from markdown using a local LLM (llama.cpp / OpenAI-compatible API). Uses tool-calling to query Neo4j before creating entities — no post-hoc dedup needed.

## Quick Start

```bash
pip install -r requirements.txt
python run.py server
# → http://localhost:8520
```

Requires:
- [Neo4j](https://neo4j.com/) running locally (bolt://localhost:7688)
- [llama.cpp](https://github.com/ggerganov/llama.cpp) server with `/v1` API at `http://localhost:8080/v1`

## Commands

| Command | Description |
|---------|-------------|
| `python run.py server` | Start web dashboard + API |
| `python run.py test` | Run self-tests |

## Dashboard Features

- **Pipeline**: one-click run with pause/resume
- **Live progress**: SSE stream shows LLM tool calls and extraction in real time
- **Setup**: configure model, temperature, concurrency
- **Stats**: entity counts by type, latest entities
- **Export**: Minecraft item format

## Project Structure

```
lorebook_v2/
├── run.py                  # Entry point
├── lorebook_api/
│   ├── web.py              # FastAPI SPA + all endpoints
│   ├── pipeline.py         # Ingest → Extract → Generate
│   ├── runner.py           # Progress queue, pause/resume, tool injection
│   ├── llm_client.py       # llama.cpp chat + tool-calling loop
│   ├── extractor.py        # Single extract_chunk() function
│   ├── chunker.py          # Token-aware markdown → chunks
│   ├── graph.py            # Neo4j storage layer
│   ├── models.py           # Pydantic models + config
│   ├── prompt_service.py   # .md → system prompt loader
│   ├── tool_loader.py      # Dynamic tool discovery + execution
│   ├── export.py           # Entity → Minecraft items
│   └── __init__.py
├── tools/
│   ├── search_entities.py  # Neo4j entity search tool
│   └── __init__.py
├── prompts/
│   ├── extraction.md       # Extraction system prompt
│   └── generation.md       # Lore generation prompt
├── requirements.txt
└── .gitignore
```

## Architecture

```
Markdown files → Chunker (12k tokens/chunk)
→ Extractor (LLM + search_entities tool)
→ Neo4j (entities, events, chunks)
→ Generator (LLM lorebook output)
→ Export (Minecraft items)
```

No resolver phase — the LLM checks for duplicates **during** extraction via tool calling.
