"""Short original-text excerpts, with only candidate IDs added for selection."""

import re
from outline_gen.runtime.tokenization import count_text_tokens, clip_tokens


def excerpt(candidate, blocks, positions, max_tokens, *, start=0, end=None):
    index = positions[candidate.block_id]
    end = len(blocks) if end is None else end

    def gather(indices, before=False):
        lines, used = [], 0
        substantive = False
        clipped = False
        for i in indices:
            block = blocks[i]
            if block.kind != "paragraph":
                if used + 5 <= max_tokens:
                    lines.append("[表格]" if block.kind == "table" else "[图片]")
                break
            value = block.text.strip()
            if not value:
                continue
            sentences = re.findall(r"[^。！？!?]+[。！？!?]?", value)
            value = "".join(sentences[-2:] if before else sentences[:2])
            remaining = max_tokens - used
            if remaining <= 0:
                clipped = True
                break
            if count_text_tokens(value) > remaining:
                value = (
                    clip_tokens(value, remaining, tail=before, marker="…")
                )
                clipped = True
            lines.append(value)
            used += count_text_tokens(value)
            substantive = (
                substantive or bool(re.search(r"[。！？!?]", value)) or len(value) >= 55
            )
            # Continue past short labels such as 重要提示／一、项目概况.
            if substantive or used >= max_tokens or len(lines) >= 4:
                break
        if before:
            lines.reverse()
        return "\n".join(lines)

    before = gather(range(index - 1, start - 1, -1), True)
    # Include the closest shallow native heading as original text, not an
    # interpretation such as "possibly its child". This keeps internal clauses
    # connected to their real section even when the neighboring sentence is far
    # from the section start.
    native = []
    for i in range(start + 1, index):
        b = blocks[i]
        if b.kind != "paragraph" or len(b.text) > 100:
            continue
        levels = [
            h.get("outline_level")
            for h in b.structure
            if h.get("source") == "word" and h.get("outline_level") is not None
        ]
        if levels:
            native.append((i, min(levels)))
    if native:
        shallow = min(level for _, level in native)
        anchor = next(i for i, level in reversed(native) if level == shallow)
        anchor_text = blocks[anchor].text
        if anchor_text not in before and index - anchor > 1:
            before = anchor_text + "\n…\n" + before
    after = gather(range(index + 1, end))
    return (
        f"摘录 {candidate.id}：\n"
        f"{before}\n\n[{candidate.id}] {candidate.text}\n{after}\n"
    )


def secondary_input(title, directory, items, blocks, positions, max_tokens, start, end):
    text = f"实际任务开始：\n当前章：{title}\n"
    if directory:
        text += "本章目录条目：\n" + "\n".join(directory) + "\n"
    text += "\n请从以下原文摘录中选择本章直属小节的编号。\n\n"
    text += "\n\n".join(
        excerpt(c, blocks, positions, max_tokens, start=start, end=end) for c in items
    )
    return (
        text
        + "\n\n实际任务结束：\n只返回本次实际任务中选中的编号；没有符合项时返回“无”。"
    )


def primary_input(title, items, blocks, positions, max_tokens):
    text = f"实际任务开始：\n目标章节：{title}\n请选择正文起点所在的一个编号。\n\n"
    text += "\n\n".join(excerpt(c, blocks, positions, max_tokens) for c in items)
    return (
        text
        + "\n\n实际任务结束：\n只返回本次实际任务中的一个编号；没有正确位置时返回“无”。"
    )
