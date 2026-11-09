"""Independent DOCX reading orchestration; no score/FACT transformations."""

from __future__ import annotations
from outline_gen.runtime.tokenization import tokenizer_identity
from outline_gen.runtime.call_policy import configured_tokens


from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
import json

from outline_gen.io.fs import write_json
from .alignment import align
from .config import ReadingConfig
from .docling_reader import read_docling
from .heading_candidates import find_toc, split_lines
from .heading_recognition import extract_toc
from .layered_headings import recognize_layered
from .model_calls import ModelCalls
from .models import payload
from .structure import organize_document
from .export import export_structure, export_reading
from .sections import build_sections
from .table_extraction import extract_tables
from .validation import validate_document, validate_structure
from .word_structure import read_word_structure


@configured_tokens
def read_document(
    input_path: Path,
    output_dir: Path,
    *,
    runtime,
    config=None,
    docling_reader=read_docling,
    progress_callback=None,
):
    config = config or ReadingConfig()
    input_path, output_dir = Path(input_path).resolve(), Path(output_dir).resolve()
    if input_path.suffix.lower() != ".docx" or not input_path.is_file():
        raise ValueError("输入必须为存在的 DOCX 文件")
    digest = sha256(input_path.read_bytes()).hexdigest()
    manifest_path = output_dir / "manifest.json"
    if output_dir.exists() and any(output_dir.iterdir()):
        if not config.resume or not manifest_path.exists():
            raise ValueError("输出目录非空；请使用新目录或 --resume")
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        if previous.get("tokenization") != tokenizer_identity(config.tokenizer):
            raise ValueError("tokenizer或预算策略变化，请使用新输出目录")
        if previous.get("input_sha256") != digest:
            raise ValueError("不能从不同输入文件恢复")
    manifest = {
        "version": 1,
        "input": str(input_path),
        "input_sha256": digest,
        "config": asdict(config),
        "tokenization": tokenizer_identity(config.tokenizer),
        "status": "running",
        "stage": "parsing",
    }
    write_json(manifest_path, manifest)

    def stage(name):
        manifest["stage"] = name
        write_json(manifest_path, manifest)
        if progress_callback:
            progress_callback(name)

    try:
        stage("parsing")
        parsed = docling_reader(input_path)
        if parsed.resource_usage:
            write_json(output_dir / "parsing/resource_usage.json", parsed.resource_usage)
        write_json(output_dir / "parsing/docling.json", parsed.raw)
        words, word_issues = read_word_structure(input_path)
        write_json(output_dir / "parsing/word_structure.json", payload(words))
        parsed.issues.extend(word_issues)
        stage("alignment")
        records = align(parsed, words)
        write_json(output_dir / "parsing/alignment.json", records)
        word_by_id = {w.id: w for w in words}
        word_map = {}
        for record in records:
            for bid in record["block_ids"]:
                word_map.setdefault(bid, []).extend(
                    word_by_id[wid] for wid in record["word_ids"]
                )
        parsed.blocks = split_lines(parsed.blocks, word_map, records)
        write_json(output_dir / "parsing/alignment.json", records)
        calls = ModelCalls(runtime, config, output_dir)
        stage("headings")
        toc_ids, toc_text = find_toc(parsed.blocks, word_map, config, parsed.tables)
        toc = []
        if toc_text:
            try:
                toc = extract_toc(toc_text, calls)
            except ValueError as exc:
                parsed.issues.append(
                    {"code": "toc_extraction_failed", "error": str(exc)}
                )
        # Failed/empty TOC extraction must not suppress actual body candidates.
        excluded_toc = toc_ids if toc else []
        write_json(
            output_dir / "headings/toc.json", {"block_ids": toc_ids, "entries": toc}
        )
        heading_result = recognize_layered(
            parsed.blocks, toc, excluded_toc, word_map, calls
        )
        candidates, decisions = heading_result.candidates, heading_result.decisions
        parsed.issues.extend(heading_result.issues)
        write_json(output_dir / "headings/candidates.json", payload(candidates))
        write_json(
            output_dir / "headings/decisions.json",
            {"selected": decisions, "calls": heading_result.audit},
        )
        sections = build_sections(parsed.blocks, candidates, decisions)
        stage("structure")
        corrected = organize_document(
            parsed.blocks, sections, candidates=candidates, issues=parsed.issues
        )
        structure_validation = validate_structure(parsed.blocks, corrected)
        write_json(output_dir / "structure/validation.json", structure_validation)
        if structure_validation["issues"]:
            raise ValueError("结构组织未通过原文覆盖检查")
        export_structure(output_dir, corrected, parsed.tables)
        stage("tables")
        tables = extract_tables(corrected, parsed.tables, calls)
        stage("finalize")
        validation = validate_document(
            parsed.blocks, tables, sections, decisions, candidates, parsed.issues
        )
        source_map = {
            "alignment": records,
            "word_elements": payload(words),
            "docling_refs": {b.id: b.ref for b in parsed.blocks if b.ref},
            "fragment_sources": [
                {
                    "fragment_id": f.id,
                    "block_id": f.block_id,
                    "start_offset": f.start_offset,
                    "end_offset": f.end_offset,
                }
                for f in corrected.fragments
            ],
            "images_skipped": [b.id for b in parsed.blocks if b.kind == "image"],
        }
        write_json(
            output_dir / "document.json",
            {
                "version": 2,
                "corrected": payload(corrected),
                "blocks": payload(parsed.blocks),
                "sections": payload(sections),
                "tables": tables,
                "source_tables": payload(parsed.tables),
            },
        )
        write_json(output_dir / "source_map.json", source_map)
        write_json(output_dir / "validation.json", validation)
        write_json(output_dir / "model_call_metrics.json", calls.metrics())
        export_reading(output_dir, corrected, tables)
        manifest.update(status=validation["status"], stage="finished")
        write_json(manifest_path, manifest)
        return validation
    except Exception as exc:
        if "calls" in locals():
            write_json(output_dir / "model_call_metrics.json", calls.metrics())
        manifest.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        write_json(manifest_path, manifest)
        raise
