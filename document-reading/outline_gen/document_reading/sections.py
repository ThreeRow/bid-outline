"""Build section ranges, including confirmed headings inside a text block."""

from .models import Section


def heading_offset(text, target):
    if not target:
        return 0
    # Ignore spacing differences only. Never delete or rewrite source text.
    compact = [(i, char) for i, char in enumerate(text) if not char.isspace()]
    needle = "".join(target.split())
    haystack = "".join(char for _, char in compact)
    if not needle or not haystack.endswith(needle):
        return 0
    start = len(haystack) - len(needle)
    # Ambiguous repeated titles need separate positional handling, not trimming.
    if haystack.count(needle) != 1:
        return 0
    return compact[start][0]


def build_sections(blocks, candidates, decisions):
    by_id = {c.id: c for c in candidates}
    chosen = {by_id[d["id"]].block_id: (d["level"], by_id[d["id"]]) for d in decisions}
    sections, stack = [], []
    for block in blocks:
        if block.id not in chosen:
            continue
        level, candidate = chosen[block.id]
        offset = heading_offset(block.text, candidate.target_title)
        while stack and stack[-1].level >= level:
            previous = stack.pop()
            previous.end_before_block_id = block.id
            previous.end_before_offset = offset
        section = Section(
            f"S{len(sections)+1:05d}",
            block.text[offset:],
            level,
            stack[-1].id if stack else None,
            block.id,
            None,
            start_offset=offset,
        )
        sections.append(section)
        stack.append(section)
    return sections
