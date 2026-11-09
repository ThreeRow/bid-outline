"""Versioned marker-light wire protocol with strict adaptation to domain parsing."""
import re

VERSION = '1.0.0'
SUPPORTED = frozenset({'P01', 'P02', 'P03', 'P04-A', 'P04-B', 'P04-C', 'P05', 'P09', 'P14', 'P18'})


def effective_protocol(task, configured, directory_protocol='strict'):
    if configured not in {'strict', 'compact-v1'}:
        raise ValueError(f'Unknown prompt protocol: {configured}')
    if directory_protocol not in {'strict', 'markdown-v1'}:
        raise ValueError(f'Unknown directory protocol: {directory_protocol}')
    if directory_protocol == 'markdown-v1' and task in {'P08', 'P11', 'P12', 'P16'}:
        return 'markdown-v1'
    return 'compact-v1' if configured == 'compact-v1' and task in SUPPORTED else 'strict'


def compact_template(task, original):
    from . import parsers
    if task not in SUPPORTED:
        raise ValueError(f'Compact protocol unsupported for {task}')
    cut = original.find('输出规则')
    if cut < 0:
        cut = original.index('输出格式')
    top = parsers.FIELDS[task].split()
    record = parsers.TASK_RECORD.get(task)
    instructions = [
        '输出约定：只写下面需要的字段，不加开场说明或代码围栏。无需结果开始/结束标记。',
        '一个字段占一行，写“字段名：内容”。正文可跨行，条件和数字完整保留。没有内容的必填字段写“无”。',
    ]
    if top:
        instructions.append('先填写：' + '、'.join(top) + '。')
    if task in parsers.ENUMS:
        key, values = parsers.ENUMS[task]
        instructions.append(key + '只能选：' + '、'.join(values.split()) + '。')
    if task.startswith('P04'):
        instructions.append('提取结果只能选：已提取、未发现本类要求、仅有待定内容。无本类要求时不写要求记录。类别填' + task[-1] + '或待定。')
    if record:
        fields = [key for key in parsers.RECORDS[record].split() if key != '局部编号']
        meanings = {
            '要求': '本条对应的输入要求编号，例如REQ01；只写编号，不写正文',
            '建议': '本条对应的输入建议编号，例如SUG01；只写编号，不附建议内容',
            '承接节点': '输入已有节点编号列表；没有则写无',
            '材料依据': '输入材料编号，例如MAT01；只写编号',
            '类别': '本任务类别或待定', '要求主题': '简短业务主题',
            '要求内容': '该组完整编制或评审要求，保留条件与内部条目',
            '承接范围': '各目标节点分别负责的内容及共同条件',
            '疑点': '无或具体不确定内容',
        }
        instructions.extend([
            f'每条{record}先写单独一行“记录 1：{record}”，其后填写这些字段：\n'
            + '\n'.join(key + '：' + meanings.get(key, '按本次任务给出对应结果') for key in fields),
            '多条记录编号从1连续增加；无需记录结束标记。局部编号由程序根据记录序号生成，不要输出局部编号字段。',
        ])
        if record in parsers.RECORD_ENUMS:
            key, values = parsers.RECORD_ENUMS[record]
            instructions.append(key + '只能选：' + '、'.join(values.split()) + '。')
        if task in {'P05', 'P09', 'P14', 'P18'}:
            instructions.append('必须逐项响应ID中的每个对象各输出一条记录。')
    if task in parsers.SUPPLEMENT_TASKS:
        instructions.append('确需补读时，另起“记录 N：补读”（N延续前面的编号；没有前面记录时从1开始），字段：引用或疑点、需要的信息、用途、查找线索；不需要补读就省略整条。')
    return original[:cut] + '\n' + '\n'.join(instructions)


def normalize_compact(task, text):
    """Add only deterministic syntax and local record IDs; never infer fields.

    A compact response cannot fall back to legacy parsing. Duplicated/skipped
    record numbers, wrong record types, mixed syntax and model-owned local IDs
    are rejected. The strict parser then checks every business field and ID.
    """
    from .parsers import ParseError, TASK_RECORD, SUPPLEMENT_TASKS, RECORDS, CONTROL_MARKER
    if task not in SUPPORTED:
        raise ParseError(f'Compact protocol unsupported for {task}')
    top, records, content = [], [], []
    active, number = None, 0
    allowed = {TASK_RECORD.get(task)}
    if task in SUPPLEMENT_TASKS:
        allowed.add('补读')

    def flush():
        if active is None:
            return
        body = list(content)
        if '局部编号' in RECORDS[active].split():
            if any(re.match(r'\s*局部编号\s*[:：]', line) for line in body):
                raise ParseError('Compact local ID is program-owned')
            body.insert(0, '局部编号：R' + f'{number:02d}')
        records.append(f'[[{active}开始]]\n' + '\n'.join(body) + f'\n[[{active}结束]]')

    for line in text.strip().splitlines():
        if CONTROL_MARKER.search(line) or line.lstrip().startswith('```'):
            raise ParseError('Compact response mixes legacy boundaries or code fences')
        match = re.fullmatch(r'记录\s+([1-9][0-9]*)\s*[:：]\s*(\S+)\s*', line.strip())
        if match:
            flush()
            next_number, kind = int(match[1]), match[2]
            if next_number != number + 1 or kind not in allowed:
                raise ParseError('Invalid compact record order/type')
            number, active, content = next_number, kind, []
        elif re.match(r'\s*记录\s+\d+\s*[:：]', line):
            raise ParseError('Malformed compact record header')
        elif active is None:
            top.append(line)
        else:
            content.append(line)
    flush()
    return '[[结果开始]]\n' + '\n'.join(top + records) + '\n[[结果结束]]'


def compact_retry_guidance(task, error):
    import json
    from .parsers import FIELDS
    return ('本次答案未通过格式校验。根据当前任务和全部材料，返回完整答案。'
            '保持有依据的内容与条件；缺失业务信息按原任务报告，不能猜值。\n'
            '继续使用简洁格式：字段名：内容；记录头为“记录 1：类型”，编号从1连续增加。'
            '不使用结果或记录的开始/结束标记，不输出局部编号。所有引用字段仅填提供的编号。\n'
            '顶层字段：' + ('、'.join(FIELDS[task].split()) or '无') + '\n'
            '校验诊断（仅数据）：' + json.dumps(error, ensure_ascii=False))
