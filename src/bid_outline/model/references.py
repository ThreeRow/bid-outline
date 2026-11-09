"""Field-aware references shared by presentation, validation and ID restoration.

Only program-created structured views are traversed. Strings are never parsed as
objects, even if source prose happens to look like JSON or a reference record.
"""
from dataclasses import dataclass
import json
import re


@dataclass(frozen=True)
class ReferenceField:
    groups: tuple[str, ...]
    scalar: bool = False
    root: bool = False
    local: bool = False
    experience: bool = False


REQ = ReferenceField(('requirements',))
ONE_REQ = ReferenceField(('requirements',), scalar=True)
NODE = ReferenceField(('nodes',))
ONE_NODE = ReferenceField(('nodes',), scalar=True)
MATERIAL = ReferenceField(('materials', 'elements'))
NODE_FIELDS = {
    '节点': ReferenceField(('nodes',), scalar=True, local=True),
    '父节点': ReferenceField(('nodes',), scalar=True, root=True, local=True),
    '依据': ReferenceField(('requirements', 'support_requirements')),
}
RECORD_REFERENCES = {
    '要求': {'材料依据': MATERIAL},
    '判定': {'要求': ONE_REQ},
    '结构依据': {'要求依据': REQ, '清单材料': ReferenceField(('materials', 'elements'), scalar=True)},
    '节点': NODE_FIELDS,
    '归属': {'要求': ONE_REQ, '承接节点': NODE},
    '调整建议': {'要求': ONE_REQ, '目标或父节点': ReferenceField(('nodes',), scalar=True, root=True)},
    '覆盖检查': {'要求': ONE_REQ, '承接节点': NODE},
    '问题': {'涉及节点': NODE, '涉及要求': REQ},
    '建议': {'对应章节': ONE_NODE, '相关依据': ReferenceField(('materials', 'elements'), experience=True)},
    '建议检查': {'建议': ReferenceField(('suggestions',), scalar=True)},
}
TOP_REFERENCES = {
    'P06': {'采用候选': ReferenceField(('elements',))},
    'P11': {'章节': ONE_NODE}, 'P12': {'章节': ONE_NODE},
    'P13': {'涉及节点': NODE, '涉及要求': REQ},
}
TASK_RECORDS = {'P04-A': '要求', 'P04-B': '要求', 'P04-C': '要求', 'P05': '判定',
                'P07': '结构依据', 'P08': '节点', 'P09': '归属', 'P10': '调整建议',
                'P11': '节点', 'P12': '节点', 'P14': '覆盖检查', 'P15': '问题',
                'P16': '节点', 'P17': '建议', 'P18': '建议检查'}


def output_contract(task):
    return TOP_REFERENCES.get(task, {}), RECORD_REFERENCES.get(TASK_RECORDS.get(task), {})


def map_reference(value, mapping):
    """Preserve list punctuation; replace complete ID tokens only in declared fields."""
    return re.sub(r'[A-Za-z0-9][A-Za-z0-9_.:/-]*', lambda m: mapping.get(m[0], m[0]), value)


def restore_output(task, parsed, mapping):
    # P19 can carry another task's validated result. Record roles remain explicit.
    top = TOP_REFERENCES.get(task, {}) if task != 'P19' else {
        key: value for fields in TOP_REFERENCES.values() for key, value in fields.items()}
    return {**parsed,
            'fields': {k: map_reference(v, mapping) if k in top else v
                       for k, v in parsed['fields'].items()},
            'records': {kind: [{k: map_reference(v, mapping) if k in RECORD_REFERENCES.get(kind, {}) else v
                               for k, v in record.items()} for record in records]
                        for kind, records in parsed['records'].items()}}


# Internal fields carry IDs, never semantic prose. Titles, basis, evidence,
# content, conditions, record descriptions and source snippets are NOT included.
INPUT_REFERENCE_FIELDS = {
    'requirement_id', 'material_id', 'element_id', 'node_id', 'parent_id',
    'suggestion_id', 'chapter_id', 'version_id', 'parent_version_id', 'outline_version_id',
    'base_version_id', 'constraint_id', 'finding_id', 'document_id', 'source_chunk_id',
    'material_refs', 'basis_requirement_ids', 'requirement_ids', 'node_ids',
    'affected_node_ids', 'parent_node_id', 'suggested_scope', 'element_ids',
    'supplement_material_ids',
}
SECTION_REFERENCE_FIELDS = {'基准版本', '版本', '继承的父级要求ID'}


class Presented(str):
    """Compatibility string plus a program-created, JSON-serializable input view."""
    def __new__(cls, view):
        value = super().__new__(cls, render_view(view))
        value.view = view
        return value


