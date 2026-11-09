"""Diagnostics are evidence of mechanical checks, never semantic acceptance."""

from collections import Counter


def validate_document(blocks, tables, sections, decisions, candidates, issues):
    findings = list(issues)
    duplicates = [k for k, n in Counter(b.id for b in blocks).items() if n > 1]
    if duplicates:
        findings.append({"code": "duplicate_block_ids", "ids": duplicates})
    for block in blocks:
        if block.kind == "table":
            result = tables.get(block.table_id)
            if result is None:
                findings.append(
                    {"code": "table_unprocessed", "table_id": block.table_id}
                )
            else:
                findings.extend(result["issues"])
    if candidates and not decisions:
        findings.append({"code": "no_headings_confirmed"})
    positions = {b.id: i for i, b in enumerate(blocks)}
    for section in sections:
        if (
            section.end_before_block_id
            and positions[section.end_before_block_id]
            < positions[section.start_block_id]
        ):
            findings.append({"code": "invalid_section_range", "section_id": section.id})
    import re

    chapter_levels = [
        s
        for s in sections
        if re.match(r"^第[〇零一二三四五六七八九十百千万两\d]+章", s.title)
    ]
    if chapter_levels and any(s.level != 1 for s in chapter_levels):
        findings.append(
            {
                "code": "main_chapter_level_review",
                "sections": [s.id for s in chapter_levels if s.level != 1],
            }
        )
    for i, block in enumerate(blocks):
        if block.kind != "paragraph" or len(block.text) < 100:
            continue
        normalized = re.sub(r"\s+", "", block.text)
        if any(
            other.kind == "paragraph" and re.sub(r"\s+", "", other.text) == normalized
            for other in blocks[max(0, i - 3) : i]
        ):
            findings.append({"code": "nearby_repeated_paragraph", "block_id": block.id})
    return {
        "status": "incomplete" if findings else "completed",
        "issues": findings,
        "block_count": len(blocks),
        "section_count": len(sections),
        "table_count": len(tables),
        "image_count": sum(b.kind == "image" for b in blocks),
        "semantic_quality": "not_evaluated",
    }


def validate_structure(blocks, document):
    """Verify source partition and ownership independently of text rendering."""
    from collections import defaultdict

    by_block = defaultdict(list)
    issues = []
    block_ids = {b.id for b in blocks}
    ids = [f.id for f in document.fragments]
    if len(ids) != len(set(ids)):
        issues.append({"code": "duplicate_fragment_ids"})
    for fragment in document.fragments:
        by_block[fragment.block_id].append(fragment)
        if fragment.block_id not in block_ids:
            issues.append(
                {"code": "unknown_fragment_source", "fragment_id": fragment.id}
            )
    expected_order = []
    for block in blocks:
        pieces = by_block[block.id]
        expected_order.extend(f.id for f in pieces)
        if not pieces:
            issues.append({"code": "source_block_unassigned", "block_id": block.id})
            continue
        cursor = 0
        for piece in pieces:
            if piece.start_offset != cursor or piece.end_offset < cursor:
                issues.append(
                    {"code": "source_partition_gap_or_overlap", "block_id": block.id}
                )
            if piece.kind != block.kind or piece.table_id != block.table_id:
                issues.append({"code": "source_kind_changed", "fragment_id": piece.id})
            if (
                block.kind == "paragraph"
                and piece.text != block.text[piece.start_offset : piece.end_offset]
            ):
                issues.append({"code": "source_text_changed", "fragment_id": piece.id})
            cursor = piece.end_offset
        expected = len(block.text) if block.kind == "paragraph" else 0
        if cursor != expected or (block.kind != "paragraph" and len(pieces) != 1):
            issues.append({"code": "source_partition_incomplete", "block_id": block.id})
    if expected_order != ids:
        issues.append({"code": "source_order_changed"})
    sections = {s.id: s for s in document.sections}
    positions = {b.id: i for i, b in enumerate(blocks)}
    for section in document.sections:
        if section.end_before_block_id is not None:
            if section.end_before_block_id not in positions:
                issues.append({"code": "unknown_section_end", "section_id": section.id})
            elif (positions[section.start_block_id], section.start_offset) >= (
                positions[section.end_before_block_id],
                section.end_before_offset,
            ):
                issues.append(
                    {"code": "invalid_section_range", "section_id": section.id}
                )
    heading_fragments = {
        f.section_id: f for f in document.fragments if f.role == "heading"
    }
    for section in document.sections:
        fragment = heading_fragments.get(section.id)
        if (
            fragment is None
            or fragment.text != section.title
            or fragment.heading_level != section.level
        ):
            issues.append(
                {"code": "heading_structure_mismatch", "section_id": section.id}
            )
    for fragment in document.fragments:
        if fragment.chapter_id is not None and (
            fragment.chapter_id not in sections
            or sections[fragment.chapter_id].level != 1
        ):
            issues.append({"code": "invalid_chapter_owner", "fragment_id": fragment.id})
        point = (positions.get(fragment.block_id, -1), fragment.start_offset)
        for owner_id in dict.fromkeys([fragment.chapter_id, fragment.section_id]):
            if owner_id not in sections:
                continue
            owner = sections[owner_id]
            begin = (positions[owner.start_block_id], owner.start_offset)
            end = (
                (positions[owner.end_before_block_id], owner.end_before_offset)
                if owner.end_before_block_id in positions
                else (len(blocks), 0)
            )
            if not begin <= point < end:
                issues.append(
                    {
                        "code": "fragment_outside_section_range",
                        "fragment_id": fragment.id,
                        "section_id": owner_id,
                    }
                )
        if fragment.section_id is not None:
            section = sections.get(fragment.section_id)
            if section is None or (
                section.level == 2 and section.parent_id != fragment.chapter_id
            ):
                issues.append(
                    {"code": "invalid_section_owner", "fragment_id": fragment.id}
                )
    return {
        "status": "invalid" if issues else "valid",
        "issues": issues,
        "fragment_count": len(document.fragments),
        "source_block_count": len(blocks),
        "recognition_status": document.status,
        "semantic_quality": "not_evaluated",
    }
