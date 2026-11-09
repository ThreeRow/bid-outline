"""Read-only rendering of adopted results; no model invocations."""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4


def write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    with temporary.open("x", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def export_result(result: dict, output: Path) -> dict[str, str]:
    output = Path(output)
    outline = result.get("outline") or {}
    nodes = outline.get("nodes", [])
    lines = ["# 技术标骨干目录", "", f"运行状态：{result['status']}", "",
             result.get("completion_scope", ""), ""]
    def visit(parent, prefix):
        children = sorted((n for n in nodes if n["parent_id"] == parent), key=lambda n: n["order"])
        for index, node in enumerate(children, 1):
            numbering = prefix + [index]
            # Keep node titles as literal one-line labels rather than executable Markdown.
            title = node["title"].replace("\n", " ").replace("\r", " ")
            lines.append("  " * len(prefix) + "- " + ".".join(map(str, numbering)) + " " + title)
            visit(node["node_id"], numbering)
    visit(None, [])
    if not nodes:
        lines.append("尚无可采用骨干目录。")
    extensions = ["# 扩展建议（不属于骨干）", ""]
    for suggestion in result.get("extensions", []):
        if suggestion["review_status"] == "删除":
            continue
        extensions.extend([f"## {suggestion['topic']}", "", f"审核：{suggestion['review_status']}", "",
                           suggestion["content"], "", "适用条件：" + suggestion["conditions"], ""])
    if len(extensions) == 2:
        extensions.append("无已输出的扩展建议。")
    unresolved = ["# 未解决事项", "", f"运行状态：{result['status']}", ""]
    items = result.get("issues", []) + result.get("unprocessed_ranges", []) + [
        f for f in result.get("findings", []) if f["status"] == "open"]
    for item in items:
        unresolved.extend(["```json", json.dumps(item, ensure_ascii=False, indent=2), "```", ""])
    if not items:
        unresolved.append("无已记录的未解决事项；不代表全文语义已经验收。")
    files = {"backbone.md": "\n".join(lines) + "\n", "extensions.md": "\n".join(extensions) + "\n",
             "unresolved.md": "\n".join(unresolved) + "\n",
             "provenance.json": json.dumps(result, ensure_ascii=False, indent=2) + "\n"}
    for name, content in files.items():
        write_atomic(output / name, content)
    return {name: str((output / name).resolve()) for name in files}
