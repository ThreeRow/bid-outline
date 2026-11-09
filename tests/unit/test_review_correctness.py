"""Cross-stage regressions for R01/R02/R03/R04/R06; no external inference."""
import asyncio
from pathlib import Path
import tempfile

import pytest

from bid_outline.config import AppConfig
from bid_outline.document import chunk_document
from bid_outline.domain import (Document, DocumentElement, SourceRef, RequirementGroup,
                                OutlineNode, RequirementAssignment, ReviewFinding)
from bid_outline.model.client import FakeModelClient
from bid_outline.model.parsers import ParseError, parse_output
from bid_outline.workflow.runner import WorkflowRunner
from bid_outline.workflow.outline import closed_list_constraint, make_version, replace_branch


def runner():
    return WorkflowRunner(Path(tempfile.mkdtemp(prefix='bid-correctness-')),
                          AppConfig(model='fake', context_tokens=100000,service_context_tokens=100000, repair_rounds=1), FakeModelClient([]))


def document(texts):
    return Document(document_id='d', source_hash='hash', reader_name='test', elements=[
        DocumentElement(element_id=f'e{i}', kind='paragraph', order=i, text=text,
                        source_ref=SourceRef(document_id='d', element_ids=[f'e{i}']))
        for i, text in enumerate(texts)])


def requirement():
    return RequirementGroup(requirement_id='r', categories=['B'], topic='部署方案', content='提供离线部署方案')


def test_independent_background_reaches_generation_and_extensions_without_becoming_obligation():
    async def run():
        r = runner()
        r.document = document([])
        req = requirement()
        req.material_refs = ['format']
        r.requirements = [req]
        r._requirement_order = {'r': (0,)}
        r.materials = {key: dict(material_id=key, text=text, kind='refined_text', uncertainties='无', source_order=[i])
                       for i, (key, text) in enumerate([('format', '须提供部署方案'),
                                                       ('background', '禁止公网连接'), ('other', '开标地点在会议室')])}
        captured = {}
        async def call(task, material, **kwargs):
            captured[task] = material
            if task == 'P07':
                return {'records': {}}
            if task == 'P08':
                return {'task_id': 'skeleton', 'fields': {'状态': '已形成候选骨架'}, 'records': {'节点': [
                    {'节点': 'N01', '父节点': 'ROOT', '同级顺序': '1', '标题': '部署方案', '依据': 'r', '形成方式': '原文明示'}]}}
            if task == 'P11':
                return {'fields': {'章节': r.version.nodes[0].node_id, '处理结果': '无须新增子目录'}}
            assert task == 'P17'
            return {'records': {}}
        r.call = call
        try:
            await r.build_skeleton()
            r.assignments = [RequirementAssignment(requirement_id='r', outline_version_id=r.version.version_id,
                                status='已分配', targets=[{'node_id': r.version.nodes[0].node_id, 'scope': '全部'}])]
            await r.generate()
            await r.extend()
            for task in ('P08', 'P11', 'P17'):
                assert '禁止公网连接' in captured[task]
                assert '开标地点在会议室' not in captured[task]
            assert len(r.requirements) == 1
            assert r.sources([req])[0]['material_id'] == 'format'
        finally:
            await r.aclose()
    asyncio.run(run())


def test_supplements_accumulate_and_provenance_does_not_expand_scan_ownership():
    async def run():
        r = runner()
        r.document = document(['按附件编制方案', '附件甲：离线部署', '附件乙：恢复要求'])
        r.document_artifact_id = r.artifacts.put('document', r.document.model_dump(mode='json'))
        seen = []
        async def execute(task, inputs, **kwargs):
            if task == 'P01':
                return {'fields': {'判断': '有'}}
            if task == 'P06':
                return {'fields': {'结论': '已找到', '采用候选': 'e1' if len(seen) == 1 else 'e2'}}
            if task.startswith('P04'):
                return {'records': {}}
            assert task == 'P03'
            seen.append(inputs.copy())
            return {'task_id': 'refined', 'fields': {'处理结果': '已摘取', '保留内容': '离线部署与恢复', '疑点': '无'},
                    'supplements': [] if len(seen) == 3 else [{'引用或疑点': '附件', '需要的信息': '内容', '用途': '摘取', '查找线索': '附件'}]}
        r.executor.execute = execute
        chunk = chunk_document(r.document.model_copy(update={'elements': r.document.elements[:1]}))[0]
        try:
            await r._process_chunk(chunk)
            assert '附件甲：离线部署' in seen[2]['material'] and '附件乙：恢复要求' in seen[2]['material']
            material = next(m for m in r.materials.values() if m['kind'] == 'refined_text')
            assert material['element_ids'] == ['e0']
            assert {e for ref in material['owned_refs'] for e in ref['element_ids']} == {'e0'}
            assert {e for ref in material['source_refs'] for e in ref['element_ids']} == {'e0', 'e1', 'e2'}
            req = requirement()
            req.material_refs = [material['material_id']]
            r.requirements = [req]
            r.commit([OutlineNode(node_id='root', title='方案', basis_requirement_ids=['r'])], 'initial')
            assert {e for ref in r.version.nodes[0].source_refs for e in ref.element_ids} == {'e0', 'e1', 'e2'}
        finally:
            await r.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('fail_coverage', [False, True])
