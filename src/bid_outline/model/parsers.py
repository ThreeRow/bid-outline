"""Strict task-specific constrained-text parser. Free text remains multiline."""
import re
from .task_specs import get_spec

class ParseError(ValueError):
    """The response cannot be adopted under its task text contract."""

def parse_ids(value: str) -> list[str]:
    """Parse a unique list of opaque IDs; the explicit value 无 means empty."""
    if value.strip() in ('无', ''):
        return []
    values = re.split(r'[、,，;；\s]+', value.strip())
    if any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/-]*', x) for x in values):
        raise ParseError(f'Invalid ID list: {value}')
    if len(values) != len(set(values)):
        raise ParseError('Duplicate ID in list')
    return values

# Field order is unconstrained; the required set is exact.
FIELDS = {
 'P01':'判断 相关主题 疑点 引用线索', 'P02':'判断 相关主题 疑点 引用线索',
 'P03':'处理结果 保留内容 疑点',
 'P04-A':'提取结果','P04-B':'提取结果','P04-C':'提取结果',
 'P05':'','P06':'结论 采用候选 关联说明 仍缺少的信息','P07':'',
 'P08':'状态 骨架 调整说明 待核查事项','P09':'','P10':'',
 'P11':'章节 处理结果 子目录 未承接事项 结构调整建议',
 'P12':'章节 整合目录 合并说明 未解决事项',
 'P13':'结论 涉及节点 涉及要求 依据 建议','P14':'',
 'P15':'检查结论','P16':'处理结论 替换目录 问题处理说明 仍需处理',
 'P17':'结果','P18':''}
RECORDS = {
 '要求':'局部编号 类别 材料依据 要求主题 要求内容 疑点',
 '判定':'要求 结论 涉及类别 依据',
 '结构依据':'局部编号 要求依据 性质 原文表达 结构含义 疑点',
 '节点':'节点 父节点 同级顺序 标题 依据 形成方式 说明',
 '归属':'要求 状态 承接节点 承接范围 未承接内容 依据或疑点',
 '调整建议':'要求 处理 目标或父节点 建议标题 位置说明 承接内容 依据与限制',
 '覆盖检查':'要求 结论 承接节点 承接说明 未覆盖或不明确内容 修正建议',
 '问题':'局部编号 类型 涉及节点 涉及要求 问题 依据 建议修正范围',
 '建议':'局部编号 对应章节 建议主题 建议内容 来源性质 相关依据 适用理由与条件',
 '建议检查':'建议 结论 依据 修改意见',
 '补读':'引用或疑点 需要的信息 用途 查找线索'}
TASK_RECORD = {'P04-A':'要求','P04-B':'要求','P04-C':'要求','P05':'判定','P07':'结构依据','P08':'节点','P09':'归属','P10':'调整建议','P11':'节点','P12':'节点','P14':'覆盖检查','P15':'问题','P16':'节点','P17':'建议','P18':'建议检查'}
ENUMS = {
 'P01':('判断','有 待确认 未发现'), 'P02':('判断','有 待确认 未发现'),
 'P03':('处理结果','已摘取 未发现相关内容 无法完整处理'),
 'P06':('结论','已找到 部分找到 无法确认 候选均不适用'),
 'P08':('状态','已形成候选骨架 信息不足'),
 'P11':('处理结果','已生成 无须新增子目录 信息不足'),
 'P13':('结论','合理交叉 确有重复 归属问题 存在冲突 无法确认'),
 'P15':('检查结论','未发现问题 发现问题 信息不足'),
 'P16':('处理结论','已修正 部分修正 无法在范围内修正 检查意见不成立'),
 'P17':('结果','有建议 无建议')}
RECORD_ENUMS = {
 '判定':('结论','A B C 多类 项目需求 保持待定 不纳入'),
 '结构依据':('性质','禁止新增 章节或顺序约束 明确组成关系 非固定清单 仅内容要求 评价描述 一般需求 待确认'),
 '节点':('形成方式','原文明示 有依据归纳'),
 '归属':('状态','已分配 部分分配 未承接 待确认'),
 '调整建议':('处理','归入已有节点 新增子节点 新增外层章节 保持待定'),
 '覆盖检查':('结论','已覆盖 部分覆盖 未覆盖 信息不足'),
 '问题':('类型','结构约束违反 父子关系不当 无依据内容 条件改变 其他'),
 '建议':('来源性质','项目需求推理 骨干深化 常规经验'),
 '建议检查':('结论','保留 修改后保留 删除 待确认')}
