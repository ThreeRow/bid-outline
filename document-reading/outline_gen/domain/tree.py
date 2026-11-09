"""目录树维护：ID 分配、遍历、原子替换子树。"""

from __future__ import annotations

from collections.abc import Iterator

from outline_gen.domain.models import OutlineNode


def walk(node: OutlineNode) -> Iterator[OutlineNode]:
    yield node
    for child in node.children:
        yield from walk(child)


def find_node(root: OutlineNode, node_id: str) -> OutlineNode | None:
    for n in walk(root):
        if n.node_id == node_id:
            return n
    return None


def find_parent(root: OutlineNode, node_id: str) -> OutlineNode | None:
    for n in walk(root):
        for child in n.children:
            if child.node_id == node_id:
                return n
    return None


def depth_of(root: OutlineNode, node_id: str) -> int | None:
    """根深度为 0。"""

    def _dfs(node: OutlineNode, d: int) -> int | None:
        if node.node_id == node_id:
            return d
        for child in node.children:
            found = _dfs(child, d + 1)
            if found is not None:
                return found
        return None

    return _dfs(root, 0)


def assign_child_ids(parent: OutlineNode) -> None:
    """按兄弟序重写子节点 ID：{parent_id}.1 / .2 / ...；根用 N1, N2。"""
    for i, child in enumerate(parent.children, start=1):
        if parent.node_id in ("ROOT", ""):
            child.node_id = f"N{i}"
        else:
            child.node_id = f"{parent.node_id}.{i}"
        assign_child_ids(child)


def detach_node(root: OutlineNode, node_id: str) -> OutlineNode | None:
    """从父节点移除并返回该子树；同时重写原父下剩余子节点 ID。"""
    if root.node_id == node_id:
        raise ValueError("不能 detach 根节点")
    parent = find_parent(root, node_id)
    if parent is None:
        return None
    for i, child in enumerate(parent.children):
        if child.node_id == node_id:
            node = parent.children.pop(i)
            assign_child_ids(parent)
            return node
    return None


def detach_node_obj(root: OutlineNode, node: OutlineNode) -> bool:
    """按对象引用 detach（搬迁前先抓引用，避免兄弟 ID 重编号导致找不到）。"""
    if root is node:
        raise ValueError("不能 detach 根节点")
    for n in walk(root):
        for i, child in enumerate(n.children):
            if child is node:
                n.children.pop(i)
                assign_child_ids(n)
                return True
    return False


def attach_child(
    parent: OutlineNode,
    node: OutlineNode,
    *,
    index: int | None = None,
) -> str:
    """挂到 parent 下并重写 ID，返回新 node_id。"""
    if index is None or index >= len(parent.children):
        parent.children.append(node)
    else:
        parent.children.insert(max(0, index), node)
    assign_child_ids(parent)
    return node.node_id


def is_ancestor(ancestor_id: str, node_id: str) -> bool:
    """ancestor_id 是否为 node_id 的祖先（含相等）。"""
    if ancestor_id == node_id:
        return True
    return node_id.startswith(ancestor_id + ".")


def move_node(
    root: OutlineNode,
    node_id: str,
    new_parent_id: str,
    *,
    index: int | None = None,
) -> dict[str, str]:
    """跨父节点搬迁子树。返回 {from_id, new_parent_id, new_id, title}。"""
    if node_id == new_parent_id or is_ancestor(node_id, new_parent_id):
        raise ValueError(f"非法搬迁: {node_id} → 父 {new_parent_id}")
    new_parent = find_node(root, new_parent_id)
    if new_parent is None:
        raise ValueError(f"新父节点不存在: {new_parent_id}")
    old_parent = find_parent(root, node_id)
    if old_parent is None:
        raise ValueError(f"待搬迁节点不存在或无父: {node_id}")
    existing = find_node(root, node_id)
    if existing is None:
        raise ValueError(f"待搬迁节点不存在: {node_id}")
    if old_parent.node_id == new_parent_id:
        return {
            "from_id": node_id,
            "new_parent_id": new_parent_id,
            "new_id": node_id,
            "title": existing.title,
            "status": "already_under_parent",
        }

    node = detach_node(root, node_id)
    if node is None:
        raise ValueError(f"detach 失败: {node_id}")
    title = node.title
    new_id = attach_child(new_parent, node, index=index)
    return {
        "from_id": node_id,
        "new_parent_id": new_parent_id,
        "new_id": new_id,
        "title": title,
        "status": "moved",
    }


