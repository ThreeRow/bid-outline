"""Small line protocols. Model responses contain selected rows only."""

import re
from .alignment import normalized


def lines(text):
    value = text.strip()
    if value == "无":
        return []
    if not value or value.startswith("```"):
        raise ValueError("请直接按行输出结果；没有结果只写“无”")
    return [line.strip() for line in value.splitlines() if line.strip()]


def parse_toc(text, max_level):
    result = []
    for line in lines(text):
        m = re.fullmatch(r"(\d+)\s+(.+)", line)
        if not m or not 1 <= int(m[1]) <= max_level:
            raise ValueError("目录每行格式：层级 空格 原文标题")
        result.append({"title": m[2].strip(), "level": int(m[1])})
    return result


def extract_toc(text, calls):
    def validate(raw):
        # The region label has no content or positional ambiguity; remove it
        # deterministically rather than requesting the entire TOC again.
        entries = [
            entry
            for entry in parse_toc(raw, calls.config.max_heading_level)
            if normalized(entry["title"]) not in {"目录", "目次"}
        ]
        for entry in entries:
            if normalized(entry["title"]) not in normalized(text):
                raise ValueError("标题须照抄输入目录")
        expected = [
            re.sub(r"(?:[.．·…]+|\t|\s{2,})\s*\d+\s*$", "", x).strip()
            for x in text.splitlines()
            if re.search(r"(?:[.．·…]{2,}|\t|\s{2,})\s*\d+\s*$", x)
        ]
        omitted = [
            x
            for x in expected
            if normalized(x) not in {normalized(e["title"]) for e in entries}
        ]
        if omitted:
            raise ValueError("目录漏项：" + "；".join(omitted))
        return entries

    return calls.ask(
        "toc",
        "document_toc_sys.md",
        "目录：\n" + text,
        validate,
        max_tokens=calls.config.heading_max_tokens,
    )
