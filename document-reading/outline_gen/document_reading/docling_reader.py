"""Native content adapter, preserving nested tables in rich cell order."""

from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
from .models import Block, Cell, CellPart, ParsedDocument, Table


def from_docling_document(document):
    raw = document.export_to_dict()
    blocks, tables, issues = [], {}, []
    raw_tables = {t["self_ref"]: t for t in raw.get("tables", [])}
    objects = {
        str(getattr(t, "self_ref", "")): t for t in getattr(document, "tables", [])
    }
    cache = {}

    def resolve(ref):
        parts = ref.split("/")
        return raw[parts[1]][int(parts[2])]

    def cell_parts(ref, seen=None):
        seen = set() if seen is None else seen
        if ref in seen:
            raise ValueError("Docling rich cell reference cycle")
        node = resolve(ref)
        seen = seen | {ref}
        if node.get("label") == "table":
            return [CellPart("table", table=decode_table(ref))]
        if "text" in node:
            return [CellPart("text", node["text"])]
        result = []
        for child in node.get("children", []):
            result.extend(cell_parts(child["$ref"], seen))
        return result

    def decode_table(ref, item=None):
        if ref in cache:
            return cache[ref]
        data = raw_tables.get(ref, {}).get("data")
        if data is None:
            source = item.data
            data = {
                "num_rows": source.num_rows,
                "num_cols": source.num_cols,
                "table_cells": [vars(c) for c in source.table_cells],
            }
        tid = (
            f'T{int(ref.rsplit("/",1)[-1])+1:04d}'
            if ref.startswith("#/tables/")
            else f"T{len(cache)+1:04d}"
        )
        table = Table(tid, data["num_rows"], data["num_cols"], [])
        cache[ref] = table
        for i, c in enumerate(data["table_cells"]):
            r, col = c["start_row_offset_idx"], c["start_col_offset_idx"]
            parts = cell_parts(c["ref"]["$ref"]) if c.get("ref") else []
            table.cells.append(
                Cell(
                    f"C{i+1:04d}",
                    r,
                    col,
                    str(c["text"]).splitlines() or [""],
                    max(1, c["end_row_offset_idx"] - r),
                    max(1, c["end_col_offset_idx"] - col),
                    bool(c.get("column_header")),
                    bool(c.get("row_header")),
                    parts,
                )
            )
        obj = item or objects.get(ref)
        if obj:
            for attr, method in [
                ("markdown", "export_to_markdown"),
                ("html", "export_to_html"),
            ]:
                try:
                    setattr(table, attr, getattr(obj, method)(doc=document))
                except Exception as exc:
                    issues.append(
                        {
                            "code": "table_export_failed",
                            "table_id": tid,
                            "format": attr,
                            "error": str(exc),
                        }
                    )
        return table

    table_level = None
    for item, level in document.iterate_items():
        if table_level is not None and level > table_level:
            continue
        table_level = None
        label = str(getattr(item, "label", "")).split(".")[-1].lower()
        ref = str(getattr(item, "self_ref", ""))
        if label == "table":
            table_level = level
            table = decode_table(ref, item)
            tables[table.id] = table
            blocks.append(
                Block(f"B{len(blocks)+1:05d}", "table", table_id=table.id, ref=ref)
            )
        elif label == "picture":
            blocks.append(Block(f"B{len(blocks)+1:05d}", "image", ref=ref))
        elif hasattr(item, "text") and str(item.text).strip():
            blocks.append(
                Block(
                    f"B{len(blocks)+1:05d}",
                    "paragraph",
                    str(item.text),
                    ref=ref,
                    structure=[
                        {
                            "source": "docling",
                            "ref": ref,
                            "label": label,
                            "level": getattr(item, "level", None),
                            "tree_depth": level,
                            "parent": str(
                                getattr(getattr(item, "parent", None), "cref", "")
                            ),
                            "marker": getattr(item, "marker", None),
                            "enumerated": getattr(item, "enumerated", None),
                        }
                    ],
                )
            )
    if not blocks:
        raise ValueError("Docling 未返回可读取内容")
    return ParsedDocument(blocks, tables, raw, issues)


def _read_docling_in_process(path: Path):
    try:
        from docling.document_converter import DocumentConverter
        from docling.datamodel.base_models import InputFormat
    except ImportError as exc:
        raise RuntimeError("需要安装 requirements-document-reading.txt") from exc
    result = DocumentConverter(allowed_formats=[InputFormat.DOCX]).convert(path)
    status = str(result.status).split(".")[-1].lower()
    if status not in {"success", "partial_success"}:
        raise RuntimeError(f"Docling 转换失败: {result.status}")
    parsed = from_docling_document(result.document)
    if status != "success" or getattr(result, "errors", []):
        parsed.issues.append(
            {"code": "docling_partial", "errors": [str(x) for x in result.errors]}
        )
    return parsed


def _restore_table(data):
    cells = []
    for item in data["cells"]:
        item = dict(item)
        item["parts"] = [CellPart(part["kind"], part.get("text", ""),
                                  _restore_table(part["table"]) if part.get("table") else None)
                         for part in item.get("parts", [])]
        cells.append(Cell(**item))
    return Table(**{**data, "cells": cells})


def read_docling(path: Path):
    """Keep Docling/native library heaps and threads out of the long-lived server.

    Use a fresh interpreter rather than fork: the server may already have
    multiple Python workers. Named temporary files are individually owned and
    removed on close; no persistent document/cache artifacts are overwritten.
    """
    environment = os.environ.copy()
    # DOCX conversion is CPU parsing, not local model inference. These settings
    # apply only to this worker, before NumPy/BLAS/OpenMP libraries are imported.
    environment.update({name: "1" for name in (
        "OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
        "BLIS_NUM_THREADS", "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS",
    )})
    environment.update(MALLOC_ARENA_MAX="2", TOKENIZERS_PARALLELISM="false")
    # Ensure the worker uses this checkout even when called outside repo cwd.
    project_root = str(Path(__file__).resolve().parents[2])
    environment["PYTHONPATH"] = os.pathsep.join(filter(None, [project_root, environment.get("PYTHONPATH")]))
    with tempfile.NamedTemporaryFile(suffix=".json") as result_file, tempfile.TemporaryFile() as error_file:
        command = [sys.executable, "-m", "outline_gen.document_reading.docling_worker",
                   "--input", str(Path(path).resolve()), "--output", result_file.name]
        try:
            process = subprocess.run(command, env=environment, stdout=subprocess.DEVNULL,
                                     stderr=error_file, timeout=600, check=False)
        except subprocess.TimeoutExpired as exc:
            # subprocess.run kills and waits for its own timed-out worker.
            raise RuntimeError("DOCX解析子进程超过600秒，已终止并释放解析资源") from exc
        if process.returncode:
            error_file.seek(0, os.SEEK_END)
            error_file.seek(max(0, error_file.tell() - 12000))
            detail = error_file.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"DOCX解析子进程失败（exit={process.returncode}）：\n{detail}")
        result_file.seek(0)
        data = json.load(result_file)
    return ParsedDocument(
        blocks=[Block(**block) for block in data["blocks"]],
        tables={key: _restore_table(table) for key, table in data["tables"].items()},
        raw=data["raw"], issues=data["issues"], resource_usage=data["resource_usage"],
    )