def replace_subtree(root: OutlineNode, node_id: str, new_node: OutlineNode) -> bool:
    """原子替换目标节点（含其整棵子树）。成功返回 True。"""
    if root.node_id == node_id:
        raise ValueError("不允许直接替换根节点；请整体替换 RunState.tree")
    parent = find_parent(root, node_id)
    if parent is None:
        return False
    for i, child in enumerate(parent.children):
        if child.node_id == node_id:
            new_node.node_id = node_id
            parent.children[i] = new_node
            return True
    return False


def collect_expandable(
    root: OutlineNode,
    *,
    max_depth: int,
    force_to_depth: bool = True,
) -> list[OutlineNode]:
    """返回应展开的叶子节点。

    force_to_depth=True（默认）：凡 depth < max_depth 的非根叶子一律展开，
    忽略模型留下的 expandable=false，使路径尽量顶满目标深度。
    force_to_depth=False：仅 expandable=true 的叶子。
    """
    out: list[OutlineNode] = []
    stopped = [node.node_id for node in walk(root) if node.expand_stop_reason]
    for n in walk(root):
        if any(n.node_id == parent or n.node_id.startswith(parent + ".") for parent in stopped):
            continue
        if n.node_id == "ROOT":
            continue
        d = depth_of(root, n.node_id)
        if d is None or d >= max_depth or n.children:
            continue
        if any(note.startswith("[重复章节折叠]") for note in n.review_notes):
            continue
        if force_to_depth or n.expandable:
            out.append(n)
    return out


def outline_markdown(root: OutlineNode) -> str:
    """导出可读目录（标题树）。"""
    lines: list[str] = []

    def _rec(node: OutlineNode, level: int) -> None:
        if node.node_id == "ROOT":
            for child in node.children:
                _rec(child, 1)
            return
        prefix = "  " * (level - 1)
        lines.append(f"{prefix}{node.node_id} {node.title}")
        for child in node.children:
            _rec(child, level + 1)

    _rec(root, 0)
    return "\n".join(lines) + ("\n" if lines else "")


def duplicate_title_groups(root: OutlineNode) -> list[dict[str, object]]:
    """Find exact semantic-title repeats across separate branches."""
    import re

    groups: dict[str, list[OutlineNode]] = {}
    for node in walk(root):
        if node.node_id == "ROOT":
            continue
        key = re.sub(r"[\W_]+", "", node.title).casefold()
        if key:
            groups.setdefault(key, []).append(node)
    return [
        {"title": nodes[0].title, "node_ids": [node.node_id for node in nodes]}
        for nodes in groups.values()
        if len(nodes) > 1
    ]


def merge_exact_duplicate_leaves(
    root: OutlineNode, *, within_score_owner: bool = False
) -> list[dict[str, str]]:
    """Fold exact repeated leaf chapters into the shallower established owner."""
    import re

    seen: dict[str, OutlineNode] = {}
    merges: list[dict[str, str]] = []
    nodes = [node for node in walk(root) if node.node_id != "ROOT"]
    nodes.sort(key=lambda node: (depth_of(root, node.node_id) or 0))
    affected: list[OutlineNode] = []
    for node in nodes:
        key = re.sub(r"[\W_]+", "", node.title).casefold()
        if not key:
            continue
        owner = seen.get(key)
        if owner is None:
            seen[key] = node
            continue
        if within_score_owner:
            from .score_ownership import score_owner
            first = score_owner(root, owner.node_id)
            second = score_owner(root, node.node_id)
            if (first.node_id if first else None) != (second.node_id if second else None):
                continue
        if node.children or node is owner:
            continue
        parent = find_parent(root, node.node_id)
        if parent is None:
            continue
        old_id = node.node_id
        owner.score_refs = list(dict.fromkeys([*owner.score_refs, *node.score_refs]))
        owner.source_refs = list(dict.fromkeys([*owner.source_refs, *node.source_refs]))
        owner.review_notes.append(f"[重复章节合并] {old_id} 的独有要点并入本节点")
        existing_lines = {line.strip() for line in owner.writing_outline.splitlines() if line.strip()}
        extra_lines = [
            line for line in node.writing_outline.splitlines()
            if line.strip() and line.strip() not in existing_lines
        ]
        if extra_lines:
            owner.writing_outline = "\n".join([owner.writing_outline, *extra_lines]).strip()
            owner.refresh_node_content()
        parent.children = [child for child in parent.children if child is not node]
        parent.review_notes.append(f"[重复章节合并] {old_id} 并入 {owner.node_id}")
        if not parent.children:
            parent.expandable = False
            parent.review_notes.append("[重复章节折叠] 本分支已由其他章节承接")
        affected.append(parent)
        merges.append({"removed_node_id": old_id, "owner_node_id": owner.node_id, "title": node.title})
    for parent in affected:
        assign_child_ids(parent)
    return merges
