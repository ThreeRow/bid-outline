"""R05/R08 regressions with retained audit directories and no model service."""
import asyncio
import json
from pathlib import Path
import tempfile

import pytest

from bid_outline.config import AppConfig
from bid_outline.document import chunk_document
from bid_outline.document.lookup import owned_keyword_hit
from bid_outline.domain import (Document, DocumentElement, SourceRef, OutlineNode,
                                OutlineConstraint, RequirementGroup, RequirementAssignment, ReviewFinding)
from bid_outline.model.client import FakeModelClient, ModelResponse
from bid_outline.model.executor import BudgetExceeded
from bid_outline.workflow.runner import WorkflowRunner


def runner(client=None, **config):
    return WorkflowRunner(Path(tempfile.mkdtemp(prefix='bid-scope-review-')),
                          AppConfig(model='fake', context_tokens=100000,service_context_tokens=100000, repair_rounds=1,
                                    format_retries=0, transport_retries=0, **config), client or FakeModelClient([]))


def section(material, name):
    return json.JSONDecoder().raw_decode(material.split(name + '：\n', 1)[1])[0]


@pytest.mark.parametrize('children', [1, 2])
def test_parent_only_finding_is_explicitly_blocked_without_duplicate_child_obligations(children):
    async def run():
        r = runner()
        r.requirements = [RequirementGroup(requirement_id='p', topic='父要求', content='父级完整义务及条件')]
        r.commit([OutlineNode(node_id='root', title='root')] + [
            OutlineNode(node_id=f'c{i}', title=f'child{i}', parent_id='root', order=i+1) for i in range(children)], 'initial')
        r.assignments = [RequirementAssignment(requirement_id='p', outline_version_id=r.version.version_id,
                                               status='已分配', targets=[{'node_id': 'root', 'scope': '全部'}])]
        async def review(*args):
            return [ReviewFinding(finding_id='parent', kind='coverage', node_ids=['root'], requirement_ids=['p'])]
        r.review = review
        def budget(*args, **kwargs):
            raise BudgetExceeded('test budget')
        r.check_budget = budget
        async def call(*args, **kwargs):
            pytest.fail('parent issue cannot be assigned to arbitrary children')
        r.call = call
        try:
            await r.review_and_repair()
            assert r.findings[0].status == 'open'
            assert len(r.unprocessed) == 1
            blocked = r.unprocessed[0]
            assert blocked['scope'] == 'root' and 'budget blocked' in blocked['error']
            assert blocked['requirements'][0]['content'] == '父级完整义务及条件'
            assert blocked['findings'][0]['finding_id'] == 'parent'
        finally:
            await r.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('child_budget,illegal', [(True, False), (False, False), (False, True)])
