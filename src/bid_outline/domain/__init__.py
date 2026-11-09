"""Validated, serializable workflow records. Source offsets are parsed-text offsets."""
from typing import Any
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Record(BaseModel):
    """Base record rejecting unknown persisted fields."""
    model_config = ConfigDict(extra='forbid')


class SourceRef(Record):
    """Source evidence with explicitly qualified positional precision."""
    document_id: str
    element_ids: list[str] = Field(default_factory=list)
    table_id: str | None = None
    cell_ids: list[str] = Field(default_factory=list)
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, ge=0)
    precision: str = 'block'
    reader_ref: dict[str, Any] = Field(default_factory=dict)
    @model_validator(mode='after')
    def ranges(self):
        if (self.start is None) != (self.end is None) or (self.start is not None and self.end < self.start):
            raise ValueError('source range must have ordered start and end')
        return self


class DocumentElement(Record):
    """One ordered reader fragment and its unmodified provenance."""
    element_id: str
    kind: str
    order: int
    text: str
    section_path: list[str] = Field(default_factory=list)
    source_ref: SourceRef
    metadata: dict[str, Any] = Field(default_factory=dict)


class Document(Record):
    """Frozen reading input with diagnostics carried into generation."""
    document_id: str
    source_hash: str
    reader_name: str
    reader_version: str = 'unknown'
    elements: list[DocumentElement] = Field(default_factory=list)
    diagnostics: list[Any] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    @model_validator(mode='after')
    def identities(self):
        ids = [e.element_id for e in self.elements]
        if len(ids) != len(set(ids)):
            raise ValueError('duplicate document element IDs')
        return self


class Chunk(Record):
    """A processing unit separating owned evidence from repeated context."""
    chunk_id: str
    text: str
    owned_refs: list[SourceRef] = Field(default_factory=list)
    context_refs: list[SourceRef] = Field(default_factory=list)
    context_notes: list[dict[str, Any]] = Field(default_factory=list)
    section_context: list[str] = Field(default_factory=list)
    section_spans: list[dict[str, Any]] = Field(default_factory=list)
    continuation_of: str | None = None
    order: int = 0


class RequirementGroup(Record):
    """A complete obligation group retaining shared conditions."""
    requirement_id: str
    categories: list[str] = Field(default_factory=list)
    topic: str
    content: str
    material_refs: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    producing_task_id: str = ''


class OutlineNode(Record):
    """A stable directory node with requirement and source links."""
    node_id: str = Field(min_length=1)
    parent_id: str | None = None
    order: int = Field(default=1, gt=0)
    title: str = Field(min_length=1)
    basis_requirement_ids: list[str] = Field(default_factory=list)
    origin_kind: str = '原文明示'
    source_refs: list[SourceRef | str] = Field(default_factory=list)


class OutlineConstraint(Record):
    """A document restriction or explicitly identified system policy."""
    constraint_id: str
    affected_node_ids: list[str] = Field(default_factory=list)
    kind: str
    origin: str
    basis: str = ''
    requirement_ids: list[str] = Field(default_factory=list)
    parent_node_id: str | None = None
    allowed_child_titles: list[str] = Field(default_factory=list)
    source_identity: str | None = None
    material_id: str | None = None
    sequence_members: list[dict[str, Any]] = Field(default_factory=list)


def validate_outline(nodes):
    """Reject duplicate IDs, missing parents, cycles, depth and order violations."""
    nodes = [OutlineNode.model_validate(n) if isinstance(n, dict) else n for n in nodes]
    index = {n.node_id: n for n in nodes}
    if len(index) != len(nodes):
        raise ValueError('duplicate outline node ID')
    positions = set()
    for n in nodes:
        if n.parent_id is not None and n.parent_id not in index:
            raise ValueError('missing parent ID')
        pos = (n.parent_id, n.order)
        if n.order <= 0 or pos in positions:
            raise ValueError('invalid or duplicate sibling order')
        positions.add(pos)
        seen = set()
        current = n
        while current is not None:
            if current.node_id in seen:
                raise ValueError('outline cycle')
            seen.add(current.node_id)
            if len(seen) > 4:
                raise ValueError('outline depth exceeds four')
            current = index.get(current.parent_id)


class OutlineVersion(Record):
    """A validated directory snapshot for atomic adoption."""
    version_id: str
    parent_version_id: str | None = None
    nodes: list[OutlineNode] = Field(default_factory=list)
    constraints: list[OutlineConstraint] = Field(default_factory=list)
    input_artifact_ids: list[str] = Field(default_factory=list)
    commit_reason: str = ''
    created_at: str = ''
    @model_validator(mode='after')
    def tree(self):
        validate_outline(self.nodes)
        ids = {n.node_id for n in self.nodes}
        if any(set(c.affected_node_ids) - ids for c in self.constraints):
            raise ValueError('constraint refers to missing node')
        if any(c.parent_node_id is not None and c.parent_node_id not in ids for c in self.constraints):
            raise ValueError('constraint parent refers to missing node')
        return self


class RequirementAssignment(Record):
    """Version-specific allocation, distinct from proof of coverage."""
    producing_artifact_id: str = ''
    requirement_id: str
    outline_version_id: str
    status: str
    targets: list[dict[str, str]] = Field(default_factory=list)
    shared_conditions: str = ''
    unassigned_content: str = ''
    basis: str = ''


class ReviewFinding(Record):
    """A retained review issue with explicit scope and disposition."""
    producing_artifact_id: str = ''
    finding_id: str
    kind: str
    severity: str = 'warning'
    requirement_ids: list[str] = Field(default_factory=list)
    node_ids: list[str] = Field(default_factory=list)
    base_version_id: str = ''
    evidence: str = ''
    suggested_scope: list[str] = Field(default_factory=list)
    status: str = 'open'


class ExtensionSuggestion(Record):
    """An optional suggestion kept outside the adopted backbone."""
    producing_artifact_id: str = ''
    suggestion_id: str
    outline_version_id: str
    chapter_id: str
    topic: str
    content: str
    source_kind: str
    material_refs: list[str] = Field(default_factory=list)
    conditions: str = ''
    review_status: str = 'pending'
