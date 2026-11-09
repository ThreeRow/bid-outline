"""Discover bounded TOC and numbered/title-style candidates in reading order."""

import re
from outline_gen.runtime.tokenization import count_text_tokens
from dataclasses import replace
from .alignment import normalized

CHAPTER = re.compile(r"^第[〇零一二三四五六七八九十百千万两\d]+(?:章|节|篇|部分|卷)")
NUMBERED = re.compile(
    r"^(?:[A-Z]\d+[、.．]|[一二三四五六七八九十百]+[、.．]|[（(][一二三四五六七八九十\d]+[)）]|\d+(?:\.\d+)+[\s、.．]|\d+[、.．])"
)
TOC_LINE = re.compile(r"(?:[.．·…]{2,}|\t|\s{2,})\s*\d+\s*$")


def plausible_line(text):
    text = text.strip()
    return (
        len(text) <= 65
        and not re.search(r"[。；;！？!?]", text)
        and bool(CHAPTER.match(text) or NUMBERED.match(text))
    )


def split_lines(blocks, word_map, records):
    """Expose original line boundaries; retain mappings and do not guess titles."""
    result = []
    for block in blocks:
        lines = block.text.splitlines()
        if (
            block.kind != "paragraph"
            or len(lines) < 2
            or not any(plausible_line(x) for x in lines)
        ):
            result.append(block)
            continue
        pieces = [
            replace(block, id=f"{block.id}.L{i+1}", text=line)
            for i, line in enumerate(lines)
            if line.strip()
        ]
        result.extend(pieces)
        for n, piece in enumerate(pieces):
            word_map[piece.id] = word_map.get(block.id, []) if n == 0 else []
        for record in records:
            if block.id in record["block_ids"]:
                record["block_ids"] = [
                    v
                    for bid in record["block_ids"]
                    for v in ([p.id for p in pieces] if bid == block.id else [bid])
                ]
                record.setdefault("split_from", []).append(block.id)
    return result


def find_toc(blocks, word_map, config, tables):
    start = next(
        (
            i
            for i, b in enumerate(blocks[: config.toc_scan_blocks])
            if normalized(b.text) in {"目录", "目次"}
            or any(w.is_toc for w in word_map.get(b.id, []))
        ),
        None,
    )
    if start is None:
        return [], ""
    selected, lines, total = [], [], 0
    entries = False
    for b in blocks[start : config.toc_scan_blocks]:
        if b.kind != "paragraph":
            break
        text = b.text.strip()
        entry = bool(TOC_LINE.search(text)) or any(
            w.is_toc for w in word_map.get(b.id, [])
        )
        if entries and not entry:
            # An unnumbered body heading is still the end of a TOC.
            break
        if count_text_tokens("\n".join([*lines, text])) > config.toc_max_tokens:
            break
        if selected and not entry and normalized(text) not in {"目录", "目次"}:
            break
        selected.append(b.id)
        lines.append(text)
        total += len(text)
        entries |= entry
    return selected, "\n".join(lines) if entries else ""
