"""Bounded, material-local list evidence; never infer list identity from a batch."""
import re

from bid_outline.domain import OutlineConstraint
from .common import identity, ids

class SourceListConflict(ValueError):
    pass


LIST_FIELDS = ('清单材料', '清单起点', '清单终点', '清单成员')


def source_list(record, materials):
    if not all(record.get(key, '无') != '无' for key in LIST_FIELDS):
        raise ValueError('missing bounded list evidence')
    mid = record['清单材料']
    if mid not in materials:
        raise ValueError('list material unavailable')
    text = materials[mid]['text']
    start, stop = record['清单起点'], record['清单终点']
    if text.count(start) != 1 or text.count(stop) != 1 or start == stop:
        raise ValueError('list anchors must be unique and distinct')
    left, right = text.index(start) + len(start), text.index(stop)
    if left >= right:
        raise ValueError('list anchor order is invalid')
    excerpt = text[left:right]
    members, stack = [], []
    for line in record['清单成员'].splitlines():
        match = re.fullmatch(r'([1-4])\|(.+)', line)
        if not match:
            raise ValueError('list member requires level|title')
        level, title = int(match[1]), match[2]
        if excerpt.count(title) != 1:
            raise ValueError('list title occurrence ambiguous within bounded material')
        offset = left + excerpt.index(title)
        if members and offset <= members[-1]['source_order']:
            raise ValueError('list member order contradicts bounded material')
        if level > len(stack) + 1:
            raise ValueError('list member hierarchy skips a parent')
        stack = stack[:level-1]
        parent = stack[-1] if stack else None
        key = identity('LM_', [mid, left, right, offset])
        members.append({'member_id': key, 'title': title, 'level': level,
                        'parent_member_id': parent, 'source_order': offset})
        stack.append(key)
    if not members:
        raise ValueError('empty list evidence')
    return identity('LIST_', [mid, left, right]), mid, members


def bind_source_list(record, materials, requirements, nodes, constraints):
    source_id, mid, members = source_list(record, materials)
    a_ids = {r.requirement_id for r in requirements if 'A' in r.categories and mid in r.material_refs}
    if not set(ids(record['要求依据'])) & a_ids:
        raise ValueError('list evidence has no matching A requirement')
    previous = next((c for c in constraints if c.source_identity == source_id), None)
    merged = {m['member_id']: dict(m) for m in previous.sequence_members} if previous else {}
    for member in members:
        old = merged.get(member['member_id'])
        if old and any(old[k] != member[k] for k in ('title', 'level', 'parent_member_id', 'source_order')):
            raise SourceListConflict('list evidence conflicts with confirmed member hierarchy')
        merged[member['member_id']] = {**member, **({'node_id': old['node_id']} if old and old.get('node_id') else {})}
    ordered = sorted(merged.values(), key=lambda m: m['source_order'])
    by_member = {m['member_id']: m for m in ordered}
    anchor_parent = previous.parent_node_id if previous else None
    anchor_known = previous is not None and any(m.get('node_id') for m in previous.sequence_members)
    for member in ordered:
        matches = [n for n in nodes if n.title == member['title'] and set(n.basis_requirement_ids) & a_ids]
        if len(matches) > 1:
            raise ValueError('list member cannot bind uniquely to directory')
        if not matches:
            if member.get('node_id'):
                raise SourceListConflict('confirmed list member disappeared')
            continue  # Another batch may create this explicitly recorded member.
        node = matches[0]
        if member.get('node_id') and member['node_id'] != node.node_id:
            raise SourceListConflict('list member changed stable node identity')
        parent_member = by_member.get(member['parent_member_id'])
        if parent_member is None and not anchor_known:
            anchor_parent, anchor_known = node.parent_id, True
        expected_parent = parent_member.get('node_id') if parent_member else anchor_parent
        if parent_member and not expected_parent:
            raise ValueError('list parent is not yet bound')
        if node.parent_id != expected_parent:
            raise SourceListConflict('list hierarchy conflicts with directory')
        member['node_id'] = node.node_id
    return OutlineConstraint(
        constraint_id=identity('C_', ['source-list', source_id]), kind='source_list_sequence', origin='系统策略',
        source_identity=source_id, material_id=mid, sequence_members=ordered, parent_node_id=anchor_parent,
        affected_node_ids=[m['node_id'] for m in ordered if m.get('node_id')],
        requirement_ids=sorted(a_ids), basis='精炼材料内有界清单顺序及层级；不是逐字原DOCX验证',
    )
