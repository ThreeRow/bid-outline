"""R11/R12: typed visibility and literal text survive real executor adoption."""
import asyncio
import json
from pathlib import Path
import re
import tempfile

import pytest

from bid_outline.config import AppConfig
from bid_outline.domain import OutlineNode, OutlineVersion, RequirementGroup
from bid_outline.model.client import FakeModelClient
from bid_outline.model.executor import ModelExecutor
from bid_outline.model.parsers import ParseError, parse_output
from bid_outline.model.references import prepare_inputs
from bid_outline.model.task_specs import render_task, unescape_material
from bid_outline.storage import ArtifactStore, RunRepository
from bid_outline.workflow.common import present
from bid_outline.workflow.runner import WorkflowRunner


def wrap(body):
    return '[[结果开始]]\n' + body + '\n[[结果结束]]'


def record(kind, **fields):
    return f'[[{kind}开始]]\n' + '\n'.join(f'{key}：{value}' for key, value in fields.items()) + f'\n[[{kind}结束]]'


def data(request):
    return {k: unescape_material(v) for k, v in re.findall(
        r'\[\[输入数据开始\]\]\n([^：\n]+)：\n(.*?)\n\[\[输入数据结束\]\]',
        request['messages'][1]['content'], re.S)}


def extraction(mid, prose):
    return wrap('提取结果：已提取\n' + record('要求', 局部编号='R01', 类别='C', 材料依据=mid,
                                           要求主题=prose, 要求内容=prose, 疑点=prose))


def test_literal_aliases_and_real_ids_survive_adoption_recovery_and_different_call_aliases():
    async def run():
        path = Path(tempfile.mkdtemp(prefix='bid-literal-contract-'))
        repo, store = RunRepository(path), ArtifactStore(path)
        mid, other = 'M_zzzzzzzzzzzzzzzzzzzz', 'M_aaaaaaaaaaaaaaaaaaaa'
        prose = f'设备MAT01、REQ01、NODE01；原文编号{mid}。\n条件：仅限离线\\路径[x]，不得改变！'
        client = FakeModelClient([extraction('MAT01', prose), extraction('MAT02', prose)])
        executor = ModelExecutor(AppConfig(prompt_view='legacy-json', model='fake', format_retries=0), client, repo, store)
        first = prepare_inputs({'material': present(材料={'material_id': mid, 'text': prose, 'kind': 'refined_text'}), 'short_ids': True})
        first = json.loads(json.dumps(first, ensure_ascii=False))  # no str-subclass state survives
        try:
            adopt = executor._adopt
            def interrupted(*args):
                raise RuntimeError('crash before adoption')
            executor._adopt = interrupted
            with pytest.raises(RuntimeError, match='before adoption'):
                await executor.execute('P04-C', first, task_id='one')
            executor._adopt = adopt
            parsed = await executor.execute('P04-C', first, task_id='one')
            assert len(client.requests) == 1
            assert await executor.execute('P04-C', first, task_id='one') == parsed
            adopted = store.get(repo.get_task('one')['accepted_artifact_id'])
            for field in ('要求主题', '要求内容', '疑点'):
                assert adopted['records']['要求'][0][field] == prose
            assert adopted['records']['要求'][0]['材料依据'] == mid
            material = data(client.requests[0])['完整材料包']
            body = json.loads(material.split('材料：\n')[1])
            assert body['material_id'] == 'MAT01' and body['text'] == prose
            second = {'material': present(材料=[{'material_id': other, 'text': '另一个完整材料'},
                                              {'material_id': mid, 'text': prose}]), 'short_ids': True}
            result = await executor.execute('P04-C', second, task_id='two')
            assert result['records']['要求'][0]['材料依据'] == mid
            assert result['records']['要求'][0]['要求内容'] == prose
            assert len(client.requests) == 2
            raw = [json.loads(p.read_text())['content'] for p in (path/'calls').rglob('response.model.json')]
            assert extraction('MAT01', prose) in raw and extraction('MAT02', prose) in raw
        finally:
            await executor.aclose()
            repo.close()
    asyncio.run(run())


