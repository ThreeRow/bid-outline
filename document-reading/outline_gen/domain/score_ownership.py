"""Structural score ownership for the L1/L2 outline skeleton."""

from __future__ import annotations

from typing import Sequence

from .models import OutlineNode
from .tree import walk


def score_owner(root: OutlineNode, node_id: str) -> OutlineNode | None:
    """Find the nearest scored ancestor, including the requested node."""
    def visit(node: OutlineNode, owner: OutlineNode | None) -> OutlineNode | None:
        current = node if node.score_refs else owner
        if node.node_id == node_id:
            return current
        for child in node.children:
            found = visit(child, current)
            if found is not None:
                return found
        return None
    return visit(root, None)


def skeleton_signature(root: OutlineNode) -> tuple[tuple[str, str, tuple[str, ...], tuple], ...]:
    return tuple(
        (node.node_id, node.title, tuple(node.score_refs), tuple(
            (child.node_id, child.title, tuple(child.score_refs)) for child in node.children
        ))
        for node in root.children
    )


def validate_score_ownership(root: OutlineNode, score_ids: Sequence[str], *, strict: bool = True) -> None:
    try:
        _validate_score_ownership(root, score_ids)
    except ValueError as exc:
        if strict:
            raise
        note = f"[生成告警] {exc}"
        if note not in root.review_notes:
            root.review_notes.append(note)


def _validate_score_ownership(root: OutlineNode, score_ids: Sequence[str]) -> None:
    expected = list(dict.fromkeys(s.upper() for s in score_ids))
    owners: dict[str, OutlineNode] = {}
    scored_order: list[str] = []
    for node in walk(root):
        if node.node_id == "ROOT":
            continue
        depth = node.node_id.count(".") + 1
        if node.score_refs and depth > 2:
            raise ValueError(f"三级以下节点不得挂评分点: {node.node_id}")
        for raw in node.score_refs:
            score_id = raw.upper()
            if score_id not in expected:
                raise ValueError(f"目录引用了未知评分点: {score_id}")
            if score_id in owners:
                raise ValueError(f"评分点 {score_id} 重复承接: {owners[score_id].node_id}, {node.node_id}")
            owners[score_id] = node
            scored_order.append(score_id)
    missing = [score_id for score_id in expected if score_id not in owners]
    if missing:
        raise ValueError(f"评分点缺少承接章: {missing}")
    if scored_order != expected:
        raise ValueError(f"评分点目录顺序与原文不一致: {scored_order} != {expected}")
