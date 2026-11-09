"""Materialize one ordered, source-preserving view after heading recognition."""

from collections import defaultdict
from .models import ContentFragment, CorrectedDocument


def organize_document(blocks, sections, *, candidates=(), issues=()):
    """Sections are evidence from recognition, not newly inferred here.

    A source paragraph is partitioned exactly once. Section intervals can be
    nested, while fragments carry one owning chapter and deepest subsection.
    """
    positions = {b.id: i for i, b in enumerate(blocks)}
    if len(positions) != len(blocks):
        raise ValueError("duplicate source block IDs")
    by_section = {s.id: s for s in sections}
    if len(by_section) != len(sections):
        raise ValueError("duplicate section IDs")
    starts = defaultdict(dict)
    for section in sections:
        if section.start_block_id not in positions:
            raise ValueError(f"unknown heading block: {section.id}")
        block = blocks[positions[section.start_block_id]]
        offset = section.start_offset
        if block.kind != "paragraph" or not 0 <= offset < len(block.text):
            raise ValueError(f"invalid heading position: {section.id}")
        if offset in starts[block.id]:
            raise ValueError(f"duplicate heading position: {block.id}:{offset}")
        starts[block.id][offset] = section
        if section.level not in {1, 2}:
            raise ValueError("corrected headings must be level 1 or 2")
        if section.level == 2:
            parent = by_section.get(section.parent_id)
            if parent is None or parent.level != 1:
                raise ValueError(
                    f"secondary heading has no primary parent: {section.id}"
                )
    issue_list = list(issues)
    failed_ids = {
        cid
        for issue in issue_list
        for cid in (
            [issue["candidate_id"]]
            if issue.get("candidate_id")
            else issue.get("candidate_ids", [])
        )
    }
    failed_blocks = {c.block_id for c in candidates if c.id in failed_ids}
    failed_targets = {
        i.get("title") for i in issue_list if i.get("code", "").startswith("primary_")
    }
    failed_blocks.update(
        c.block_id for c in candidates if c.target_title in failed_targets
    )
    uncertain_chapters = {
        i.get("chapter")
        for i in issue_list
        if i.get("code", "").startswith("secondary_")
    }
    uncertain_chapter_ids = {
        s.id for s in sections if s.level == 1 and s.title in uncertain_chapters
    }
    candidate_blocks = {c.block_id for c in candidates}
    # Issues without candidate IDs still make the global result unresolved.
    unresolved = any(
        i.get("code", "").startswith(("primary_", "secondary_", "toc_"))
        for i in issue_list
    )
    fragments = []
    chapter = subsection = None
    for block in blocks:
        events = starts.get(block.id, {})
        if block.kind != "paragraph":
            spans = [(0, 0)]
        else:
            boundaries = sorted({0, len(block.text), *events})
            spans = list(zip(boundaries, boundaries[1:])) or [(0, 0)]
        for left, right in spans:
            heading = events.get(left)
            if heading:
                if heading.level == 1:
                    chapter, subsection = heading.id, None
                else:
                    if chapter != heading.parent_id:
                        raise ValueError(
                            f"heading outside active chapter: {heading.id}"
                        )
                    subsection = heading.id
            status = (
                "confirmed"
                if heading
                else (
                    "unresolved"
                    if block.id in failed_blocks
                    or (
                        block.id in candidate_blocks
                        and chapter in uncertain_chapter_ids
                    )
                    else (
                        "not_selected"
                        if block.id in candidate_blocks
                        else "not_candidate"
                    )
                )
            )
            fragments.append(
                ContentFragment(
                    f"F{len(fragments)+1:06d}",
                    block.id,
                    block.kind,
                    "heading" if heading else block.kind,
                    left,
                    right,
                    block.text[left:right] if block.kind == "paragraph" else "",
                    block.table_id,
                    chapter,
                    subsection or chapter,
                    heading.level if heading else None,
                    status,
                    [dict(h) for h in block.structure],
                )
            )
    return CorrectedDocument(
        sections, fragments, issue_list, "unresolved" if unresolved else "organized"
    )


def section_path(document, fragment):
    by_id = {s.id: s for s in document.sections}
    ids = [fragment.chapter_id]
    if fragment.section_id != fragment.chapter_id:
        ids.append(fragment.section_id)
    return [by_id[sid].title for sid in ids if sid in by_id]


def chapter_groups(document):
    groups = [(None, [f for f in document.fragments if f.chapter_id is None])]
    groups.extend(
        (s, [f for f in document.fragments if f.chapter_id == s.id])
        for s in document.sections
        if s.level == 1
    )
    return groups