def test_source_json_and_p07_titles_are_free_text_but_node_identity_is_structured():
    rid, nid = 'R_12345678901234567890', 'N_12345678901234567890'
    literal = '{"node_id": "' + nid + '", "requirement_id": "REQ01", "材料依据": "MAT01"}'
    material = present(正文=literal, 节点=OutlineNode(node_id=nid, title=nid, basis_requirement_ids=[rid]),
                       结构依据={'要求依据': rid, '原文表达': literal, '限制父级': nid, '允许子项': 'MAT01\nREQ01'},
                       要求=RequirementGroup(requirement_id=rid, topic='NODE01', content=literal))
    inputs = prepare_inputs({'material': material, 'id_aliases': {rid: 'REQ01', nid: 'NODE01'}})
    wire = data({'messages': render_task('P08', inputs, view_version='legacy-json')})['完整材料包']
    assert literal in wire  # standalone raw JSON is never traversed
    node = json.JSONDecoder().raw_decode(wire.split('节点：\n')[1])[0]
    assert node['node_id'] == 'NODE01' and node['title'] == nid
    evidence = json.JSONDecoder().raw_decode(wire.split('结构依据：\n')[1])[0]
    assert evidence['要求依据'] == 'REQ01'
    assert evidence['限制父级'] == nid and evidence['允许子项'] == 'MAT01\nREQ01'
    raw = render_task('P01', {'material': literal, 'allowed_ids': [nid], 'id_aliases': {nid: 'NODE01'}}, view_version='legacy-json')
    assert data({'messages': raw})['完整材料包'] == literal


GROUPS = {'requirements': ['REQ1'], 'nodes': ['NODE1'], 'materials': ['MAT1'],
          'elements': ['SRC1'], 'suggestions': ['SUG1']}
CASES = [
    ('P15', '检查结论：发现问题\n' + record('问题', 局部编号='P01', 类型='其他', 涉及节点='MAT1', 涉及要求='REQ1', 问题='问题', 依据='无', 建议修正范围='局部')),
    ('P15', '检查结论：发现问题\n' + record('问题', 局部编号='P01', 类型='其他', 涉及节点='NODE1', 涉及要求='NODE1', 问题='问题', 依据='无', 建议修正范围='局部')),
    ('P06', '结论：已找到\n采用候选：MAT1\n关联说明：找到\n仍缺少的信息：无'),
    ('P18', record('建议检查', 建议='NODE1', 结论='保留', 依据='无', 修改意见='无')),
    ('P09', record('归属', 要求='REQ1', 状态='已分配', 承接节点='MAT1', 承接范围='全部', 未承接内容='无', 依据或疑点='无')),
    ('P14', record('覆盖检查', 要求='REQ1', 结论='已覆盖', 承接节点='MAT1', 承接说明='全部', 未覆盖或不明确内容='无', 修正建议='无')),
    ('P04-C', '提取结果：已提取\n' + record('要求', 局部编号='R01', 类别='C', 材料依据='NODE1', 要求主题='主题', 要求内容='内容', 疑点='无')),
    ('P07', record('结构依据', 局部编号='S01', 要求依据='MAT1', 性质='非固定清单', 原文表达='清单', 结构含义='清单', 疑点='无')),
    ('P13', '结论：合理交叉\n涉及节点：REQ1\n涉及要求：REQ1\n依据：无\n建议：无'),
    ('P17', '结果：有建议\n' + record('建议', 局部编号='E01', 对应章节='NODE1', 建议主题='主题', 建议内容='内容', 来源性质='项目需求推理', 相关依据='REQ1', 适用理由与条件='条件')),
]


