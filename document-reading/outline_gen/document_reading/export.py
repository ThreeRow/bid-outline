"""Derived artifacts; no independent interpretation of chapter ownership."""

from .models import payload
from .render import render_corrected, render_fragments
from .structure import chapter_groups
from .table_render import render_table
from outline_gen.io.fs import write_json, write_text


def source_table_views(tables, fmt="md"):
    return {
        tid: {"text": render_table(table, fmt), "status": "source_view", "issues": []}
        for tid, table in tables.items()
    }


def export_structure(output, document, source_tables):
    write_json(output / "structure/document.json", payload(document))
    write_text(
        output / "structure/reading.md",
        render_corrected(document, source_table_views(source_tables)),
    )
    write_text(
        output / "headings.md",
        "\n".join("#" * s.level + " " + s.title for s in document.sections),
    )


def export_reading(output, document, tables):
    write_text(output / "reading.md", render_corrected(document, tables))
    index = []
    for section, fragments in chapter_groups(document):
        name = f"{section.id}.md" if section else "frontmatter.md"
        write_text(output / "chapters" / name, render_fragments(fragments, tables))
        index.append(
            {
                "chapter_id": section.id if section else None,
                "title": section.title if section else "首章前内容",
                "file": f"chapters/{name}",
                "fragment_ids": [f.id for f in fragments],
                "child_section_ids": [
                    s.id
                    for s in document.sections
                    if section and s.parent_id == section.id
                ],
                "source_range": (
                    {
                        "start_block_id": section.start_block_id,
                        "start_offset": section.start_offset,
                        "end_before_block_id": section.end_before_block_id,
                        "end_before_offset": section.end_before_offset,
                    }
                    if section
                    else None
                ),
                "structure_status": document.status,
            }
        )
    write_json(output / "chapters/index.json", index)