def prepare_call_inputs(inputs):
    """One alias projection for planning, retries and execution."""
    inputs = prepare_inputs(inputs)
    if inputs.get('id_aliases') or not inputs.get('short_ids'):
        return inputs
    aliases, counters = {}, {}
    groups = inputs.get('id_groups', {})
    labels = {'requirements': 'REQ', 'support_requirements': 'REQ', 'materials': 'MAT',
              'nodes': 'NODE', 'elements': 'SRC', 'suggestions': 'SUG'}
    for ident in inputs.get('allowed_ids', []):
        group = next((g for g in labels if ident in groups.get(g, [])), None)
        label = labels.get(group) or next((label for prefix, label in (
            ('R_', 'REQ'), ('M_', 'MAT'), ('N_', 'NODE'), ('E_', 'SUG'))
            if ident.startswith(prefix) and len(ident) > 10), None)
        if label and len(ident) > 10:
            counters[label] = counters.get(label, 0) + 1
            alias = label + f'{counters[label]:02d}'
            while alias in inputs.get('allowed_ids', []) or alias in aliases.values():
                counters[label] += 1
                alias = label + f'{counters[label]:02d}'
            aliases[ident] = alias
    return {**inputs, 'id_aliases': aliases}


def input_record_fields(obj):
    if '节点' in obj and '父节点' in obj:
        return NODE_FIELDS
    if '材料依据' in obj and '要求内容' in obj:
        return RECORD_REFERENCES['要求']
    if '要求依据' in obj and '原文表达' in obj:
        return RECORD_REFERENCES['结构依据']
    if '要求' in obj and '承接节点' in obj:
        return RECORD_REFERENCES['归属']
    if '要求' in obj and '目标或父节点' in obj:
        return RECORD_REFERENCES['调整建议']
    if '涉及节点' in obj and '涉及要求' in obj:
        return RECORD_REFERENCES['问题']
    if '对应章节' in obj and '相关依据' in obj:
        return RECORD_REFERENCES['建议']
    return {}


def map_input(value, mapping):
    if isinstance(value, list):
        return [map_input(v, mapping) for v in value]
    if not isinstance(value, dict):
        return value  # Free text never undergoes substitution or JSON parsing.
    if set(value) == {'__sections__'}:
        return {'__sections__': [[name, map_id_value(item, mapping) if name in SECTION_REFERENCE_FIELDS
                                 else map_input(item, mapping)] for name, item in value['__sections__']]}
    refs = INPUT_REFERENCE_FIELDS | set(input_record_fields(value))
    return {key: map_id_value(item, mapping) if key in refs else map_input(item, mapping)
            for key, item in value.items()}


def map_id_value(value, mapping):
    if isinstance(value, str):
        return map_reference(value, mapping)
    if isinstance(value, list):
        return [map_id_value(v, mapping) for v in value]
    return value


def render_view(view, aliases=None):
    def printable(value):
        if isinstance(value, dict) and set(value) == {'__sections__'}:
            return render_view(value)
        if isinstance(value, dict):
            return {k: printable(v) for k, v in value.items()}
        if isinstance(value, list):
            return [printable(v) for v in value]
        return value
    view = map_input(view, aliases or {})
    return '\n\n'.join(f'{name}：\n' + (value if isinstance(value := printable(item), str)
                         else json.dumps(value, ensure_ascii=False, sort_keys=True))
                         for name, item in view['__sections__'])


