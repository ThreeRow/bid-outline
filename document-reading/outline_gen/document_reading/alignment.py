"""Monotone source alignment; source alternatives remain in sidecars."""

from difflib import SequenceMatcher
import re
from dataclasses import replace
from .models import Block


def normalized(text):
    return re.sub(r"\s+", "", text or "")


def without_markers(text):
    return normalized(
        re.sub(r"(?m)^\s*(?:[（(]\d+[)）]|\d+[、.．]|[①②③④⑤⑥⑦⑧⑨⑩])\s*", "", text)
    )


def sync_table(primary, word, issues):
    lookup = {(c.row, c.col): c for c in word.cells}
    for cell in primary.cells:
        other = lookup.get((cell.row, cell.col))
        if other is None:
            continue
        if normalized(cell.text) == normalized(other.text) or without_markers(
            cell.text
        ) == without_markers(other.text):
            if cell.text != other.text:
                # Restore original paragraph breaks/list labels once, not twice.
                primary.modified = True
            cell.paragraphs = other.paragraphs
            if other.parts:
                # Keep the Docling nested table, synchronize each level by cell.
                own_nested = [p.table for p in cell.parts if p.kind == "table"]
                source_nested = [p.table for p in other.parts if p.kind == "table"]
                if len(own_nested) == len(source_nested):
                    for left, right in zip(own_nested, source_nested):
                        sync_table(left, right, issues)
                    nested = iter(own_nested)
                    cell.parts = [
                        replace(p, table=next(nested)) if p.kind == "table" else p
                        for p in other.parts
                    ]
                    primary.modified = primary.modified or bool(own_nested)
                elif not own_nested and source_nested:
                    cell.parts = other.parts
                    primary.modified = True
                    issues.append(
                        {"code": "nested_table_word_recovery", "table_id": primary.id}
                    )
                else:
                    issues.append(
                        {
                            "code": "nested_table_conflict",
                            "table_id": primary.id,
                            "cell_id": cell.id,
                        }
                    )
            else:
                cell.parts = []
        else:
            issues.append(
                {
                    "code": "table_cell_text_difference",
                    "table_id": primary.id,
                    "cell_id": cell.id,
                    "docling_text": cell.text,
                    "word_text": other.text,
                }
            )
    # No assumption that equal row/column positions prove equal geometry.
    if [(c.row, c.col, c.row_span, c.col_span) for c in primary.cells] != [
        (c.row, c.col, c.row_span, c.col_span) for c in word.cells
    ]:
        issues.append({"code": "table_geometry_difference", "table_id": primary.id})


