"""All reading views consume the same corrected document."""

import re
from .structure import organize_document


def render_fragments(fragments, tables):
    parts = []
    for fragment in fragments:
        if fragment.role == "heading":
            parts.append("#" * fragment.heading_level + " " + fragment.text)
        elif fragment.kind == "paragraph":
            text = re.sub(r"(?m)^(\s*)(#{1,6})(?=\s)", r"\1\\\2", fragment.text)
            native = next(
                (
                    h
                    for h in fragment.structure
                    if h.get("source") == "docling" and h.get("label") == "list_item"
                ),
                None,
            )
            marker = str(native.get("marker") or "") if native else ""
            if (
                marker
                and not text.lstrip().startswith(marker)
                and not re.match(
                    r"^\s*(?:[（(]?\d+[)）、.．]|[一二三四五六七八九十]+、)", text
                )
            ):
                text = marker + " " + text
            # Native heading labels stay in source metadata. They cannot
            # compete with the corrected level-1/2 roles, even typographically.
            parts.append(text)
        elif fragment.kind == "image":
            parts.append("[图片已跳过]")
        elif fragment.kind == "table":
            parts.append(tables[fragment.table_id]["text"])
    return "\n\n".join(parts) + "\n"


def render_corrected(document, tables):
    return render_fragments(document.fragments, tables)


def render_reading(blocks, sections, tables):
    """Compatibility entry: normalization is still done by the common builder."""
    return render_corrected(organize_document(blocks, sections), tables)
