"""Mechanical directory boundaries and preservation policies."""

from __future__ import annotations

from bid_outline.domain import OutlineConstraint, OutlineNode, OutlineVersion, validate_outline

from .common import dump, identity, ids, now


def nodes_from_records(records: list[dict], base: list[OutlineNode], requirements: set[str], salt: str) -> list[OutlineNode]:
    existing = {n.node_id: n for n in base}
    local = {}
    for record in records:
        key = record["节点"]
        if key in local:
            raise ValueError(f"duplicate node: {key}")
        if key in existing:
            local[key] = key
        else:
            import re
            if not re.fullmatch(r"N\d{2,}", key):
                raise ValueError(f"unknown node: {key}")
            local[key] = identity("N_", [salt, key])
    output = []
    for record in records:
        parent = record["父节点"].strip()
        parent_id = None if parent == "ROOT" else local.get(parent, parent)
        if parent_id is not None and parent not in local and parent not in existing:
            raise ValueError(f"unknown parent: {parent}")
        basis = ids(record["依据"])
        if not set(basis) <= requirements:
            raise ValueError("node refers to unknown requirement")
        if not basis:
            raise ValueError("backbone node has no requirement basis")
        output.append(OutlineNode(
            node_id=local[record["节点"]], parent_id=parent_id,
            order=int(record["同级顺序"]), title=record["标题"].strip(),
            basis_requirement_ids=basis, origin_kind=record["形成方式"],
        ))
    return output


def descendants(nodes: list[OutlineNode], root: str, *, include_root: bool = True) -> list[OutlineNode]:
    included = {root}
    changed = True
    while changed:
        before = len(included)
        included.update(n.node_id for n in nodes if n.parent_id in included)
        changed = len(included) > before
    return [n for n in nodes if n.node_id in included and (include_root or n.node_id != root)]


def depth(nodes: list[OutlineNode], node_id: str) -> int:
    lookup = {n.node_id: n for n in nodes}
    count, seen = 0, set()
    while node_id is not None:
        if node_id in seen or node_id not in lookup:
            raise ValueError("invalid ancestor chain")
        seen.add(node_id)
        count += 1
        node_id = lookup[node_id].parent_id
    return count


def preserve(base: OutlineVersion, nodes: list[OutlineNode]) -> None:
    old, new = {n.node_id: n for n in base.nodes}, {n.node_id: n for n in nodes}
    enforce_constraints(base.constraints, nodes)
    for constraint in base.constraints:
        for key in constraint.affected_node_ids:
            if key not in old:
                raise ValueError(f"constraint has unknown node {key}")
            if key not in new:
                raise ValueError(f"preserved node missing: {key}")
            a, b = old[key], new[key]
            if (a.title, a.parent_id) != (b.title, b.parent_id):
                raise ValueError(f"preserved node changed: {key}")
        # Order is relative: insertion need not freeze the numeric order field.
        for parent in {old[k].parent_id for k in constraint.affected_node_ids}:
            keys = {k for k in constraint.affected_node_ids if old[k].parent_id == parent}
            before = sorted(keys, key=lambda k: old[k].order)
            after = sorted(keys, key=lambda k: new[k].order)
            if before != after:
                raise ValueError("preserved sibling order changed")


def make_version(nodes: list[OutlineNode], base: OutlineVersion | None, reason: str,
                 constraints: list[OutlineConstraint] | None = None, *, input_artifact_ids=None) -> OutlineVersion:
    validate_outline(nodes)
    enforce_constraints(constraints if constraints is not None else (base.constraints if base else []), nodes)
    if base:
        preserve(base, nodes)
    return OutlineVersion(
        version_id=identity("V_", [base.version_id if base else None, dump(nodes), reason,
                                   dump(constraints if constraints is not None else (base.constraints if base else [])), input_artifact_ids or []]),
        parent_version_id=base.version_id if base else None, nodes=nodes,
        constraints=constraints if constraints is not None else (base.constraints if base else []),
        commit_reason=reason, created_at=now(), input_artifact_ids=input_artifact_ids or [],
    )