def test_repair_reassigns_deleted_target_and_rechecks_old_requirement(fail_coverage):
    async def run():
        r = runner()
        r.requirements = [requirement()]
        r.commit([OutlineNode(node_id='root', title='实施', basis_requirement_ids=['r']),
                  OutlineNode(node_id='old', parent_id='root', title='旧方案', basis_requirement_ids=['r'])], 'initial')
        r.assignments = [RequirementAssignment(requirement_id='r', outline_version_id=r.version.version_id,
                                              status='已分配', targets=[{'node_id': 'old', 'scope': '全部'}])]
        original_review = r.review
        initial = True
        async def review(affected=None):
            nonlocal initial
            if initial:
                initial = False
                return [ReviewFinding(finding_id='old-finding', kind='coverage', node_ids=['old'], requirement_ids=['r'])]
            return await original_review(affected)
        r.review = review
        calls = []
        async def call(task, material, **kwargs):
            calls.append(task)
            if task == 'P16':
                return {'task_id': 'replacement', 'fields': {'处理结论': '已修正'}, 'records': {'节点': [
                    {'节点': 'N01', '父节点': 'root', '同级顺序': '1', '标题': '离线方案', '依据': 'r', '形成方式': '有依据归纳'}]}}
            if task == 'P09':
                new = next(n.node_id for n in r.version.nodes if n.parent_id == 'root')
                return {'records': {'归属': [{'承接节点': new, '状态': '已分配', '承接范围': '全部', '未承接内容': '无', '依据或疑点': '方案'}]}}
            if task == 'P14':
                assert kwargs['expected'] == ['r']
                if fail_coverage:
                    r.unprocessed.append({'task_type': 'P14'})
                    return None
                return {'task_id': 'coverage', 'records': {'覆盖检查': [{'要求': 'r', '承接节点': r.assignments[0].targets[0]['node_id'],
                        '结论': '已覆盖', '未覆盖或不明确内容': '无'}]}}
            assert task == 'P15'
            return {'task_id': 'structure', 'fields': {'检查结论': '未发现问题'}, 'records': {}}
        r.call = call
        try:
            await r.review_and_repair()
            assert calls.index('P16') < calls.index('P09') < calls.index('P14')
            assert all(t['node_id'] in {n.node_id for n in r.version.nodes} for a in r.assignments for t in a.targets)
            assert all(a.outline_version_id == r.version.version_id for a in r.assignments)
            assert next(f for f in r.findings if f.finding_id == 'old-finding').status == ('open' if fail_coverage else 'superseded')
        finally:
            await r.aclose()
    asyncio.run(run())


def restriction(parent='ROOT', titles='A\nB'):
    return {'性质': '禁止新增', '限制父级': parent, '允许子项': titles,
            '要求依据': 'r', '原文表达': '只允许列出的直属章节，不得新增', '结构含义': '封闭清单'}


def test_closed_list_blocks_initial_and_later_additions_but_allows_lower_detail():
    a = OutlineNode(node_id='a', title='A', order=1)
    b = OutlineNode(node_id='b', title='B', order=2)
    c = OutlineNode(node_id='c', title='C', order=3)
    constraint = closed_list_constraint(restriction(), [a, b])
    with pytest.raises(ValueError, match='forbids'):
        make_version([a, b, c], None, 'invalid-first', [constraint])
    base = make_version([a, b], None, 'initial', [constraint])
    with pytest.raises(ValueError, match='forbids'):
        make_version([a, b, c], base, 'invalid-later')
    detail = OutlineNode(node_id='detail', title='具体方案', parent_id='a')
    assert len(replace_branch(base, 'a', [detail])) == 3
    child_constraint = closed_list_constraint(restriction('A', '无'), [a, b])
    base = make_version([a, b], None, 'closed-child', [child_constraint])
    with pytest.raises(ValueError, match='forbids'):
        replace_branch(base, 'a', [detail])
    with pytest.raises(ValueError, match='ambiguous'):
        closed_list_constraint(restriction('missing'), [a, b])