SUPPLEMENT_TASKS = set(FIELDS) - {'P06','P12','P13','P17','P18'}
DIRECTORY_FIELDS = {'P08':'骨架','P11':'子目录','P12':'整合目录','P16':'替换目录'}
CONTROL_KINDS = set(RECORDS) | {'结果', '格式修复失败'}
CONTROL_MARKER = re.compile(r'\[\[(?:' + '|'.join(map(re.escape, sorted(CONTROL_KINDS))) + r')(?:开始|结束)')

def _fields(
    lines: list[str], required: str, empty_allowed: tuple[str, ...] = ()
) -> dict[str, str]:
    result: dict[str, str] = {}
    current = None
    names = required.split()
    pattern = None
    if names:
        alternatives = '|'.join(map(re.escape, names))
        pattern = re.compile(r'^\s*(' + alternatives + r')\s*[:：](.*)$')
    for line in lines:
        match = pattern.match(line) if pattern else None
        if match:
            current = match[1]
            if current in result:
                raise ParseError(f'Duplicate field: {current}')
            result[current] = match[2].strip()
        elif line.strip():
            if current is None:
                raise ParseError(f'Text outside fields: {line.strip()[:160]!r}')
            result[current] += '\n' + line
        elif current is not None:
            result[current] += '\n'
    result = {key: value.strip() for key, value in result.items()}
    if set(result) != set(names):
        raise ParseError(f'Missing fields: {set(names) - set(result)}')
    if any(not value and key not in empty_allowed for key, value in result.items()):
        raise ParseError('Empty required field')
    return result


def _enum(data: dict[str, str], definition: tuple[str, str]) -> None:
    field, values = definition
    if data[field] not in values.split():
        raise ParseError(f'Invalid {field}: {data[field]}')