def replace_branch(base: OutlineVersion, root: str, replacement: list[OutlineNode]) -> list[OutlineNode]:
    """Replacement contains descendants, never the boundary root or another chapter."""
    old_scope = {n.node_id for n in descendants(base.nodes, root, include_root=False)}
    outside = {n.node_id for n in base.nodes} - old_scope
    if any(n.node_id in outside for n in replacement):
        raise ValueError("replacement modifies a node outside its scope")
    new_ids = {n.node_id for n in replacement}
    if any(n.parent_id not in new_ids | {root} for n in replacement):
        raise ValueError("replacement escapes boundary parent")
    merged = [n for n in base.nodes if n.node_id not in old_scope] + replacement
    validate_outline(merged)
    preserve(base, merged)
    return merged


def retain_skeleton_nodes(base: OutlineVersion, root: str, candidate: list[OutlineNode]) -> list[OutlineNode]:
    """Generation may add detail but cannot implicitly delete its supplied skeleton.

    Preserve omitted nodes only when the combined candidate remains a valid tree;
    conflicting orders/parents still require correction, never silent renumbering.
    """
    current = {node.node_id for node in candidate}
    retained = [node.model_copy(deep=True) for node in descendants(base.nodes, root, include_root=False)
                if node.node_id not in current]
    result = candidate + retained
    replace_branch(base, root, result)
    return result


def enforce_constraints(constraints: list[OutlineConstraint], nodes: list[OutlineNode]) -> None:
    """Enforce an explicit closed list at exactly one parent, including first adoption."""
    node_ids = {n.node_id for n in nodes}
    for constraint in constraints:
        if constraint.kind == "source_list_sequence":
            lookup = {n.node_id: n for n in nodes}
            members = {m['member_id']: m for m in constraint.sequence_members}
            previous = {}
            for member in sorted(members.values(), key=lambda m: m['source_order']):
                key = member.get('node_id')
                if not key:
                    continue
                if key not in lookup or lookup[key].title != member['title']:
                    raise ValueError('source list member missing or renamed')
                node = lookup[key]
                parent = members.get(member['parent_member_id'])
                expected = parent.get('node_id') if parent else constraint.parent_node_id
                if (parent and not expected) or node.parent_id != expected:
                    raise ValueError('source list hierarchy changed')
                if node.order <= previous.get(node.parent_id, 0):
                    raise ValueError('source list relative order changed')
                previous[node.parent_id] = node.order
            continue
        if constraint.kind != "no_add_children":
            continue
        parent = constraint.parent_node_id
        if parent is not None and parent not in node_ids:
            raise ValueError("restricted parent missing")
        titles = [n.title for n in nodes if n.parent_id == parent]
        if len(titles) != len(set(titles)):
            raise ValueError("source forbids duplicate children in closed list")
        forbidden = [title for title in titles if title not in constraint.allowed_child_titles]
        if forbidden:
            raise ValueError(f"source forbids additional children: {forbidden}")


def closed_list_constraint(record: dict, nodes: list[OutlineNode]) -> OutlineConstraint:
    """Bind a model-identified restriction to an unambiguous candidate parent."""
    parent_title = record["限制父级"]
    parent = None
    if parent_title != "ROOT":
        matches = [n for n in nodes if n.title == parent_title]
        if len(matches) != 1:
            raise ValueError("restricted parent title is missing or ambiguous")
        parent = matches[0].node_id
    titles = [] if record["允许子项"] == "无" else record["允许子项"].splitlines()
    return OutlineConstraint(
        constraint_id=identity("C_", record), kind="no_add_children", origin="文件明确",
        affected_node_ids=[parent] if parent is not None else [], parent_node_id=parent,
        allowed_child_titles=titles, requirement_ids=ids(record["要求依据"]),
        basis=record["原文表达"] + "\n" + record["结构含义"],
    )