@pytest.mark.parametrize('task,body', CASES)
def test_every_reference_role_rejects_other_visible_object_types(task, body):
    allowed = [value for values in GROUPS.values() for value in values]
    with pytest.raises(ParseError, match='IDs visible'):
        parse_output(task, wrap(body), allowed_ids=allowed, id_groups=GROUPS)


def test_invisible_object_and_scalar_list_are_rejected():
    body = wrap('结论：已找到\n采用候选：SRC2\n关联说明：找到\n仍缺少的信息：无')
    with pytest.raises(ParseError):
        parse_output('P06', body, allowed_ids=['SRC1', 'SRC2'], id_groups={'elements': ['SRC1']})
    body = wrap(record('建议检查', 建议='SUG1、SUG2', 结论='保留', 依据='无', 修改意见='无'))
    with pytest.raises(ParseError, match='scalar'):
        parse_output('P18', body, allowed_ids=['SUG1','SUG2'], id_groups={'suggestions': ['SUG1','SUG2']})


def test_reference_metadata_can_only_preserve_same_existing_node_basis():
    def response(node):
        return wrap('状态：已形成候选骨架\n骨架：\n' + record('节点', 节点=node, 父节点='ROOT', 同级顺序='1',
                    标题='原标题', 依据='OLD', 形成方式='原文明示', 说明='保留') + '\n调整说明：保留\n待核查事项：无')
    args = dict(allowed_ids=['NODE1', 'NODE2', 'OLD'], id_groups={'nodes': ['NODE1','NODE2'], 'support_requirements': ['OLD']},
                existing_node_basis={'NODE1': ['OLD'], 'NODE2': []})
    assert parse_output('P08', response('NODE1'), **args)
    for node in ('N01', 'NODE2'):
        with pytest.raises(ParseError, match='metadata cannot establish'):
            parse_output('P08', response(node), **args)


def test_p09_rendered_request_does_not_grow_with_global_unrelated_objects():
    async def run():
        wires = []
        for count in (0, 200):
            req = RequirementGroup(requirement_id='R_11111111111111111111', topic='实施', content='完整要求', material_refs=['M_11111111111111111111'])
            client = FakeModelClient([wrap(record('归属', 要求='REQ01', 状态='已分配', 承接节点='NODE01', 承接范围='全部', 未承接内容='无', 依据或疑点='无'))])
            r = WorkflowRunner(Path(tempfile.mkdtemp(prefix='bid-visible-p09-')), AppConfig(prompt_view='legacy-json', model='fake'), client)
            r.version = OutlineVersion(version_id='v', nodes=[OutlineNode(node_id='N_11111111111111111111', title='实施', basis_requirement_ids=[req.requirement_id])])
            r.requirements = [req] + [RequirementGroup(requirement_id=f'unrelated{i}', topic='无关', content='无关正文'*50) for i in range(count)]
            r.materials = {mid: {'material_id': mid, 'text': '实际完整依据', 'kind': 'refined_text', 'uncertainties': '无', 'source_order': [0,0,0]}
                           for mid in req.material_refs + [f'unrelated-m{i}' for i in range(count)]}
            try:
                await r.assign_all({req.requirement_id})
                assert r.assignments[0].status == '已分配'
                wires.append(client.requests[0]['messages'])
                assert '允许引用ID' not in data(client.requests[0])
                assert 'unrelated' not in json.dumps(wires[-1])
            finally:
                await r.aclose()
        assert wires[0] == wires[1]
    asyncio.run(run())


