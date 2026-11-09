"""阅读调用器所需的问题记录与可选缓存工具；不含目录生成阶段恢复。"""
from pathlib import Path
from threading import RLock
from outline_gen.runtime.output_issues import issue_context
from outline_gen.io.fs import load_json, write_json
_ISSUE_LOCK = RLock()

def record_generation_fallback(output_dir, stage, error, *, policy="preserve_source_and_structure",
                               operation=None, node_id=None, node_title=None, batch_id=None,
                               material_ids=None, fallback_detail=None, affected_scope=None,
                               evidence=None):
    """Serialize concurrent append operations and retain actionable locations."""
    path = Path(output_dir) / "generation_fallbacks.json"
    context = issue_context(error)
    failures = getattr(error, "failures", [])
    if failures:
        # Preserve every failed batch item's evidence, not just a joined string.
        context["evidence"] = {f"{item.logical_item_id}:{label}": paths
            for item in failures for label, paths in issue_context(item.error).get("evidence", {}).items()}
        context["logical_item_id"] = ", ".join(item.logical_item_id for item in failures)
    with _ISSUE_LOCK:
        entries = load_json(path) if path.exists() else []
        entry = {
            "issue_id": f"GEN-{len(entries) + 1:05d}", "stage": stage,
            "operation": operation or context.get("operation") or stage,
            "node_id": node_id or context.get("node_id"), "node_title": node_title,
            "batch_id": batch_id or context.get("batch_id"),
            "material_ids": list(material_ids or context.get("material_ids") or []),
            "logical_item_id": context.get("logical_item_id") or getattr(error, "logical_item_id", None),
            "request_key": context.get("request_key"),
            "error_type": context.get("error_type", type(error).__name__), "error": str(error), "reason": str(error),
            "attempts": context.get("attempts", {"generation": getattr(error, "attempts", 0),
                                                  "local_repair": None, "model_repair": None}),
            "status": "needs_review", "policy": policy, "fallback_kind": policy,
            "fallback_detail": fallback_detail or {
                "preserve_source_and_structure": "保留原文或已有结构，使用阶段最小后备结果继续。",
                "empty_optional": "可选结果留空，未完成内容不视为通过。",
                "fixed_template": "使用固定技术结构模板，业务事实保持未知。",
                "stop_branch": "保留父节点，停止该分支；失败子树不提交。",
                "retain_source_screening_unverified": "保留同源原文，筛选未完成，内容边界待核。",
            }.get(policy, policy),
            "affected_scope": affected_scope or node_title or node_id or stage,
            "evidence": evidence or context.get("evidence", {}),
        }
        entries.append(entry)
        write_json(path, entries)
    return entry

def read_optional_cache(path, *, output_dir=None, stage="cache"):
    """Malformed optional caches are misses; I/O failures remain I/O errors."""
    try:
        value = load_json(path)
        if not isinstance(value, dict):
            raise ValueError("可选缓存应为JSON对象")
        return value
    except (ValueError, TypeError, RecursionError) as error:
        if output_dir is not None:
            record_generation_fallback(output_dir, stage, error, policy="empty_optional",
                operation="cache_revalidation", evidence={"cache": str(Path(path).resolve())},
                fallback_detail="可选缓存损坏，忽略缓存并重新生成。")
        return {}
