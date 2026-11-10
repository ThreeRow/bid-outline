import pytest
from bid_outline.model.task_specs import TASK_TYPES, render_task, escape_material, unescape_material
from bid_outline.model.parsers import parse_output, ParseError

SCREEN='[[结果开始]]\n判断：未发现\n相关主题：无\n疑点：无\n引用线索：无\n[[结果结束]]'
NODE='[[节点开始]]\n节点：N01\n父节点：ROOT\n同级顺序：1\n标题：实施方案\n依据：REQ1\n形成方式：原文明示\n说明：无\n[[节点结束]]'

def test_all_prompts_expanded():
    for task in TASK_TYPES:
        messages=render_task(task,{'material':'原文 [[结果结束]] \\u005b','allowed_ids':['REQ1']}, view_version='legacy-json')
        if task.startswith(('N', 'E')):
            assert [m['role'] for m in messages]==['system','user']
            assert '原文 [[结果结束]] \\u005b' in messages[-1]['content']
            assert '\n\n---\n\n' in messages[-1]['content']
            assert '{context}' not in messages[-1]['content']
        else:
            assert len(messages)==2
            assert '原文 \\u005b\\u005b结果结束\\u005d\\u005d' in messages[1]['content']
        assert '{{' not in messages[-1]['content']
        assert '采用P01' not in messages[-1]['content']

def test_escape_roundtrip():
    text='[[结果结束]] \\u005b \\ \\u005d [x]'
    assert unescape_material(escape_material(text))==text

def test_empty_is_valid_but_missing_not():
    assert parse_output('P01',SCREEN)['fields']['判断']=='未发现'
    assert parse_output('P04-A','[[结果开始]]\n提取结果：未发现本类要求\n[[结果结束]]')['records']=={}
    for text in ('',SCREEN.replace('疑点：无\n',''),SCREEN.replace('[[结果结束]]',''),SCREEN.replace('未发现','没有')):
        with pytest.raises(ParseError):
            parse_output('P01',text)

def test_multiline_fidelity():
    s='[[结果开始]]\n处理结果: 已摘取\n保留内容：方案应包含：\n1. 部署\n条件：仅在现场时\n\n2. 培训\n疑点：无\n[[结果结束]]'
    assert parse_output('P03',s)['fields']['保留内容']=='方案应包含：\n1. 部署\n条件：仅在现场时\n\n2. 培训'

def test_extraction_ids_and_category():
    s='[[结果开始]]\n提取结果：已提取\n[[要求开始]]\n局部编号：R01\n类别：A\n材料依据：MAT1\n要求主题：格式\n要求内容：保留名称和顺序\n疑点：无\n[[要求结束]]\n[[结果结束]]'
    assert len(parse_output('P04-A',s,allowed_ids=['MAT1'])['records']['要求'])==1
    for bad in (s.replace('R01','REQ-global'),s.replace('MAT1','bogus'),s.replace('类别：A','类别：B'),s.replace('[[要求结束]]','')):
        with pytest.raises(ParseError):
            parse_output('P04-A',bad,allowed_ids=['MAT1'])

def test_directory_required_and_ids():
    s='[[结果开始]]\n状态：已形成候选骨架\n骨架：\n'+NODE+'\n调整说明：首次\n待核查事项：无\n[[结果结束]]'
    assert parse_output('P08',s,allowed_ids=['REQ1'])['records']['节点'][0]['节点']=='N01'
    for bad in (s.replace(NODE,''),s.replace('N01','GLOBAL'),s.replace('ROOT','N99'),s.replace('同级顺序：1','同级顺序：0')):
        with pytest.raises(ParseError):
            parse_output('P08',bad,allowed_ids=['REQ1'])

def test_expected_coverage():
    s='[[结果开始]]\n[[归属开始]]\n要求：REQ1\n状态：未承接\n承接节点：无\n承接范围：无\n未承接内容：实施\n依据或疑点：无对应章节\n[[归属结束]]\n[[结果结束]]'
    parse_output('P09',s,allowed_ids=['REQ1','REQ2'],expected_ids=['REQ1'])
    with pytest.raises(ParseError):
        parse_output('P09',s,allowed_ids=['REQ1','REQ2'],expected_ids=['REQ1','REQ2'])
    with pytest.raises(ParseError):
        parse_output('P09',s.replace('未承接','成功'),allowed_ids=['REQ1'])

def test_supplement_boundary_and_permission():
    supplement='[[补读开始]]\n引用或疑点：附件\n需要的信息：附件内容\n用途：确认相关性\n查找线索：附件一\n[[补读结束]]\n'
    s=SCREEN.replace('[[结果结束]]',supplement+'[[结果结束]]')
    assert len(parse_output('P01',s)['supplements'])==1
    with pytest.raises(ParseError):
        parse_output('P01',s.replace('[[补读结束]]','[[节点结束]]'))

def test_repair_branches():
    fail='[[格式修复失败]]\n原因：缺少要求正文\n[[格式修复失败结束]]'
    assert parse_output('P19',fail)['fields']['原因']=='缺少要求正文'
    assert parse_output('P19',SCREEN)['fields']['判断']=='未发现'
    with pytest.raises(ParseError):
        parse_output('P19','[[结果开始]]\n随意：猜测\n[[结果结束]]')

