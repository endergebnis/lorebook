"""
Prompt service — loads .md templates with {{var}} substitution.
ponytail: 30 lines, no cache, no @file:, no compose_multi. Add when needed.
"""

from pathlib import Path

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


def load_prompt(name: str, **variables: str) -> str:
    """Load a prompt .md file and substitute {{var}} placeholders."""
    text = (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")
    for key, value in variables.items():
        text = text.replace(f"{{{{{key}}}}}", str(value))
    return text