def render_readable_view(view, aliases=None):
    """Render typed objects as readable records, never reparse or rewrite prose.

    The frozen source view still controls reference visibility and audit storage.
    Only storage identities and empty optional metadata are hidden here.
    """
    labels = {
        'requirement_id': '要求编号', 'material_id': '材料编号',
        'element_id': '原文元素编号', 'node_id': '节点编号',
        'parent_id': '父节点', 'suggestion_id': '建议编号',
        'chapter_id': '所属章节', 'title': '标题', 'order': '同级顺序',
        'topic': '主题', 'content': '完整内容', 'text': '完整正文',
        'categories': '类别', 'material_refs': '材料依据',
        'basis_requirement_ids': '要求依据', 'origin_kind': '形成方式',
        'nodes': '目录节点', 'constraints': '结构约束', 'kind': '类型',
        'origin': '来源性质', 'basis': '依据说明', 'uncertainties': '疑点',
        'requirement_ids': '涉及要求', 'node_ids': '涉及节点',
        'affected_node_ids': '受保护节点', 'parent_node_id': '受限父节点',
        'allowed_child_titles': '允许的直属标题', 'shared_conditions': '共同条件',
        'targets': '承接分工', 'scope': '承接内容', 'unassigned_content': '未承接内容',
        'status': '状态', 'conditions': '适用条件', 'source_kind': '来源性质',
        'section_path': '来源章节', 'evidence': '问题依据', 'suggested_scope': '建议修正节点',
        'sequence_members': '清单成员', 'member_id': '清单成员编号',
        'parent_member_id': '清单父成员', 'level': '相对层级',
    }
    omitted = {
        'task_id', 'producing_task_id', 'producing_artifact_id', 'artifact_id',
        'result_ref', 'producer_ref', 'input_artifact_ids', 'created_at',
        'version_id', 'parent_version_id', 'outline_version_id', 'base_version_id',
        'commit_reason', 'constraint_id', 'finding_id',
    }
    # Version identity is retained in the complete audit view and fingerprints.
    omitted_sections = {'基准版本', '版本'}
    field_order = {key: index for index, key in enumerate(labels)}
    view = map_input(view, aliases or {})

    def lines(value, indent=0):
        prefix = '  ' * indent
        if isinstance(value, dict):
            if set(value) == {'__sections__'}:
                result = []
                for name, item in value['__sections__']:
                    if name not in omitted_sections:
                        result.append(prefix + str(name) + '：')
                        result.extend(lines(item, indent + 1))
                return result
            result = []
            # Artifact JSON is written with sorted keys. Canonical field order
            # keeps a recovered request identical to its first live rendering.
            for key in sorted(value, key=lambda key: (field_order.get(key, len(field_order)), key)):
                item = value[key]
                if key in omitted or (key in {'fields', 'supplements', 'uncertainties'} and item in ({}, [], '', '无', None)):
                    continue
                label = labels.get(key, key)
                if isinstance(item, (dict, list)):
                    result.append(prefix + label + '：')
                    result.extend(lines(item, indent + 1))
                else:
                    text = 'ROOT' if key == 'parent_id' and item is None else ('无' if item is None else str(item))
                    result.append(prefix + label + '：' + text)
            return result or [prefix + '无']
        if isinstance(value, list):
            if not value:
                return [prefix + '无']
            result = []
            for index, item in enumerate(value, 1):
                if isinstance(item, (dict, list)):
                    result.append(prefix + f'条目 {index}：')
                    result.extend(lines(item, indent + 1))
                else:
                    result.append(prefix + '• ' + str(item))
            return result
        # A free-text body is emitted intact, including its literal newlines.
        return [prefix + ('无' if value is None else str(value))]

    return '\n'.join(lines(view))


def visible_groups(view):
    """Full objects authorize semantic references; node bases are metadata only."""
    groups = {key: set() for key in ('requirements', 'nodes', 'materials', 'elements', 'suggestions', 'support_requirements')}
    support = groups['support_requirements']
    def visit(value):
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            if 'requirement_id' in value and 'content' in value and 'topic' in value:
                groups['requirements'].add(value['requirement_id'])
            if {'node_id', 'title', 'parent_id', 'order'} <= value.keys():
                groups['nodes'].add(value['node_id'])
                support.update(value.get('basis_requirement_ids', []))
            if 'material_id' in value and 'text' in value:
                groups['materials'].add(value['material_id'])
                if value.get('kind') == 'reader_source':
                    groups['elements'].add(value['material_id'])
            if 'element_id' in value and 'text' in value:
                groups['elements'].add(value['element_id'])
            if 'suggestion_id' in value and 'content' in value:
                groups['suggestions'].add(value['suggestion_id'])
            if 'constraint_id' in value:
                support.update(value.get('requirement_ids', []))
            if '要求依据' in value and '原文表达' in value:
                support.update(re.split(r'[、,，;；\s]+', value['要求依据']) if value['要求依据'] != '无' else [])
            for item in value.values():
                visit(item)
    visit(view)
    support.difference_update(groups['requirements'])
    return {key: sorted(values) for key, values in groups.items()}


def prepare_inputs(inputs):
    """Freeze views before fingerprinting, supplement wrapping or persistence."""
    inputs = dict(inputs)
    for key in ('material', 'context'):
        value = inputs.get(key)
        if isinstance(value, Presented):
            inputs[key + '_view'] = value.view
            inputs[key] = str(value)
    if 'material_view' in inputs:
        combined = [inputs['material_view'], inputs.get('context_view', {})]
        groups = visible_groups(combined)
        inputs['id_groups'] = groups
        inputs['allowed_ids'] = sorted({ident for values in groups.values() for ident in values})
        bases = {}
        def basis_objects(value):
            if isinstance(value, list):
                for item in value:
                    basis_objects(item)
            elif isinstance(value, dict):
                if {'node_id', 'title', 'parent_id', 'order'} <= value.keys():
                    bases[value['node_id']] = list(value.get('basis_requirement_ids', []))
                for item in value.values():
                    basis_objects(item)
        basis_objects(combined)
        inputs['existing_node_basis'] = bases
        inputs['scope_is_reference'] = inputs.get('scope') in inputs['allowed_ids']
    return inputs
