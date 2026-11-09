from outline_gen.runtime.tokenization import selected_tokenizer, set_thread_tokenizer
from outline_gen.runtime.tokenization import count_text_tokens
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from collections import Counter
import re
from .table_render import chunks, render_cells, render_table
from outline_gen.io.fs import write_json, write_text


def numbers(text):
    # Keep decimal/thousands grouping and suffixes as one quantity.
    return Counter(
        re.findall(r"(?<![A-Za-z])\d+(?:,\d{3})*(?:[.．]\d+)*(?:%|％|‰)?", text)
    )


def validate_table(raw, source, background=""):
    text = raw.strip()
    if not text or text == "无" or text.startswith(("{", "```")):
        raise ValueError("直接输出完整展开文字，不要JSON或代码围栏")
    if re.search(r"(?m)^\s*\|.+\||^\s*[^\n|]+\|[^\n|]+\|", text):
        raise ValueError("请将每行对应关系展开成文字，不要继续输出表格")
    if re.search(
        r"(?m)^\s*(?:所属章节|表前文字|表后文字|表后说明|阅读背景)[：:]", text
    ):
        raise ValueError("不要复制阅读背景，只输出待展开表格")
    missing = numbers(source) - numbers(text)
    if missing:
        raise ValueError("原文数字、百分号或出现次数疑似遗漏：" + str(dict(missing)))
    compact = lambda value: re.sub(r"\s+", "", value)
    for line in background.splitlines():
        line = re.sub(r"^(?:前文|后文|章节)[：:]", "", line).strip()
        if (
            len(line) > 60
            and compact(line) not in compact(source)
            and compact(line) in compact(text)
        ):
            raise ValueError("输出复制了阅读背景中的完整段落，请仅保留表格内容")
    return text


def extract_one(table, context, calls):
    directory = calls.output / "tables" / table.id
    for fmt, ext in [("md", "md"), ("html", "html"), ("text", "txt")]:
        write_text(directory / f"input.{ext}", render_table(table, fmt))
    if table.markdown:
        write_text(directory / "native.md", table.markdown)
    if table.html:
        write_text(directory / "native.html", table.html)
    results, errors, audit = [], [], []
    limit = min(
        calls.config.table_chunk_tokens,
        (calls.config.request_max_tokens) - count_text_tokens(context) - 1500,
    )
    fragments = list(chunks(table, max(200, limit), calls.config.table_format))
    for index, (scope, repeated) in enumerate(fragments):
        view = replace(table, cells=scope)
        if len(fragments) > 1:
            view = replace(view, markdown="", html="", modified=True)
        body = render_table(view, calls.config.table_format, native=len(fragments) == 1)
        prompt = "阅读背景（不输出）：\n" + context
        if repeated:
            prompt += "\n共同表头及父项（仅用于解释下方条目的归属）：\n" + render_cells(
                repeated
            )
        prompt += (
            "\n\n待展开表格：\n"
            + body
            + "\n\n待展开表格结束。展开以上内容，必要时沿用共同表头及父项的原文标签说明归属；不要复制阅读背景或补写未提供内容。"
        )
        source = "\n".join(c.text for c in scope)
        chunk = {
            "index": index,
            "cell_ids": [c.id for c in scope],
            "context_cell_ids": [c.id for c in repeated],
            "input_chars": len(prompt),
        }
        try:
            text = calls.ask(
                f"table_{table.id}_{index:04d}",
                "document_table_sys.md",
                prompt,
                lambda raw: validate_table(raw, source, context),
            )
            chunk["status"] = "completed"
            results.append(text)
        except ValueError as exc:
            chunk.update(status="failed", error=str(exc))
            errors.append(
                {
                    "code": "table_extraction_failed",
                    "table_id": table.id,
                    "chunk": index,
                    "error": str(exc),
                }
            )
            results.append("[表格片段展开失败，保留原文]\n" + render_cells(scope))
        audit.append(chunk)
    result = {
        "table_id": table.id,
        "format": calls.config.table_format,
        "status": "incomplete" if errors else "completed",
        "text": "\n\n".join(results),
        "chunks": audit,
        "issues": errors,
    }
    write_json(directory / "result.json", result)
    return result


def extract_tables(document, tables, calls):
    from .structure import section_path

    fragments = document.fragments
    tasks = []
    for i, fragment in enumerate(fragments):
        if fragment.kind != "table":
            continue
        before = "\n".join(
            f.text for f in fragments[max(0, i - 2) : i] if f.kind == "paragraph"
        )[-800:]
        after = "\n".join(
            f.text for f in fragments[i + 1 : i + 3] if f.kind == "paragraph"
        )[:500]
        context = f'章节：{" / ".join(section_path(document,fragment)) or "首章前内容"}\n前文：{before}\n后文：{after}'
        tasks.append((tables[fragment.table_id], context))
    with ThreadPoolExecutor(max_workers=calls.config.workers, initializer=set_thread_tokenizer, initargs=(selected_tokenizer(),)) as pool:
        results = list(pool.map(lambda task: extract_one(*task, calls), tasks))
    return {r["table_id"]: r for r in results}