def test_local_repairs_inherit_parent_conditions_and_keep_blocked_parent_open(child_budget, illegal):
    async def run():
        r = runner()
        r.requirements = [RequirementGroup(requirement_id='p', topic='父', content='仅离线环境适用的完整父义务'),
                          RequirementGroup(requirement_id='c', topic='子', content='恢复方案')]
        nodes = [OutlineNode(node_id='root', title='root'), OutlineNode(node_id='child', parent_id='root', title='child')]
        constraints = [OutlineConstraint(constraint_id='closed', kind='no_add_children', origin='文件明确',
                                        affected_node_ids=['root'], parent_node_id='root', allowed_child_titles=['child']),
                       OutlineConstraint(constraint_id='keep', kind='preserve', origin='系统策略', affected_node_ids=['child'])]
        if illegal:
            constraints.append(OutlineConstraint(constraint_id='no-detail', kind='no_add_children', origin='文件明确',
                                                 affected_node_ids=['child'], parent_node_id='child', allowed_child_titles=[]))
        r.commit(nodes, 'initial', constraints)
        r.assignments = [RequirementAssignment(requirement_id=rid, outline_version_id=r.version.version_id,
                         status='已分配', targets=[{'node_id': target, 'scope': '全部'}], shared_conditions='所有响应仅限离线环境')
                         for rid, target in [('p', 'root'), ('c', 'child')]]
        first = True
        original_review = r.review
        async def review(affected=None):
            nonlocal first
            if first:
                first = False
                return [ReviewFinding(finding_id='parent', kind='coverage', node_ids=['root'], requirement_ids=['p']),
                        ReviewFinding(finding_id='local', kind='coverage', node_ids=['child'], requirement_ids=['c'])]
            return await original_review(affected)
        r.review = review
        def budget(task, material, **kwargs):
            if task == 'P16' and (section(material, '边界父节点')['node_id'] == 'root' or child_budget):
                raise BudgetExceeded('test budget')
        r.check_budget = budget
        calls = []
        async def call(task, material, **kwargs):
            calls.append((task, kwargs))
            if task == 'P16':
                assert kwargs['scope'] == 'child'
                assert {x['requirement_id'] for x in section(material, '范围内全部要求')} == {'p', 'c'}
                assert section(material, '继承的父级要求ID') == ['p']
                assert '所有响应仅限离线环境' in material and '不是每个子树都必须新增' in material
                assert {c['constraint_id'] for c in section(material, '必须保留')} >= {'closed', 'keep'}
                return {'task_id': 'repair', 'fields': {'处理结论': '已修正'}, 'records': {'节点': [
                    {'节点': 'N01', '父节点': 'child', '同级顺序': '1', '标题': '恢复方案', '依据': 'c', '形成方式': '有依据归纳'}]}}
            if task == 'P14':
                return {'task_id': 'coverage-'+kwargs['expected'][0], 'records': {'覆盖检查': [
                    {'要求': kwargs['expected'][0], '承接节点': 'child', '结论': '已覆盖', '未覆盖或不明确内容': '无'}]}}
            assert task == 'P15'
            return {'task_id': 'structure', 'fields': {'检查结论': '未发现问题'}, 'records': {}}
        r.call = call
        try:
            await r.review_and_repair()
            assert next(f for f in r.findings if f.finding_id == 'parent').status == 'open'
            if child_budget:
                assert calls == [] and {u['scope'] for u in r.unprocessed} == {'root', 'child'}
                assert all(f.status == 'open' for f in r.findings)
            elif illegal:
                assert any(i['kind'] == 'repair_rejected' for i in r.issues)
                assert len(r.version.nodes) == 2
                assert all(f.status == 'open' for f in r.findings)
            else:
                assert {kw['expected'][0] for task, kw in calls if task == 'P14'} == {'p', 'c'}
                assert next(f for f in r.findings if f.finding_id == 'local').status == 'superseded'
        finally:
            await r.aclose()
    asyncio.run(run())


def wrapped(body):
    return '[[结果开始]]\n' + body + '\n[[结果结束]]'


POSITIVE = wrapped('判断：有\n相关主题：技术\n疑点：无\n引用线索：无')
NEGATIVE = wrapped('判断：未发现\n相关主题：无\n疑点：无\n引用线索：无')
REFINED = wrapped('处理结果：已摘取\n保留内容：模型可重排正文，不要求逐字一致\n疑点：无')
EMPTY = wrapped('提取结果：未发现本类要求')


def paragraph(text):
    return Document(document_id='d', source_hash='hash', reader_name='test', elements=[DocumentElement(
        element_id='e', kind='paragraph', order=0, text=text,
        source_ref=SourceRef(document_id='d', element_ids=['e'], precision='parsed_text', start=70, end=70+len(text)))])


@pytest.mark.parametrize('adaptive', [False, True])
def test_keyword_review_uses_negative_leaf_owned_text_despite_positive_sibling(adaptive):
    async def run():
        doc = paragraph('甲'*300 + '技术方案验收' + '乙'*294 + '丙'*300)
        plan = {'P01': [POSITIVE, NEGATIVE, NEGATIVE], 'P02': [NEGATIVE], 'P03': [REFINED],
                **{f'P04-{c}': [EMPTY] for c in 'ABC'}}
        if adaptive:
            # Parent fails; leaves include one positive and one negative hit.
            doc = paragraph('甲'*300 + '技术方案验收' + '乙'*294)
            plan['P01'] = [ModelResponse('partial', finish_reason='length'), POSITIVE, NEGATIVE]
        client = FakeModelClient(plan)
        r = runner(client, chunk_chars=1200 if adaptive else 300)
        try:
            result = await r.run(doc)
            assert result['unprocessed_ranges'] == []
            assert sum(q['_task_type'] == 'P02' for q in client.requests) == 1
            assert sum(q['_task_type'] == 'P03' for q in client.requests) == 1
            leaves = list(r._leaf_chunks.values())
            if adaptive:
                assert len(leaves) == 2 and all(':split:' in c.chunk_id for c in leaves)
            reviewed = next(q for q in client.requests if q['_task_type'] == 'P02')
            assert '技术方案验收' in reviewed['messages'][1]['content']
            assert '甲甲甲' not in reviewed['messages'][1]['content']
        finally:
            await r.aclose()
    asyncio.run(run())


