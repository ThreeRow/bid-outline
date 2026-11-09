import asyncio

from bid_outline.config import AppConfig
from bid_outline.document.locate import locate
from bid_outline.document.organization import route_organization
from bid_outline.domain import Document, DocumentElement, SourceRef
from bid_outline.model.client import FakeModelClient
from bid_outline.workflow.text_runner import TextWorkflowRunner as WorkflowRunner


def document(items):
    return Document(document_id='doc', source_hash='hash', reader_name='fixture', elements=[
        DocumentElement(element_id=f'e{i}', kind='paragraph', order=i, text=text, section_path=path,
            source_ref=SourceRef(document_id='doc', element_ids=[f'e{i}']))
        for i, (path, text) in enumerate(items)])


def test_known_pools_are_not_cross_routed_by_submission_or_neighbors():
    doc = document([
        (['投标文件格式'], '在投标文件中提供技术方案。'),
        (['技术评审标准'], '提供方案，按完整性评审。'),
        (['采购需求'], '投标文件中应提供应急方案。'),
    ])
    candidates = locate(doc)
    assert any(r.category == 'C' and 'e0' in r.record()['element_ids'] for r in candidates)
    ranges, plan = route_organization(doc, candidates)
    targets = {r['element_id']: r['target_categories'] for r in plan['routes']}
    assert targets == {'e0': ['A'], 'e1': ['B'], 'e2': ['C']}
    assert {e.element_id for r in ranges if r.category == 'A' for e in r.document.elements} == {'e0'}
    assert {e.element_id for r in ranges if r.category == 'B' for e in r.document.elements} == {'e1'}
    assert {e.element_id for r in ranges if r.category == 'C' for e in r.document.elements} == {'e2'}
    assert plan['routes'][0]['suppressed_categories']


def test_multiple_direct_pool_evidence_retains_only_relevant_categories():
    doc = document([(['投标文件格式', '技术评审标准'], '投标文件中提供技术方案。')])
    _, plan = route_organization(doc, locate(doc))
    assert plan['routes'][0]['target_categories'] == ['A', 'B']
    assert plan['routes'][0]['basis'] == 'multiple_matched_pools'
    assert [x['task_type'] for x in plan['inputs']] == ['N01-A', 'N01-B']


def test_ambiguous_neighbor_categories_are_explicit_not_implicit_abc():
    doc = document([
        ([], '投标文件格式'), ([], '待解释的条目'), ([], '技术评审标准'),
    ])
    _, plan = route_organization(doc, locate(doc))
    middle = next(x for x in plan['routes'] if x['element_id'] == 'e1')
    assert middle['basis'] == 'ambiguous_neighbor_pools'
    assert middle['target_categories'] == ['A', 'B']


def test_model_requests_receive_only_their_routed_pool(tmp_path):
    doc = document([
        (['投标文件格式'], '投标文件中提供格式方案。'),
        (['技术评审标准'], '提供评分方案，按完整性评分。'),
        (['采购需求'], '投标文件中提供应急预案。'),
    ])
    async def run():
        client = FakeModelClient({
            'N01-A': ['格式方案'], 'N01-B': ['评分方案'], 'N01-C': ['应急预案'],
            'N02-A': ['格式方案'], 'N02-B': ['评分方案'], 'N03': ['融合方案'],
            'N04': ['补充方案'], 'N05': ['# 最终目录']})
        runner = WorkflowRunner(tmp_path, AppConfig(model='fake'), client)
        try:
            result = await runner.run(doc)
        finally:
            await runner.aclose()
        assert result['status'] == 'completed'
        organizers = [r for r in client.requests if r['_task_type'].startswith('N01')]
        assert len(organizers) == 3
        for request, expected, excluded in zip(organizers, ['格式方案', '评分方案', '应急预案'],
                                             [('评分方案', '应急预案'), ('格式方案', '应急预案'), ('格式方案', '评分方案')]):
            material = request['messages'][1]['content'].split('负责正文：\n')[1]
            assert expected in material
            assert all(text not in material for text in excluded)
        assert (tmp_path / 'stages/organization_plan.json').exists()
    asyncio.run(run())
