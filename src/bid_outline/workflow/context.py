"""Retrieve complete background blocks independently of ABC obligations.

These are lexical/context candidates, not newly inferred obligations. Downstream
semantic tasks decide applicability. No source body is summarized or clipped.
"""
import re


def chapter_constraints(constraints, nodes, root_id):
    """Include restrictions applying to this branch or its top-level boundary."""
    from .outline import descendants
    branch = {n.node_id for n in descendants(nodes, root_id)}
    return [c for c in constraints
            if set(c.affected_node_ids) & branch or c.parent_node_id in branch
            or (c.kind == 'no_add_children' and c.parent_node_id is None)]


def chapter_candidate_views(candidates, existing_nodes):
    """Give each unadopted candidate a local namespace without touching prose."""
    from bid_outline.model.references import map_reference
    known = {n.node_id for n in existing_nodes}
    output = []
    for index, candidate in enumerate(candidates, 1):
        label = f'C{index:02d}'
        records = candidate['records'].get('节点', [])
        mapping = {r['节点']: f"{label}.{r['节点']}" for r in records if r['节点'] not in known}
        output.append({
            '候选编号': label,
            '节点': [{key: map_reference(value, mapping) if key in {'节点', '父节点'} else value
                    for key, value in record.items()} for record in records],
            '未承接事项': candidate['fields'].get('未承接事项', '无'),
            '结构调整建议': candidate['fields'].get('结构调整建议', '无'),
        })
    return output


def ancestor_path(nodes, node_id):
    """Return complete ancestor objects from chapter root to direct parent."""
    lookup = {n.node_id: n for n in nodes}
    path, seen = [], {node_id}
    parent = lookup[node_id].parent_id
    while parent is not None:
        if parent in seen or parent not in lookup:
            raise ValueError('invalid ancestor chain')
        seen.add(parent)
        path.append(lookup[parent])
        parent = lookup[parent].parent_id
    return list(reversed(path))


def background_candidates(materials, requirements, document):
    direct = {mid for req in requirements for mid in req.material_refs}
    elements = {e.element_id: e for e in document.elements} if document else {}

    def sections(material):
        return {tuple(elements[key].section_path) for key in material.get('element_ids', [])
                if key in elements and elements[key].section_path}

    local_sections = {path for key in direct if key in materials for path in sections(materials[key])}
    topics = ' '.join(req.topic for req in requirements)
    # Two-character terms such as 技术/编制 turned every small batch into an
    # almost full-document request. Use longer phrases, preserving local scope.
    terms = set(re.findall(r'[A-Za-z0-9_]{3,}', topics))
    for phrase in re.findall(r'[\u4e00-\u9fff]{4,}', topics):
        terms.update(phrase[i:i+4] for i in range(len(phrase)-3))
    # Cross-chapter prohibitions can constrain every solution (e.g. no public
    # network access). Keep these conservative candidates; page them by budget
    # instead of silently dropping a possibly global restriction.
    global_clues = ('禁止', '不得', '仅允许', '项目范围', '部署环境', '网络环境', '服务范围')
    directory_restriction = re.compile(
        r'(?:目录|章节|技术标|技术文件)[^\n。；]{0,40}(?:不得|禁止|仅允许)'
        r'|(?:不得|禁止|仅允许)[^\n。；]{0,40}(?:增删|增加|删除|目录|章节)')
    selected = []
    for key, material in materials.items():
        if key in direct or material.get('kind') != 'refined_text':
            continue
        text = material['text']
        if (local_sections & sections(material) or any(term in text for term in terms)
                or any(clue in text for clue in global_clues) or directory_restriction.search(text)):
            selected.append(key)
    return sorted(selected)