def test_table_owned_rows_exclude_header_and_inherited_keyword_context():
    async def run():
        rows = [{'row': 0, 'cell_ids': ['h'], 'header': True, 'text': '技术方案表头' + '甲'*300},
                {'row': 1, 'cell_ids': ['a'], 'text': '验收要求' + '乙'*300},
                {'row': 2, 'cell_ids': ['b'], 'text': '普通内容' + '丙'*300,
                 'inherited_text': '方案共同父项', 'inherited_cell_ids': ['h']}]
        doc = Document(document_id='d', source_hash='hash', reader_name='test', elements=[DocumentElement(
            element_id='e', kind='table', order=0, text='派生文字'*400,
            source_ref=SourceRef(document_id='d', element_ids=['e'], table_id='t', cell_ids=['h','a','b'], precision='table'),
            metadata={'table_rows': rows})])
        chunks = chunk_document(doc, 300)
        assert [owned_keyword_hit(doc, c) for c in chunks] == [True, True, False]
        assert '技术方案表头' in chunks[-1].text and '方案共同父项' in chunks[-1].text
        client = FakeModelClient({'P01': [POSITIVE, NEGATIVE, NEGATIVE], 'P02': [NEGATIVE], 'P03': [REFINED],
                                  **{f'P04-{c}': [EMPTY] for c in 'ABC'}})
        r = runner(client, chunk_chars=300)
        try:
            result = await r.run(doc)
            assert result['unprocessed_ranges'] == []
            assert sum(q['_task_type'] == 'P02' for q in client.requests) == 1
            assert sum(q['_task_type'] == 'P03' for q in client.requests) == 1
        finally:
            await r.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['P01', 'P03', 'P02'])
def test_failed_ranges_are_not_treated_as_negative_or_successful(failure):
    async def run():
        plan = {'P01': [NEGATIVE if failure == 'P02' else POSITIVE], 'P03': [REFINED], 'P02': [NEGATIVE]}
        plan[failure] = ['invalid']
        client = FakeModelClient(plan)
        r = runner(client)
        try:
            result = await r.run(paragraph('技术方案'))
            assert result['status'] == 'incomplete'
            assert any(u['task_type'] == failure for u in result['unprocessed_ranges'])
            assert sum(q['_task_type'] == 'P02' for q in client.requests) == (failure == 'P02')
        finally:
            await r.aclose()
    asyncio.run(run())


def test_parsed_offsets_are_relative_to_element_and_context_has_no_ownership():
    doc = paragraph('技术方案' + '普通内容'*30)
    chunk = chunk_document(doc, 1000)[0]
    chunk.owned_refs[0].start = 74
    chunk.context_refs = [doc.elements[0].source_ref]
    assert not owned_keyword_hit(doc, chunk)


def test_keyword_retry_split_only_reviews_hit_leaf_and_never_replays_replaced_parent():
    async def run():
        doc = paragraph('甲'*300 + '技术方案' + '乙'*296)
        client = FakeModelClient({'P01': [NEGATIVE, NEGATIVE],
                                  'P02': [ModelResponse('partial', finish_reason='length'), NEGATIVE]})
        r = runner(client, chunk_chars=1200)
        try:
            result = await r.run(doc)
            assert result['unprocessed_ranges'] == []
            leaves = list(r._leaf_chunks.values())
            assert len(leaves) == 2 and all(':split:' in c.chunk_id for c in leaves)
            assert [q['_task_type'] for q in client.requests] == ['P01', 'P02', 'P01', 'P02']
            last = client.requests[-1]['messages'][1]['content']
            assert '技术方案' in last and '甲甲甲' not in last
            assert set(r._leaf_chunks) <= r._negative
        finally:
            await r.aclose()
    asyncio.run(run())


def test_cross_branch_finding_is_not_turned_into_independent_child_repairs():
    from bid_outline.workflow.repair_scope import RepairScope
    nodes = [OutlineNode(node_id='root', title='root'),
             OutlineNode(node_id='a', title='a', parent_id='root'),
             OutlineNode(node_id='b', title='b', parent_id='root', order=2)]
    finding = ReviewFinding(finding_id='cross', kind='cross_branch', node_ids=['a', 'b'])
    scope = RepairScope('root', 'root', [], [], [], [finding], set())
    children, blocked = scope.subdivide(nodes)
    assert children == [] and blocked == [finding]