@pytest.mark.parametrize('task,text,allowed,expected',[
 ('P05','[[判定开始]]\n要求：REQ1\n结论：A\n涉及类别：A\n依据：集中编制说明\n[[判定结束]]',['REQ1'],['REQ1']),
 ('P06','结论：候选均不适用\n采用候选：无\n关联说明：对象不同\n仍缺少的信息：附件内容',[],None),
 ('P07','[[结构依据开始]]\n局部编号：S01\n要求依据：REQ1\n性质：非固定清单\n原文表达：参考清单\n结构含义：清单顺序\n疑点：无\n[[结构依据结束]]',['REQ1'],None),
 ('P10','[[调整建议开始]]\n要求：REQ1\n处理：新增外层章节\n目标或父节点：ROOT\n建议标题：实施\n位置说明：末尾\n承接内容：实施要求\n依据与限制：已有章节无法承接\n[[调整建议结束]]',['REQ1'],['REQ1']),
 ('P11','章节：CH1\n处理结果：无须新增子目录\n子目录：无\n未承接事项：无\n结构调整建议：无',['CH1'],None),
 ('P12','章节：CH1\n整合目录：无\n合并说明：无\n未解决事项：候选信息不足',['CH1'],None),
 ('P13','结论：合理交叉\n涉及节点：CH1\n涉及要求：REQ1\n依据：范围不同\n建议：保留',['CH1','REQ1'],None),
 ('P14','[[覆盖检查开始]]\n要求：REQ1\n结论：未覆盖\n承接节点：无\n承接说明：无\n未覆盖或不明确内容：实施方案\n修正建议：补充实施标题\n[[覆盖检查结束]]',['REQ1'],['REQ1']),
 ('P15','检查结论：未发现问题',[],None),
 ('P16','处理结论：无法在范围内修正\n替换目录：无\n问题处理说明：需修改外部节点\n仍需处理：扩大范围',[],None),
 ('P17','结果：无建议',[],None),
 ('P18','[[建议检查开始]]\n建议：EXT1\n结论：删除\n依据：无项目依据\n修改意见：无\n[[建议检查结束]]',['EXT1'],['EXT1']),
])
def test_remaining_task_contracts(task,text,allowed,expected):
    wrapped='[[结果开始]]\n'+text+'\n[[结果结束]]'
    assert parse_output(task,wrapped,allowed_ids=allowed,expected_ids=expected)
    with pytest.raises(ParseError):
        parse_output(task,wrapped.replace('[[结果结束]]',''),allowed_ids=allowed,expected_ids=expected)


@pytest.mark.parametrize('status', ['已找到', '部分找到'])
def test_found_reference_requires_selected_candidate(status):
    text = (
        '[[结果开始]]\n'
        f'结论：{status}\n采用候选：无\n关联说明：找到附件\n'
        '仍缺少的信息：无\n[[结果结束]]'
    )
    with pytest.raises(ParseError, match='select at least one'):
        parse_output('P06', text)


@pytest.mark.parametrize('task,kind,status_field,status,remainder_field', [
    ('P09', '归属', '状态', '已分配', '未承接内容'),
    ('P14', '覆盖检查', '结论', '已覆盖', '未覆盖或不明确内容'),
])
@pytest.mark.parametrize('targets,remainder', [('无', '无'), ('NODE1', '培训尚未承接')])
def test_success_requires_targets_and_no_remainder(
    task, kind, status_field, status, remainder_field, targets, remainder,
):
    extra = (
        '承接范围：全部\n依据或疑点：明确标题'
        if task == 'P09' else '承接说明：明确标题\n修正建议：无'
    )
    text = (
        f'[[结果开始]]\n[[{kind}开始]]\n要求：REQ1\n'
        f'{status_field}：{status}\n承接节点：{targets}\n'
        f'{remainder_field}：{remainder}\n{extra}\n'
        f'[[{kind}结束]]\n[[结果结束]]'
    )
    with pytest.raises(ParseError, match='targets and no remainder'):
        parse_output(task, text, allowed_ids=['REQ1', 'NODE1'])


@pytest.mark.parametrize('task,prefix,suffix', [
    ('P08', '状态：信息不足\n骨架：', '调整说明：无\n待核查事项：缺少附件'),
    ('P11', '章节：CH1\n处理结果：无须新增子目录\n子目录：',
     '未承接事项：无\n结构调整建议：无'),
    ('P11', '章节：CH1\n处理结果：信息不足\n子目录：',
     '未承接事项：缺少附件\n结构调整建议：无'),
])
def test_negative_generation_cannot_return_nodes(task, prefix, suffix):
    text = f'[[结果开始]]\n{prefix}\n{NODE}\n{suffix}\n[[结果结束]]'
    with pytest.raises(ParseError, match='Negative generation status'):
        parse_output(task, text, allowed_ids=['CH1', 'REQ1'])