def align(parsed, words):
    def bt(block):
        return (
            "".join(c.text for c in parsed.tables[block.table_id].cells)
            if block.kind == "table"
            else block.text
        )

    left = [(b.kind, normalized(bt(b))) for b in parsed.blocks]
    keys = set(left)

    def wt(w):
        label = (w.numbering or {}).get("label")
        if (
            label
            and w.text.startswith(label)
            and (w.kind, normalized(w.text[len(label) :])) in keys
        ):
            return w.text[len(label) :]
        return w.text

    right = [(w.kind, normalized(wt(w))) for w in words]
    result, records = [], []

    def emit(bis, wis, *, canonical=False):
        bs, ws = [parsed.blocks[i] for i in bis], [words[j] for j in wis]
        if canonical:
            for n, w in enumerate(ws):
                bid = bs[0].id if len(ws) == 1 else f"{bs[0].id}.{n+1}"
                structure = [dict(hint) for source in bs for hint in source.structure]
                structure.append(
                    {
                        "source": "word",
                        "word_id": w.id,
                        "style": w.style,
                        "outline_level": w.outline_level,
                        "numbering": w.numbering,
                    }
                )
                result.append(
                    Block(bid, "paragraph", w.text, ref=bs[0].ref, structure=structure)
                )
                records.append(
                    {
                        "block_ids": [bid],
                        "word_ids": [w.id],
                        "status": "matched",
                        "docling_block_ids": [b.id for b in bs],
                    }
                )
        else:
            for b in bs:
                if len(bs) == len(ws) == 1:
                    w = ws[0]
                    if b.kind == "table":
                        sync_table(parsed.tables[b.table_id], w.table, parsed.issues)
                    elif b.kind == "paragraph" and (w.numbering or {}).get("label"):
                        b.text = w.text
                b.structure += [
                    {
                        "source": "word",
                        "word_id": w.id,
                        "style": w.style,
                        "outline_level": w.outline_level,
                        "numbering": w.numbering,
                    }
                    for w in ws
                ]
                result.append(b)
            records.append(
                {
                    "block_ids": [b.id for b in bs],
                    "word_ids": [w.id for w in ws],
                    "status": "matched",
                }
            )

    def fallback_b(i):
        b = parsed.blocks[i]
        result.append(b)
        records.append({"block_ids": [b.id], "word_ids": [], "status": "unmatched"})
        if b.kind != "image":
            parsed.issues.append({"code": "docling_block_unmatched", "block_id": b.id})

    def fallback_w(j):
        w = words[j]
        bid = f"BW{j+1:05d}"
        tid = None
        if w.table:
            tid = f"TW{j+1:05d}"
            w.table.id = tid
            parsed.tables[tid] = w.table
        result.append(
            Block(
                bid,
                w.kind,
                w.text if w.kind == "paragraph" else "",
                tid,
                structure=[
                    {
                        "source": "word",
                        "word_id": w.id,
                        "style": w.style,
                        "outline_level": w.outline_level,
                        "numbering": w.numbering,
                    }
                ],
            )
        )
        records.append(
            {"block_ids": [bid], "word_ids": [w.id], "status": "word_fallback"}
        )
        if w.kind != "image":
            parsed.issues.append(
                {"code": "word_content_recovered", "word_id": w.id, "block_id": bid}
            )

    matcher = SequenceMatcher(None, left, right, autojunk=False)
    for tag, a, b, c, d in matcher.get_opcodes():
        if tag == "equal":
            for i, j in zip(range(a, b), range(c, d)):
                emit([i], [j])
            continue
        i, j = a, c
        while i < b and j < d:
            # Accumulate adjacent text blocks until both cover the same original
            # text. No three-block cap: inline placeholders often create >10.
            if left[i][0] == right[j][0] == "paragraph":
                x, y, ni, nj = "", "", i, j
                found = False
                while (ni < b or nj < d) and ni - i < 128 and nj - j < 128:
                    if not x or len(x) <= len(y):
                        if ni >= b or left[ni][0] != "paragraph":
                            break
                        x += left[ni][1]
                        ni += 1
                    else:
                        if nj >= d or right[nj][0] != "paragraph":
                            break
                        y += right[nj][1]
                        nj += 1
                    if x and x == y:
                        emit(list(range(i, ni)), list(range(j, nj)), canonical=True)
                        i, j = ni, nj
                        found = True
                        break
                if found:
                    continue
            if (
                left[i][0] == right[j][0] == "table"
                and SequenceMatcher(
                    None, left[i][1], right[j][1], autojunk=False
                ).ratio()
                >= 0.85
            ):
                emit([i], [j])
                i += 1
                j += 1
                continue
            # Boundaries are not evidence of missing text. If a whole adjacent
            # paragraph region only differs in numbering/Word symbols, use the
            # original Word text once and retain differences in the sidecar.
            bi = i
            while bi < b and left[bi][0] == "paragraph":
                bi += 1
            wj = j
            while wj < d and right[wj][0] == "paragraph":
                wj += 1
            if bi > i and wj > j:
                x = "\n".join(parsed.blocks[k].text for k in range(i, bi))
                y = "\n".join(words[k].text for k in range(j, wj))
                if (
                    without_markers(x) == without_markers(y)
                    or SequenceMatcher(
                        None, normalized(x), normalized(y), autojunk=False
                    ).ratio()
                    >= 0.88
                ):
                    emit(list(range(i, bi)), list(range(j, wj)), canonical=True)
                    parsed.issues.append(
                        {
                            "code": "paragraph_text_reconciled",
                            "docling_block_ids": [
                                parsed.blocks[k].id for k in range(i, bi)
                            ],
                            "word_ids": [words[k].id for k in range(j, wj)],
                            "docling_text": x,
                            "word_text": y,
                        }
                    )
                    i, j = bi, wj
                    continue
            if b - i > d - j:
                fallback_b(i)
                i += 1
            else:
                fallback_w(j)
                j += 1
        for k in range(i, b):
            fallback_b(k)
        for k in range(j, d):
            fallback_w(k)
    parsed.blocks = result
    return records