def parse_output(
    task_type: str,
    text: str,
    *,
    allowed_ids: list[str] | None = None,
    expected_ids: list[str] | None = None,
    id_groups: dict[str, list[str]] | None = None,
    existing_node_basis: dict[str, list[str]] | None = None,
) -> dict:
    """Validate a complete response without guessing missing fields or business results.

    Existing references must belong to allowed_ids. Tasks answering input records
    must cover expected_ids exactly when that list is supplied. Multiline field
    content is retained; directory structure is validated later by the workflow.
    """
    get_spec(task_type)
    if not isinstance(text, str):
        raise ParseError('Response must be text')
    if task_type == 'P19':
        if text.strip().startswith('[[格式修复失败]]'):
            lines = text.strip().splitlines()
            if lines[-1].strip() != '[[格式修复失败结束]]' or any(
                '[[' in line or ']]' in line for line in lines[1:-1]
            ):
                raise ParseError('Unclosed repair failure')
            return {
                'fields': _fields(lines[1:-1], '原因'),
                'records': {},
                'supplements': [],
            }
        # The caller must also parse successful repair under the original task.
        for target in FIELDS:
            try:
                return parse_output(
                    target, text, allowed_ids=allowed_ids, expected_ids=expected_ids, id_groups=id_groups, existing_node_basis=existing_node_basis
                )
            except ParseError:
                continue
        raise ParseError('Repaired output does not match a task contract')

    normalized = text.strip()
    # Models often place the optional supplement block after the result closing
    # marker because the template shows its format after that marker. Accept only
    # well-formed supplement blocks in that tail, then validate them normally.
    close = normalized.find('[[结果结束]]')
    if close >= 0 and normalized[close + len('[[结果结束]]'):].strip():
        tail = normalized[close + len('[[结果结束]]'):].strip()
        if task_type in SUPPLEMENT_TASKS and tail.startswith('[[补读开始]]'):
            if tail.count('[[补读开始]]') != tail.count('[[补读结束]]'):
                raise ParseError('Unclosed supplement after result')
            normalized = normalized[:close] + '\n' + tail + '\n[[结果结束]]'
    lines = normalized.splitlines()
    if (
        len(lines) < 2
        or lines[0].strip() != '[[结果开始]]'
        or lines[-1].strip() != '[[结果结束]]'
    ):
        raise ParseError('Missing result boundaries')
    top: list[str] = []
    records: dict[str, list[dict[str, str]]] = {}
    supplements: list[dict[str, str]] = []
    active = None
    buffer: list[str] = []
    name = TASK_RECORD.get(task_type)
    for line in lines[1:-1]:
        mark = re.fullmatch(r'\s*\[\[(.+?)(开始|结束)\]\]\s*', line)
        if mark and mark[1] not in CONTROL_KINDS:
            mark = None  # e.g. literal [[输入数据结束]] in extracted source prose
        if mark:
            kind, edge = mark.groups()
            if edge == '开始':
                permitted = kind == name or (
                    kind == '补读' and task_type in SUPPLEMENT_TASKS
                )
                if active or not permitted:
                    raise ParseError('Unexpected record boundary')
                active = kind
                buffer = []
            else:
                if active != kind:
                    raise ParseError('Mismatched record boundary')
                required_fields = RECORDS[kind]
                if kind == '结构依据' and any(re.match(r'\s*(限制父级|允许子项)\s*[:：]', line) for line in buffer):
                    required_fields += ' 限制父级 允许子项'
                list_fields = ('清单材料', '清单起点', '清单终点', '清单成员')
                if kind == '结构依据' and any(re.match(r'\s*(清单材料|清单起点|清单终点|清单成员)\s*[:：]', line) for line in buffer):
                    required_fields += ' ' + ' '.join(list_fields)
                obj = _fields(buffer, required_fields)
                if kind == '结构依据' and '清单材料' in obj:
                    filled = [obj[k] != '无' for k in list_fields]
                    if any(filled) and not all(filled):
                        raise ParseError('List evidence requires all bounded fields')
                    if all(filled):
                        if len(parse_ids(obj['清单材料'])) != 1:
                            raise ParseError('List evidence requires one material')
                        if any(not re.fullmatch(r'[1-4]\|.+', line) for line in obj['清单成员'].splitlines()):
                            raise ParseError('List member requires level|title')
                if kind == '结构依据':
                    if obj['性质'] == '禁止新增':
                        if not {'限制父级', '允许子项'} <= obj.keys() or obj['限制父级'] == '无':
                            raise ParseError('Closed list requires explicit parent and allowed children')
                        titles = obj['允许子项'].splitlines()
                        if len(titles) != len(set(titles)):
                            raise ParseError('Duplicate closed-list title')
                    elif any(obj.get(key, '无') != '无' for key in ('限制父级', '允许子项')):
                        raise ParseError('Restriction fields require 禁止新增 nature')
                if kind == '补读':
                    supplements.append(obj)
                else:
                    records.setdefault(kind, []).append(obj)
                active = None
        elif CONTROL_MARKER.search(line):
            raise ParseError('Reserved marker inside content')
        elif active:
            buffer.append(line)
        else:
            top.append(line)
    if active:
        raise ParseError('Unclosed record')

    directory_field = DIRECTORY_FIELDS.get(task_type)
    empty_allowed = (directory_field,) if records.get('节点') else ()
    fields = _fields(top, FIELDS[task_type], empty_allowed)
    if task_type in ENUMS:
        _enum(fields, ENUMS[task_type])
    items = records.get(name, [])
    allowed = set(allowed_ids or [])
    local_nodes = {
        item['节点']
        for item in records.get('节点', [])
        if re.fullmatch(r'N\d{2,}', item['节点'])
    }

    def check(
        value: str, *, local: bool = False, root: bool = False,
        experience: bool = False,
    ) -> None:
        if experience and value == '常规经验':
            return
        for ident in parse_ids(value):
            if ident == 'ROOT' and root:
                continue
            if local and ident in local_nodes:
                continue
            if ident not in allowed:
                raise ParseError(f'ID not provided: {ident}')

    for key in ('章节', '涉及节点', '涉及要求', '采用候选'):
        if key in fields:
            check(fields[key])
            if key == '章节' and len(parse_ids(fields[key])) != 1:
                raise ParseError('Expected one chapter ID')
    top_refs, record_refs = get_spec(task_type).reference_contract

    def check_role(field, value, contract):
        values = parse_ids(value) if value not in ('常规经验', 'ROOT') else [value]
        if contract.scalar and len(values) > 1:
            raise ParseError(f'Expected scalar reference: {field}')
        if id_groups is None:
            return  # Legacy parser API: callers without typed objects get membership validation.
        visible = set().union(*(set(id_groups.get(group, [])) for group in contract.groups))
        if contract.local:
            visible |= local_nodes
        if contract.root:
            visible.add('ROOT')
        if contract.experience:
            visible.add('常规经验')
        if set(values) - visible:
            role = 'requirement' if 'requirements' in contract.groups else '/'.join(contract.groups)
            raise ParseError(f'{field} must reference {role} IDs visible in this task: {sorted(set(values) - visible)}')

    for field, contract in top_refs.items():
        check_role(field, fields[field], contract)
    seen: set[str] = set()
    for item in items:
        for field, contract in record_refs.items():
            if field in item:
                check_role(field, item[field], contract)
        if name in RECORD_ENUMS:
            _enum(item, RECORD_ENUMS[name])
        if '局部编号' in item:
            prefix = {'要求': 'R', '结构依据': 'S', '问题': 'P', '建议': 'E'}[name]
            ident = item['局部编号']
            if not re.fullmatch(prefix + r'\d{2,}', ident) or ident in seen:
                raise ParseError('Invalid/duplicate local ID')
            seen.add(ident)
        for key in (
            '要求', '材料依据', '清单材料', '要求依据', '承接节点',
            '涉及节点', '涉及要求', '对应章节', '建议',
        ):
            if key in item:
                check(item[key])
        if name == '建议':
            check(item['相关依据'], experience=True)
        if name == '调整建议':
            check(item['目标或父节点'], root=True)
        if name == '节点':
            if id_groups is not None:
                metadata_only = set(parse_ids(item['依据'])) & set(id_groups.get('support_requirements', []))
                original_basis = set((existing_node_basis or {}).get(item['节点'], []))
                if metadata_only - original_basis:
                    raise ParseError('Reference-only requirement metadata cannot establish new node basis')
            if len(parse_ids(item['节点'])) != 1 or len(parse_ids(item['父节点'])) != 1:
                raise ParseError('Expected scalar node and parent IDs')
            check(item['节点'], local=True)
            check(item['父节点'], local=True, root=True)
            check(item['依据'])
            if item['节点'] in seen:
                raise ParseError('Duplicate node')
            seen.add(item['节点'])
            if not re.fullmatch(r'[1-9]\d*', item['同级顺序']):
                raise ParseError('Invalid sibling order')
        if name in ('归属', '判定', '覆盖检查', '建议检查'):
            response_key = '建议' if name == '建议检查' else '要求'
            if len(parse_ids(item[response_key])) != 1:
                raise ParseError('Expected one response ID')
        if name == '要求':
            if not parse_ids(item['材料依据']):
                raise ParseError('Requirement needs material provenance')
            if item['类别'] not in (task_type[-1], '待定'):
                raise ParseError('Wrong extraction category')
        if name == '判定' and item['涉及类别'] != '无':
            categories = re.split('[、,， ]+', item['涉及类别'])
            if any(category not in ('A', 'B', 'C') for category in categories):
                raise ParseError('Invalid categories')
        if name == '归属' and item['状态'] == '已分配':
            if not parse_ids(item['承接节点']) or item['未承接内容'] != '无':
                raise ParseError('Assigned requirement must have targets and no remainder')
        if name == '覆盖检查' and item['结论'] == '已覆盖':
            if not parse_ids(item['承接节点']) or item['未覆盖或不明确内容'] != '无':
                raise ParseError('Covered requirement must have targets and no remainder')

    if task_type == 'P06' and fields['结论'] in ('已找到', '部分找到'):
        if not parse_ids(fields['采用候选']):
            raise ParseError('Found reference must select at least one candidate')
    if task_type.startswith('P04-'):
        _enum(fields, ('提取结果', '已提取 未发现本类要求 仅有待定内容'))
        if (fields['提取结果'] == '未发现本类要求') != (not items):
            raise ParseError('Extraction status/records mismatch')
        if fields['提取结果'] == '仅有待定内容' and any(
            item['类别'] != '待定' for item in items
        ):
            raise ParseError('Nonpending extraction')
    for field, positive, negative in (
        ('检查结论', '发现问题', '未发现问题'),
        ('结果', '有建议', '无建议'),
    ):
        if field in fields and (
            (fields[field] == positive and not items)
            or (fields[field] == negative and items)
        ):
            raise ParseError('Status/record mismatch')
    if directory_field:
        value = fields[directory_field]
        if items and value in ('无', '清空'):
            raise ParseError('Directory says none but includes nodes')
        if not items and value != '无' and not (task_type == 'P16' and value == '清空'):
            raise ParseError('Directory requires node records or explicit 无')
        if task_type == 'P16' and value == '清空' and fields['处理结论'] not in ('已修正', '部分修正'):
            raise ParseError('Clear action requires a successful repair status')
        generated = (
            (task_type == 'P08' and fields['状态'] == '已形成候选骨架')
            or (task_type == 'P11' and fields['处理结果'] == '已生成')
            or (task_type == 'P16' and fields['处理结论'] in ('已修正', '部分修正'))
        )
        if generated and not items and not (task_type == 'P16' and value in ('无', '清空')):
            raise ParseError('Generated directory has no nodes')
        negative = (
            (task_type == 'P08' and fields['状态'] == '信息不足')
            or (task_type == 'P11' and fields['处理结果'] != '已生成')
        )
        if negative and items:
            raise ParseError('Negative generation status must not include nodes')
    if task_type in ('P05', 'P09', 'P14', 'P18', 'P10'):
        key = '建议' if task_type == 'P18' else '要求'
        response_ids = [ident for item in items for ident in parse_ids(item[key])]
        # One complete requirement can need several non-overlapping placements.
        # P10 emits adjustment suggestions, not one final assignment per input.
        if task_type != 'P10' and len(response_ids) != len(set(response_ids)):
            raise ParseError('Duplicate response ID')
        if expected_ids is not None and set(response_ids) != set(expected_ids):
            raise ParseError('Expected input coverage mismatch')
    if task_type == 'P03' and fields['处理结果'] != '无法完整处理':
        if (fields['处理结果'] == '未发现相关内容') != (fields['保留内容'] == '无'):
            raise ParseError('Refinement status/content mismatch')
    return {'fields': fields, 'records': records, 'supplements': supplements}