def test_native_short_id_collision_does_not_hide_object_or_confuse_new_nodes():
    async def run():
        root = Path(tempfile.mkdtemp(prefix='bid-alias-collision-'))
        repo = RunRepository(root)
        long = 'M_12345678901234567890'
        client = FakeModelClient([extraction('MAT02', '型号MAT01不得改写')])
        executor = ModelExecutor(AppConfig(prompt_view='legacy-json', model='fake'), client, repo, ArtifactStore(root))
        try:
            result = await executor.execute('P04-C', {'material': present(材料=[
                {'material_id': 'MAT01', 'text': '原生短ID对象'}, {'material_id': long, 'text': '另一完整材料'}]),
                'short_ids': True}, task_id='collision')
            assert result['records']['要求'][0]['材料依据'] == long
            assert data(client.requests[0])['材料ID'] == 'MAT01、MAT02'
            assert result['records']['要求'][0]['要求内容'] == '型号MAT01不得改写'
        finally:
            await executor.aclose()
            repo.close()
    asyncio.run(run())


def test_executor_maps_existing_parent_and_requirement_but_not_local_node_or_title():
    async def run():
        path = Path(tempfile.mkdtemp(prefix='bid-node-contract-'))
        repo = RunRepository(path)
        rid, nid = 'R_12345678901234567890', 'N_12345678901234567890'
        title = '设备NODE01及REQ01；' + nid
        response = wrap('处理结论：已修正\n替换目录：\n' + record('节点', 节点='N01', 父节点='NODE01', 同级顺序='1',
                         标题=title, 依据='REQ01', 形成方式='有依据归纳', 说明='NODE01为原型号') +
                         '\n问题处理说明：REQ01只作字面说明\n仍需处理：无')
        executor = ModelExecutor(AppConfig(prompt_view='legacy-json', model='fake'), FakeModelClient([response]), repo, ArtifactStore(path))
        try:
            result = await executor.execute('P16', {'material': present(
                父节点=OutlineNode(node_id=nid, title='根'),
                要求=RequirementGroup(requirement_id=rid, topic='设备', content='内容')), 'short_ids': True}, task_id='node')
            node = result['records']['节点'][0]
            assert node['节点'] == 'N01' and node['父节点'] == nid and node['依据'] == rid
            assert node['标题'] == title and node['说明'] == 'NODE01为原型号'
            assert result['fields']['问题处理说明'] == 'REQ01只作字面说明'
        finally:
            await executor.aclose()
            repo.close()
    asyncio.run(run())


def test_workflow_rejects_globally_known_but_unprovided_requirement():
    async def run():
        path = Path(tempfile.mkdtemp(prefix='bid-invisible-contract-'))
        hidden = 'R_hidden12345678901234567890'
        response = wrap(record('判定', 要求=hidden, 结论='C', 涉及类别='C', 依据='假冒不可见要求'))
        client = FakeModelClient([response])
        r = WorkflowRunner(path, AppConfig(prompt_view='legacy-json', model='fake', format_retries=0), client)
        req = RequirementGroup(requirement_id='R_visible12345678901234567890', topic='待定', content='完整当前要求')
        r.requirements = [req, RequirementGroup(requirement_id=hidden, topic='无关', content='无关正文')]
        try:
            result = await r.call('P05', present(要求=req), allowed=r.allowed(), expected=[req.requirement_id])
            assert result is None and r.unprocessed[0]['task_type'] == 'P05'
            assert hidden not in json.dumps(client.requests[0]['messages'])
        finally:
            await r.aclose()
    asyncio.run(run())


def test_element_sources_are_valid_material_evidence_and_supplement_views_remain_typed():
    source = {'element_id': 'source_12345678901234567890', 'text': '原文MAT01', 'kind': 'paragraph'}
    original = present(材料={'material_id': 'M_12345678901234567890', 'text': '初始完整材料'})
    view = prepare_inputs({'material': present(原任务完整材料=original, 补充材料=[source])})
    assert view['id_groups']['elements'] == [source['element_id']]
    assert view['id_groups']['materials'] == ['M_12345678901234567890']
    parsed = parse_output('P04-C', extraction(source['element_id'], '原文MAT01'),
                          allowed_ids=view['allowed_ids'], id_groups=view['id_groups'])
    assert parsed['records']['要求'][0]['材料依据'] == source['element_id']