def test_closed_list_protocol_requires_explicit_scope():
    body = ('[[结果开始]]\n[[结构依据开始]]\n局部编号：S01\n要求依据：r\n性质：禁止新增\n'
            '原文表达：仅允许A和B\n结构含义：不得新增顶层章节\n疑点：无\n')
    end = '[[结构依据结束]]\n[[结果结束]]'
    with pytest.raises(ParseError, match='explicit'):
        parse_output('P07', body + end, allowed_ids=['r'])
    result = parse_output('P07', body + '限制父级：ROOT\n允许子项：A\nB\n' + end, allowed_ids=['r'])
    assert result['records']['结构依据'][0]['允许子项'] == 'A\nB'


@pytest.mark.parametrize('old_titles', [['A', 'B'], ['A', 'B', 'C']])
def test_rejected_late_skeleton_cannot_lose_known_restriction(old_titles):
    async def run():
        r = runner()
        r.config.chunk_chars = 1
        r.requirements = [requirement(), requirement().model_copy(update={'requirement_id': 'r2'})]
        r._requirement_order = {'r': (0,), 'r2': (1,)}
        round_no = 0
        evidence_no = 0
        async def call(task, material, **kwargs):
            nonlocal round_no, evidence_no
            if task == 'P07':
                evidence_no += 1
                record = restriction()
                record['要求依据'] = 'r2'
                return {'records': {'结构依据': [record] if evidence_no == 2 else []}}
            assert task == 'P08'
            round_no += 1
            if round_no == 1:
                records = [{'节点': f'N{i:02}', '父节点': 'ROOT', '同级顺序': str(i), '标题': title,
                            '依据': 'r', '形成方式': '原文明示'} for i, title in enumerate(old_titles, 1)]
            else:
                records = [{'节点': n.node_id, '父节点': 'ROOT', '同级顺序': str(n.order), '标题': n.title,
                            '依据': 'r', '形成方式': '原文明示'} for n in r.version.nodes]
                records.append({'节点': 'N99', '父节点': 'ROOT', '同级顺序': '99', '标题': '非法新增', '依据': 'r2', '形成方式': '有依据归纳'})
            return {'task_id': f'skeleton{round_no}', 'fields': {'状态': '已形成候选骨架'}, 'records': {'节点': records}}
        r.call = call
        try:
            await r.build_skeleton()
            assert r.unprocessed
            if 'C' in old_titles:
                assert r.version is None
                assert any(item['scope'] == 'skeleton_restrictions' for item in r.unprocessed)
            else:
                assert any(c.kind == 'no_add_children' for c in r.version.constraints)
                with pytest.raises(ValueError, match='forbids'):
                    r.commit(r.version.nodes + [OutlineNode(node_id='bad', title='C', order=3, basis_requirement_ids=['r'])], 'later')
        finally:
            await r.aclose()
    asyncio.run(run())


def test_accumulated_supplements_replay_from_durable_calls():
    def wrapped(body):
        return '[[结果开始]]\n' + body + '\n[[结果结束]]'
    def extraction(need=None):
        body = '处理结果：已摘取\n保留内容：离线部署与恢复\n疑点：无'
        if need:
            body += f'\n[[补读开始]]\n引用或疑点：附件{need}\n需要的信息：附件内容\n用途：摘取\n查找线索：附件{need}\n[[补读结束]]'
        return wrapped(body)
    async def run():
        r = runner()
        path, config = r.run_dir, r.config
        await r.aclose()
        inputs = document(['按附件编制方案', '附件甲：离线部署', '附件乙：恢复要求'])
        responses = {'P03': [extraction('甲'), extraction('乙'), extraction()], 'P06': [
            wrapped(f'结论：已找到\n采用候选：{key}\n关联说明：对应附件\n仍缺少的信息：无') for key in ('e1', 'e2')]}
        first = FakeModelClient(responses)
        result = None
        for client in (first, FakeModelClient([])):
            active = WorkflowRunner(path, config, client)
            active.document = inputs
            active.document_artifact_id = active.artifacts.put('document', inputs.model_dump(mode='json'))
            try:
                current = await active.call('P03', '按附件编制方案')
                assert current['supplement_material_ids'] == ['e1', 'e2']
                if result is not None:
                    assert current == result
                    assert client.requests == []
                result = current
            finally:
                await active.aclose()
        assert len(first.requests) == 5
    asyncio.run(run())