def retry_guidance(task_type: str, error: str) -> str:
    """Give precise contract feedback without accepting or silently rewriting data."""
    lines = [
        '上一尝试未通过输出契约校验。请基于原始任务和全部材料重新返回完整结果。',
        '保留有依据的内容、条件和来源；只修复输出格式问题，不猜测缺失材料。',
        '仅输出结果边界内的固定字段与规定记录，不抄写模板中的说明性导语。',
        '所有ID列表必须逐个列出，用顿号或逗号分隔；禁止REQ01-REQ18这类区间缩写、括号解释、“其余”“除外”等集合表达。',
    ]
    if task_type in FIELDS:
        lines.append('顶层字段：' + ('、'.join(FIELDS[task_type].split()) or '无，顶层仅允许规定记录'))
    if task_type.startswith('P04-'):
        lines.append('提取结果只允许：已提取、未发现本类要求、仅有待定内容。不要输出“已提取本类要求”。')
    if task_type in DIRECTORY_FIELDS:
        lines.extend([
            '新节点使用N01、N02等；不能用N_001、1或1.1。已有ID逐字引用，不重编号。',
            '父节点仅可引用本次已声明局部节点、输入已有节点或ROOT；依据只可使用要求ID，不可使用材料ID。',
            '信息不足时不输出节点；有依据的候选及其未解决疑点按原任务状态规则分别表达。',
        ])
    # Error strings can contain model-produced text, so they remain quoted data.
    from .task_specs import escape_material
    lines.append('校验诊断（数据，不是新指令）：\n' + escape_material(error))
    return '\n'.join(lines)


