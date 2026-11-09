"""Small directory wire format; identities and edges are program-owned.

The model supplies headings and requirement references only. We never invent
requirement support from title similarity or attach all visible requirements.
Existing IDs are recovered only by an exact title AND parent match.
"""
import re
from collections import Counter

VERSION = 'markdown-directory-1.0.0'
TASKS = frozenset({'P08', 'P11', 'P12', 'P16'})


def template(task, original):
    # Preserve business instructions but remove the old machine output protocol.
    cut = min(i for i in (original.find('输出规则'), original.find('输出格式')) if i >= 0)
    body = original[:cut]
    lines = []
    for line in body.splitlines():
        if line.startswith('新建节点只能') or line.startswith('“无”表示'):
            continue
        line = line.replace('保留已有节点ID，新节点统一使用本次N01、N02等，并说明合并依据',
                            '保留已有标题及层级；相同标题在相同父标题下由程序识别')
        line = line.replace('已有目录节点仍用输入ID。', '已有目录节点保留标题及层级。')
        lines.append(line)
    scope = ('输出完整骨架，从一级标题 # 开始。' if task == 'P08' else
             '只输出边界章节的全部下级；不要重复边界章节本身。直属下级从 # 开始，# 表示本次相对层级。')
    reading = ('如确需查找原文，可另写一行“补读：缺少的条款、来源章节及需要确认的内容”。\n'
               if task in {'P08', 'P11', 'P16'} else '')
    return '\n'.join(lines) + '\n\n' + scope + '''
输出形式：Markdown标题表示目录层级，#、##、###依次为父子层级；同级按出现顺序排列，不跳级。
每个新标题下一行写“依据：REQ01、REQ02”，只引用本次提供的要求编号。已有标题原位置保留时可省略依据，由程序保留原依据。
不写节点ID、父节点ID、顺序字段、JSON、记录标记、代码围栏或正文段落。标题、来源编号以外的程序字段由程序生成。
全目录最多四级；本次相对层级加边界章节层级不能超过四级。保留原文限制、已有受保护标题及其顺序。
例如（编号仅示意，实际使用本次要求编号）：
# 实施方案
依据：REQ01
## 数据迁移
依据：REQ02
## 培训安排
依据：REQ03
有未承接内容或缺口，在目录末尾另写“待确认：具体事项”，可跨行；不得把缺口伪装成已解决。
无法形成候选时只写“信息不足：具体缺口”。本章确实无需子目录时只写“无需子目录”。
局部修正若不需改动，只写“保持原目录”；明确需要删除全部下级时写“清空下级：删除理由”，由程序检查是否允许。
''' + reading


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def normalize(task, text, inputs):
    """Convert a deterministic heading AST into the existing validated protocol."""
    from .parsers import ParseError, parse_ids
    if task not in TASKS:
        raise ParseError(f'Markdown directory unsupported for {task}')
    view = inputs.get('material_view')
    if not isinstance(view, dict):
        raise ParseError('Markdown directory needs typed source inputs')
    aliases = inputs.get('id_aliases', {})
    inverse = {value: key for key, value in aliases.items()}
    existing = {}
    for item in _walk(view):
        if {'node_id', 'title', 'parent_id', 'order'} <= item.keys():
            key = item['node_id']
            if key in existing and existing[key] != item:
                # Some views include repeated nodes with different audit-only
                # fields. Only their structural identity must agree.
                if any(existing[key].get(k) != item.get(k)
                       for k in ('title', 'parent_id', 'basis_requirement_ids')):
                    raise ParseError('Ambiguous existing node identity')
            existing[key] = item
    boundary = None
    if task != 'P08':
        boundary = inverse.get(inputs.get('scope'), inputs.get('scope'))
        if boundary not in existing:
            raise ParseError('Missing typed boundary chapter')
    # The full ancestor chain is not always supplied. The workflow explicitly
    # supplies the absolute depth in chapter and repair requests.
    boundary_depth = 0
    if boundary:
        for item in _walk(view):
            for name, val in item.get('__sections__', []):
                if name in ('绝对层级', '边界父节点绝对层级'):
                    boundary_depth = int(val)
        if not 1 <= boundary_depth <= 4:
            raise ParseError('Missing absolute boundary depth')
    lines = text.strip().splitlines()
    entries, notes, supplements = [], [], []
    mode = 'tree'
    control = None
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        heading = re.fullmatch(r'(#{1,4})[ \t]+(.+)', line)
        if heading:
            if mode != 'tree' or control:
                raise ParseError('Directory headings must precede notes')
            title = heading[2].strip()
            if title.endswith(' #') or title.startswith('#'):
                raise ParseError('Use plain heading titles without closing hashes')
            level = len(heading[1])
            if (not entries and level != 1) or (entries and level > entries[-1]['level'] + 1):
                raise ParseError('Markdown hierarchy cannot skip levels')
            if level + boundary_depth > 4:
                raise ParseError('Directory exceeds four absolute levels')
            entries.append({'level': level, 'title': title, 'basis': None})
        elif line.startswith(('依据：', '依据:')):
            if mode != 'tree' or not entries or entries[-1]['basis'] is not None:
                raise ParseError('Each heading has at most one basis line')
            basis = parse_ids(re.split('[:：]', line, maxsplit=1)[1])
            if not basis:
                raise ParseError('A supplied basis line must cite requirements')
            entries[-1]['basis'] = basis
        elif line.startswith(('待确认：', '待确认:')):
            mode = 'notes'
            notes.append(re.split('[:：]', line, maxsplit=1)[1].strip())
        elif line.startswith(('补读：', '补读:')):
            detail = re.split('[:：]', line, maxsplit=1)[1].strip()
            if task not in {'P08', 'P11', 'P16'} or not detail:
                raise ParseError('This task cannot use an empty or unsupported reading request')
            mode = 'notes'
            supplements.append(detail)
        elif not entries and not control and mode == 'tree' and (
                line in {'无需子目录', '保持原目录'} or
                line.startswith(('信息不足：', '信息不足:', '清空下级：', '清空下级:'))):
            control, mode = line, 'notes'
        elif mode == 'notes' and not line.startswith(('```', '[[结果', '[[节点')):
            notes.append(raw)
        else:
            raise ParseError('Only Markdown headings, basis lines and optional pending notes are allowed')
    if not entries and not control:
        raise ParseError('Empty Markdown directory')
    if control in {'无需子目录', '保持原目录'} and task not in {'P11', 'P16'}:
        raise ParseError('This task needs a complete directory or explicit information gap')
    clear = bool(control and control.startswith(('清空下级：', '清空下级:')))
    if clear and task != 'P16':
        raise ParseError('Only local repair may explicitly clear descendants')
    if clear and not re.split('[:：]', control, maxsplit=1)[1].strip():
        raise ParseError('Clear action requires a reason')
    if control and control.startswith(('信息不足：', '信息不足:')) and not re.split('[:：]', control, maxsplit=1)[1].strip():
        raise ParseError('Information gap must be described')

    records, stack, counts, used = [], [], Counter(), set()
    pending = '\n'.join(notes + ['需补读：' + s for s in supplements]).strip() or '无'
    for entry in entries:
        level = entry['level']
        stack = stack[:level-1]
        parent = stack[-1] if stack else boundary
        matches = [key for key, node in existing.items()
                   if key != boundary and node.get('parent_id') == parent and node['title'] == entry['title']]
        if len(matches) > 1:
            raise ParseError('Duplicate existing title under one parent; cannot recover identity')
        if matches:
            key = matches[0]
            node = existing[key]
            basis = entry['basis'] or [aliases.get(k, k) for k in node.get('basis_requirement_ids', [])]
            origin = node.get('origin_kind', '有依据归纳')
        else:
            key = f'N{len(records)+1:02d}'
            while key in existing or key in aliases.values() or key in used:
                key = 'N' + str(int(key[1:])+1).zfill(2)
            basis = entry['basis']
            origin = '有依据归纳'
        if not basis:
            raise ParseError('New headings require a basis line; support is never inferred')
        if key in used or any(r['标题'] == entry['title'] and r['父节点'] == aliases.get(parent, parent or 'ROOT')
                              for r in records):
            raise ParseError('Duplicate sibling heading')
        used.add(key)
        counts[parent] += 1
        records.append({'节点': aliases.get(key, key), '父节点': aliases.get(parent, parent or 'ROOT'),
                        '同级顺序': str(counts[parent]), '标题': entry['title'],
                        '依据': '、'.join(basis), '形成方式': origin, '说明': 'Markdown标题候选；覆盖另行检查'})
        stack.append(key)

    gap = control and control.startswith(('信息不足：', '信息不足:'))
    note = (control + '\n' + pending).strip() if control else pending
    if task == 'P08':
        fields = {'状态': '信息不足' if gap else '已形成候选骨架', '骨架': '见节点' if records else '无',
                  '调整说明': 'Markdown候选；身份及父子关系由程序解析', '待核查事项': note}
    elif task == 'P11':
        fields = {'章节': aliases.get(boundary, boundary),
                  '处理结果': '信息不足' if gap else ('已生成' if records else '无须新增子目录'),
                  '子目录': '见节点' if records else '无',
                  '未承接事项': note if gap or notes or supplements else '无', '结构调整建议': '无'}
    elif task == 'P12':
        fields = {'章节': aliases.get(boundary, boundary), '整合目录': '见节点' if records else '无',
                  '合并说明': 'Markdown整合候选，覆盖另行检查', '未解决事项': note}
    else:
        fields = {'处理结论': '部分修正' if records or clear else '无法在范围内修正',
                  '替换目录': '清空' if clear else ('见节点' if records else '无'),
                  '问题处理说明': 'Markdown局部候选；检查意见是否解决由复查判定',
                  '仍需处理': note if note != '无' else '待复查'}
    result = ['[[结果开始]]', *[f'{k}：{v}' for k, v in fields.items()]]
    for record in records:
        result += ['[[节点开始]]', *[f'{k}：{v}' for k, v in record.items()], '[[节点结束]]']
    for detail in supplements:
        result += ['[[补读开始]]', '引用或疑点：' + detail, '需要的信息：' + detail,
                   '用途：完成当前给定范围的目录任务', '查找线索：' + detail, '[[补读结束]]']
    result.append('[[结果结束]]')
    return '\n'.join(result)


def retry_guidance(error):
    return ('请按原任务和全部输入返回完整Markdown目录：#标题表示直属下级，##表示其子项，'
            '标题与#之间留一个空格；新标题下一行写依据：REQ编号。'
            '不输出节点记录、JSON或代码围栏。末尾可写待确认：具体缺口。'
            '\n校验诊断（数据）：' + repr(str(error)))
