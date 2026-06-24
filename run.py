#!/usr/bin/env python3
"""Entry point: python run.py server"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)

logger = logging.getLogger("lorebook")


def cmd_server():
    import uvicorn
    uvicorn.run("lorebook_api.web:app", host="0.0.0.0", port=8520, reload=False, log_level="info")


def cmd_test():
    """Quick smoke test: load first .md, chunk it, show stats."""
    from lorebook_api.chunker import split_markdown
    from lorebook_api.models import LorebookConfig

    config = LorebookConfig()
    input_dir = Path(config.input_dir)
    md_files = sorted(input_dir.glob("*.md"))
    if not md_files:
        print("No .md files found in", input_dir)
        return

    for f in md_files[:3]:
        chunks = split_markdown(f, config.chunk_size_tokens, config.chunk_overlap_tokens)
        print(f"{f.name}: {len(chunks)} chunks, ~{sum(c.token_count for c in chunks)} tokens")


def main():
    parser = argparse.ArgumentParser(description="Lorebook v2")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("server", help="Start FastAPI server on :8520")
    sub.add_parser("test", help="Quick chunking stats")

    args = parser.parse_args()

    if args.command == "server":
        cmd_server()
    elif args.command == "test":
        cmd_test()
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