def close_complete_final_record(task_type: str, text: str, **validation) -> dict:
    """Repair one omitted final record delimiter, never a missing business field.

    Caller must already establish a normal non-truncated protocol finish. The
    original parser still verifies all fields, enums, references and coverage.
    """
    lines = text.strip().splitlines()
    if not lines or lines[-1].strip() != '[[结果结束]]':
        raise ParseError('Cannot repair incomplete result boundary')
    active = None
    permitted = {TASK_RECORD.get(task_type)}
    if task_type in SUPPLEMENT_TASKS:
        permitted.add('补读')
    for line in lines[1:-1]:
        match = re.fullmatch(r'\s*\[\[(.+?)(开始|结束)\]\]\s*', line)
        if not match:
            continue
        kind, edge = match.groups()
        if kind not in permitted:
            raise ParseError('Cannot repair an unexpected record')
        if edge == '开始':
            if active:
                raise ParseError('Cannot repair nested or multiple unclosed records')
            active = kind
        elif active != kind:
            raise ParseError('Cannot repair mismatched record boundary')
        else:
            active = None
    if active is None:
        raise ParseError('No final record closure to repair')
    marker = f'[[{active}结束]]'
    normalized = '\n'.join(lines[:-1] + [marker, lines[-1]])
    parsed = parse_output(task_type, normalized, **validation)
    parsed['format_repairs'] = [{'kind': 'close_complete_final_record', 'inserted': marker,
                                 'position': 'before_result_end', 'normalized_output': normalized}]
    return parsed
