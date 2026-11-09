"""Load prompt templates and assemble explicit shared policy fragments."""
from __future__ import annotations

from pathlib import Path

from .fs import read_text


_SHARED_FRAGMENTS = {
    "{{MATERIAL_BOUNDARY_GENERATION}}": "material_boundary_generation.md",
    "{{MATERIAL_BOUNDARY_FORWARDING}}": "material_boundary_forwarding.md",
}


def read_prompt(path: Path) -> str:
    """Expand policy markers before stage-specific user template filling.

    Shared files live beside the stage templates and are read on each load.
    Missing or empty policy files fail explicitly; no policy is silently omitted.
    Other placeholders (e.g. CASE_ID) remain for fill_template().
    """
    prompt = read_text(path)
    for marker, filename in _SHARED_FRAGMENTS.items():
        if marker in prompt:
            prompt = prompt.replace(marker, read_text(path.parent / filename))
    return prompt
