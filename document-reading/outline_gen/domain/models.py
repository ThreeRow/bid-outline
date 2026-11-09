"""目录树与中间产物的数据结构（程序侧契约）。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


def normalize_detail_level(value: Any, default: int = 3) -> int:
    try:
        level = int(value)
    except (TypeError, ValueError):
        level = default
    return max(1, min(5, level))


def assemble_node_content(
    *,
    summary: str = "",
    split_plan: str = "",
    writing_outline: str = "",
) -> str:
    """由拆分字段组装兼容用 node_content（不含字数）。"""
    parts: list[str] = []
    if (summary or "").strip():
        parts.append("## 概述\n" + summary.strip())
    if (split_plan or "").strip():
        parts.append("## 拆分指引\n" + split_plan.strip())
    if (writing_outline or "").strip():
        parts.append("## 撰写大纲\n" + writing_outline.strip())
    return "\n\n".join(parts)


def truncate_writing_outline(outline: str, *, max_chars: int = 400) -> str:
    """expand 用：撰写大纲摘要/截断。"""
    text = (outline or "").strip()
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 20].rstrip() + "\n…（撰写大纲已截断）"


@dataclass
class OutlineNode:
    """章节工作卡。"""

    node_id: str
    title: str
    summary: str = ""
    split_plan: str = ""
    writing_outline: str = ""
    target_words: int = 0
    target_leaves: int = 0  # 本节点子树的大致四级叶节点目标
    node_content: str = ""  # 程序组装；模型不直出
    source_refs: list[str] = field(default_factory=list)
    score_refs: list[str] = field(default_factory=list)
    priority: str = "medium"  # high / medium / low
    detail_level: int = 3  # 1..5，模型判断写作投入，程序用于字数权重
    expandable: bool = True
    expand_stop_reason: str = ""
    # 仅一级预设节点使用；空值表示普通模型节点。
    preset_title: str = ""
    preset_original_title: str = ""
    review_notes: list[str] = field(default_factory=list)
    children: list[OutlineNode] = field(default_factory=list)

    def refresh_node_content(self) -> None:
        self.node_content = assemble_node_content(
            summary=self.summary,
            split_plan=self.split_plan,
            writing_outline=self.writing_outline,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "title": self.title,
            "summary": self.summary,
            "split_plan": self.split_plan,
            "writing_outline": self.writing_outline,
            "target_words": int(self.target_words or 0),
            "target_leaves": int(self.target_leaves or 0),
            "node_content": self.node_content,
            "source_refs": list(self.source_refs),
            "score_refs": list(self.score_refs),
            "priority": self.priority,
            "detail_level": int(self.detail_level or 3),
            "expandable": self.expandable,
            "expand_stop_reason": self.expand_stop_reason,
            "preset_title": self.preset_title,
            "preset_original_title": self.preset_original_title,
            "review_notes": list(self.review_notes),
            "children": [c.to_dict() for c in self.children],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OutlineNode:
        children = [cls.from_dict(c) for c in data.get("children") or []]
        summary = str(data.get("summary") or "").strip()
        split_plan = str(data.get("split_plan") or "").strip()
        writing_outline = str(data.get("writing_outline") or "").strip()
        legacy_content = str(data.get("node_content") or "").strip()
        # 旧产物仅有 node_content：整段落入 summary
        if not summary and not split_plan and not writing_outline and legacy_content:
            summary = legacy_content
        tw_raw = data.get("target_words", 0)
        try:
            target_words = int(tw_raw or 0)
        except (TypeError, ValueError):
            target_words = 0
        try:
            target_leaves = int(data.get("target_leaves") or 0)
        except (TypeError, ValueError):
            target_leaves = 0
        node = cls(
            node_id=str(data["node_id"]),
            title=str(data.get("title") or ""),
            summary=summary,
            split_plan=split_plan,
            writing_outline=writing_outline,
            target_words=max(0, target_words),
            target_leaves=max(0, target_leaves),
            node_content="",
            source_refs=list(data.get("source_refs") or []),
            score_refs=list(data.get("score_refs") or []),
            priority=str(data.get("priority") or "medium"),
            detail_level=normalize_detail_level(data.get("detail_level")),
            expandable=bool(data.get("expandable", True)),
            expand_stop_reason=str(data.get("expand_stop_reason") or ""),
            preset_title=str(data.get("preset_title") or "").strip(),
            preset_original_title=str(data.get("preset_original_title") or "").strip(),
            review_notes=list(data.get("review_notes") or []),
            children=children,
        )
        node.refresh_node_content()
        return node


def node_from_model_fields(
    *,
    node_id: str,
    title: str,
    summary: str,
    split_plan: str,
    writing_outline: str,
    target_words: int = 0,
    source_refs: list[str] | None = None,
    score_refs: list[str] | None = None,
    priority: str = "medium",
    detail_level: int = 3,
    expandable: bool = True,
    preset_title: str = "",
    preset_original_title: str = "",
    children: list[OutlineNode] | None = None,
    review_notes: list[str] | None = None,
) -> OutlineNode:
    """从模型字段构造节点并组装 node_content。"""
    node = OutlineNode(
        node_id=node_id,
        title=title,
        summary=(summary or "").strip(),
        split_plan=(split_plan or "").strip(),
        writing_outline=(writing_outline or "").strip(),
        target_words=max(0, int(target_words or 0)),
        source_refs=list(source_refs or []),
        score_refs=list(score_refs or []),
        priority=priority,
        detail_level=normalize_detail_level(detail_level),
        expandable=expandable,
        preset_title=(preset_title or "").strip(),
        preset_original_title=(preset_original_title or "").strip(),
        children=list(children or []),
        review_notes=list(review_notes or []),
    )
    node.refresh_node_content()
    return node


@dataclass
class RoutedContext:
    """Context Router 输出：系统信息 + 特型信息。"""

    system_brief: str = ""
    special_info: dict[str, str] = field(default_factory=dict)
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RoutedContext:
        return cls(
            system_brief=str(data.get("system_brief") or ""),
            special_info=dict(data.get("special_info") or {}),
            notes=str(data.get("notes") or ""),
        )


@dataclass
class ReviewFinding:
    """单条审查意见（审查 agent 不直接改树）。"""

    kind: str  # 覆盖 / 结构 / 深度 / 边界 / 字数 / 拆分
    target_node_id: str | None
    message: str
    action: str = ""  # merge / split / add / deepen / move / rewrite / ...

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReviewFinding:
        return cls(
            kind=str(data.get("kind") or ""),
            target_node_id=data.get("target_node_id"),
            message=str(data.get("message") or ""),
            action=str(data.get("action") or ""),
        )


@dataclass
class RunState:
    """一次流水线运行的内存态。"""

    case_id: str
    canonical_md: str = ""
    source_index: dict[str, Any] = field(default_factory=dict)
    score_guidance: dict[str, dict[str, Any]] = field(default_factory=dict)
    score_interpretation: str = ""
    performance_score_ids: list[str] = field(default_factory=list)
    excluded_material_score_ids: list[str] = field(default_factory=list)
    routed: RoutedContext = field(default_factory=RoutedContext)
    cognitive_map: str = ""
    tree: OutlineNode | None = None
    review_findings: list[ReviewFinding] = field(default_factory=list)
    coverage_matrix: dict[str, Any] = field(default_factory=dict)
    version: int = 0
    call_count: int = 0
    total_words: int = 0  # 技术标正文约数；0 表示未启用字数预算
    # 规范化后的预设一级标题及其匹配/保护审计。
    preset_l1_titles: list[str] = field(default_factory=list)
    preset_metadata: dict[str, Any] = field(default_factory=dict)
    # Safe generation views produced by new reading/ABC preprocessing. These
    # IDs describe material scope, not proven semantic source coverage.
    integration_mode: bool = False
    technical_materials: dict[str, dict[str, Any]] = field(default_factory=dict)

    def tree_dict(self) -> dict[str, Any] | None:
        return None if self.tree is None else self.tree.to_dict()
