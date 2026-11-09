"""Internal records. These records are never serialized directly into prompts."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class CellPart:
    kind: str
    text: str = ""
    table: Table | None = None


@dataclass
class Cell:
    id: str
    row: int
    col: int
    paragraphs: list[str]
    row_span: int = 1
    col_span: int = 1
    column_header: bool = False
    row_header: bool = False
    parts: list[CellPart] = field(default_factory=list)

    @property
    def text(self) -> str:
        if self.parts:
            return "\n".join(
                p.text if p.kind == "text" else "\n".join(c.text for c in p.table.cells)
                for p in self.parts
                if p.kind == "text" or p.table is not None
            )
        return "\n".join(self.paragraphs)


@dataclass
class Table:
    id: str
    rows: int
    cols: int
    cells: list[Cell]
    markdown: str = ""
    html: str = ""
    modified: bool = False


@dataclass
class Block:
    id: str
    kind: str
    text: str = ""
    table_id: str | None = None
    ref: str = ""
    structure: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class WordElement:
    id: str
    kind: str
    text: str
    path: str
    style: str = ""
    outline_level: int | None = None
    numbering: dict[str, Any] | None = None
    table: Table | None = None
    is_toc: bool = False


@dataclass
class Candidate:
    id: str
    block_id: str
    text: str
    signals: list[str] = field(default_factory=list)
    suggested_level: int | None = None
    target_title: str | None = None


@dataclass
class HeadingGroup:
    id: str
    title: str
    candidates: list[Candidate]
    toc_index: int | None = None


@dataclass
class Section:
    id: str
    title: str
    level: int
    parent_id: str | None
    start_block_id: str
    end_before_block_id: str | None
    start_offset: int = 0
    end_before_offset: int = 0


@dataclass
class ParsedDocument:
    blocks: list[Block]
    tables: dict[str, Table]
    raw: dict[str, Any]
    issues: list[dict[str, Any]] = field(default_factory=list)
    resource_usage: dict[str, Any] = field(default_factory=dict)


def payload(value: Any) -> Any:
    """Convert dataclasses/collections for persistence only."""
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    if isinstance(value, dict):
        return {k: payload(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [payload(v) for v in value]
    return value


@dataclass
class ContentFragment:
    id: str
    block_id: str
    kind: str
    role: str
    start_offset: int
    end_offset: int
    text: str = ""
    table_id: str | None = None
    chapter_id: str | None = None
    section_id: str | None = None
    heading_level: int | None = None
    heading_status: str = "not_candidate"
    structure: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class CorrectedDocument:
    sections: list[Section]
    fragments: list[ContentFragment]
    issues: list[dict[str, Any]] = field(default_factory=list)
    status: str = "organized"
