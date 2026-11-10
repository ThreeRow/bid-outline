"""Perception decisions reuse structural splitting and preserve source order."""
import asyncio
import json

import pytest

from bid_outline.config import AppConfig
from bid_outline.document.locate import LocatedRange
from bid_outline.model.client import FakeModelClient
from bid_outline.workflow import WorkflowRunner
from bid_outline.workflow.extraction import Extraction
from test_collaborative_extraction import config, document


def run_organization(tmp_path, texts, replies, **updates):
    async def run():
        doc = document([(['技术需求书'], text) for text in texts])
        settings = AppConfig.model_validate({**config().model_dump(), **updates})
        runner = WorkflowRunner(tmp_path, settings, FakeModelClient(replies))
        runner.manifest = {}
        parent = runner.artifacts.put('document', doc.model_dump(mode='json'))
        runner.extraction = Extraction(runner, doc, parent)
        try:
            _, items = await runner.organize(LocatedRange('scope', 'C', doc, []), parent)
            return runner, items
        finally:
            await runner.aclose()
    return asyncio.run(run())


@pytest.mark.parametrize('offset,expected', [(-1, 1), (0, 1), (1, 2)])
def test_perception_boundary_counts_context_and_only_splits_above(tmp_path, offset, expected):
    # Obtain the exact material size from the real planner, including chapter context.
    sample = 'x' * 1200
    probe, _ = run_organization(tmp_path/'probe', [sample], {'N01-C': ['要求']})
    plan = json.loads(next((tmp_path/'probe/stages').glob('organization_plan_*.json')).read_text())
    runner, items = run_organization(tmp_path/'actual', [sample],
        {'N01-C': ['要求一', '要求二', '要求三'], 'E03-C': ['完整要求']},
        long_input_threshold_tokens=plan['material_tokens']-offset, split_target_tokens=850)
    tasks = [r['_task_type'] for r in runner.executor.client.requests]
    assert tasks.count('N01-C') >= expected
    if expected == 1:
        assert tasks == ['N01-C']
    else:
        assert tasks[-1] == 'E03-C'
        assert len(items) == 1 and items[0]['text'] == '完整要求'


def test_medium_input_ignores_historical_3000_cap(tmp_path):
    runner, items = run_organization(tmp_path, ['x'*5000], {'N01-C':['完整要求']})
    assert [r['_task_type'] for r in runner.executor.client.requests] == ['N01-C']
    assert items[0]['text'] == '完整要求'


def test_hard_budget_triggers_split_below_perception_threshold(tmp_path):
    runner, items = run_organization(tmp_path, ['x'*5000],
        {'N01-C':['要求']*20, 'E03-C':['完整要求']},
        context_tokens=5500, service_context_tokens=5500)
    plan = json.loads(next((tmp_path/'stages').glob('organization_plan_*.json')).read_text())
    assert plan['reason'] == 'request_budget'
    assert len(plan['chunks']) > 1
    assert all(c['request_fits'] for c in plan['chunks'])
    assert items


def test_no_extra_category_summary_for_consolidated_single_position(tmp_path):
    # helper owns its event loop; get results before entering the next one.
    runner, items = run_organization(tmp_path, ['x'*10000],
        {'N01-C':['一','二','三','四'], 'E03-C':['完整要求']})
    calls = len(runner.executor.client.requests)
    assert asyncio.run(runner.extraction.summarize('C', items)) == items
    assert len(runner.executor.client.requests) == calls


def test_oversize_summary_record_is_preserved_without_sending(tmp_path):
    runner, _ = run_organization(tmp_path, ['短材料'], {'N01-C':['x'*10000]})
    item = {'text':'x'*10000, 'artifact_id':'test-source'}
    result = asyncio.run(runner.extraction.summarize('C', [item], force=True))
    assert result == [item]
    assert runner.unprocessed[-1]['task_type'] == 'E03-C'
    assert [r['_task_type'] for r in runner.executor.client.requests] == ['N01-C']


def test_summary_uses_finite_hierarchy_and_preserves_failed_groups(tmp_path):
    runner, _ = run_organization(tmp_path, ['短'], {'N01-C':['短']})
    items = []
    for n in range(4):
        text = str(n)*4000
        aid = runner.artifacts.put('fixture', {'text':text})
        items.append({'text':text, 'artifact_id':aid})
    runner = WorkflowRunner(tmp_path, config(), FakeModelClient({
        'E03-C':['甲'*300,'乙'*300,'统一要求']}))
    runner.manifest = {}
    runner.extraction = Extraction(runner, document([]), '')
    async def summarize():
        try:
            return await runner.extraction.summarize('C', items)
        finally:
            await runner.aclose()
    result = asyncio.run(summarize())
    assert result[0]['text'] == '统一要求'
    requests = [r for r in runner.executor.client.requests if r['_task_type']=='E03-C']
    assert len(requests) == 3
    assert '0'*4000 in requests[0]['messages'][-1]['content']
    assert '3'*4000 in requests[1]['messages'][-1]['content']
    assert requests[-1]['messages'][-1]['content'].index('甲'*300) < requests[-1]['messages'][-1]['content'].index('乙'*300)


def test_partial_failure_retains_other_outputs_and_source_failure(tmp_path):
    from bid_outline.model.client import ModelClientError
    runner, items = run_organization(tmp_path, ['x'*10000],
        {'N01-C':[ModelClientError('failed block'),'保留第二部分','保留第三部分','保留第四部分'],
         'E03-C':['成功部分完整整理']}, transport_retries=0)
    assert items and runner.unprocessed
    assert any(p['task_type']=='N01-C' and p['parents'] for p in runner.unprocessed)
    assert items[0]['text'] == '成功部分完整整理'


def test_large_single_table_cell_reuses_expanded_text_slices(tmp_path):
    async def run():
        doc = document([(['技术需求书'], '必须满足。'*1200)])
        doc.elements[0].kind = 'table'
        runner = WorkflowRunner(tmp_path, config(), FakeModelClient({
            'N01-C':['要求']*20, 'E03-C':['完整要求']}))
        runner.manifest = {}
        parent = runner.artifacts.put('document', doc.model_dump(mode='json'))
        runner.extraction = Extraction(runner, doc, parent)
        try:
            _, items = await runner.organize(LocatedRange('table', 'C', doc, []), parent)
            assert items[0]['text'] == '完整要求'
            calls = [x for x in runner.stages if x['task_type']=='N01-C']
            chunks = [runner.artifacts.get(x['parents'][0]) for x in calls]
            assert len(chunks)>1
            assert ''.join(c['text'] for c in chunks) == doc.elements[0].text
            assert all(c['owned_refs'][0]['reader_ref']['text_slice_basis']=='expanded_table_text' for c in chunks)
        finally:
            await runner.aclose()
    asyncio.run(run())


def test_parallel_completion_is_merged_in_source_order(tmp_path):
    from bid_outline.model.client import ModelResponse
    class DelayedClient(FakeModelClient):
        async def generate(self, request):
            self.requests.append(request)
            body = request['messages'][-1]['content'].split('\n\n---\n\n')[1]
            if request['_task_type']=='E03-C':
                assert body.index('第一部分') < body.index('第二部分')
                return ModelResponse('完整要求')
            await asyncio.sleep(0.02 if 'first' in body else 0)
            return ModelResponse('第一部分' if 'first' in body else '第二部分')
    async def run():
        doc = document([(['技术需求书'],'first'+'x'*5000),(['技术需求书'],'second'+'y'*5000)])
        runner = WorkflowRunner(tmp_path, config(), DelayedClient([]))
        runner.manifest = {}
        parent = runner.artifacts.put('document', doc.model_dump(mode='json'))
        runner.extraction = Extraction(runner, doc, parent)
        try:
            _, items = await runner.organize(LocatedRange('ordered', 'C', doc, []), parent)
            assert items[0]['text']=='完整要求'
        finally:
            await runner.aclose()
    asyncio.run(run())


def test_failed_summary_keeps_full_failed_group(tmp_path):
    from bid_outline.model.client import ModelClientError
    async def run():
        runner = WorkflowRunner(tmp_path, config(transport_retries=0), FakeModelClient({
            'E03-C':[ModelClientError('summary failed'), '第二分册整理']}))
        runner.manifest = {}
        runner.extraction = Extraction(runner, document([]), '')
        items = [{'text':str(n)*4000, 'artifact_id':runner.artifacts.put('fixture', {'n':n})} for n in range(4)]
        try:
            result = await runner.extraction.summarize('C', items)
            assert [x['text'] for x in result] == ['0'*4000, '1'*4000, '第二分册整理']
            assert runner.unprocessed[-1]['task_type']=='E03-C'
        finally:
            await runner.aclose()
    asyncio.run(run())


def test_long_anchor_requests_use_actual_e02_budget(tmp_path):
    runner, items = run_organization(tmp_path, ['x'*10000], {'E02':['无']*20}, extraction_mode='anchors')
    plan = json.loads(next((tmp_path/'stages').glob('organization_plan_*.json')).read_text())
    assert plan['task']=='E02' and len(plan['chunks'])>1
    assert not items
    assert all(r['_task_type']=='E02' for r in runner.executor.client.requests)
    assert all(c['request_fits'] for c in plan['chunks'])


def test_long_a_is_merged_once_and_never_regenerated(tmp_path):
    from test_collaborative_extraction import execute
    async def run():
        doc = document([(['投标文件格式'], '技术方案\n'+'x'*10000)])
        result, client = await execute(tmp_path, doc, {'N01-A':['技术方案']*20,
            'E03-A':['技术方案\n    实施计划'], 'N05':['技术方案\n    实施计划']}, directory_assistance=False)
        tasks = [r['_task_type'] for r in client.requests]
        assert tasks.count('N01-A')>1 and tasks.count('E03-A')==1
        assert 'N02-A' not in tasks
        assert result['directory_text']=='技术方案\n    实施计划'
    asyncio.run(run())


def test_shared_conditions_larger_than_split_target_still_allow_processing(tmp_path):
    async def run():
        doc = document([(['技术需求书'], 'x'*5000)])
        doc.elements[0].metadata['shared_context'] = '共同适用条件'+'y'*5000
        runner = WorkflowRunner(tmp_path, config(), FakeModelClient({
            'N01-C':['第一部分','第二部分'], 'E03-C':['完整条件与要求']}))
        runner.manifest = {}
        parent = runner.artifacts.put('document', doc.model_dump(mode='json'))
        runner.extraction = Extraction(runner, doc, parent)
        try:
            _, items = await runner.organize(LocatedRange('shared', 'C', doc, []), parent)
            calls = [r for r in runner.executor.client.requests if r['_task_type']=='N01-C']
            assert len(calls)==2 and not runner.unprocessed
            assert all('y'*5000 in r['messages'][-1]['content'] for r in calls)
            assert items[0]['text']=='完整条件与要求'
            plan = json.loads(next((tmp_path/'stages').glob('organization_plan_*.json')).read_text())
            assert all(4000 < c['material_tokens'] <= 9000 for c in plan['chunks'])
        finally:
            await runner.aclose()
    asyncio.run(run())


def test_real_comparison_projection_preserves_conditions_and_long_path(tmp_path, monkeypatch):
    """A projected excerpt must not become a short input by losing its context."""
    import importlib.util
    from pathlib import Path
    from bid_outline.domain import Chunk
    import bid_outline.document.token_chunks as chunk_module
    spec = importlib.util.spec_from_file_location('evaluate_long_inputs',
        Path(__file__).resolve().parents[2]/'scripts/evaluate_long_inputs.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    doc = document([(['技术需求书'],'x'*8500),(['技术需求书'],'必要条件'+'y'*900)])
    original = chunk_module.token_chunks
    def chunks(document, **kwargs):
        if document.elements[0].element_id=='e0':
            return [Chunk(chunk_id='original',text=doc.elements[0].text,
                owned_refs=[doc.elements[0].source_ref],section_context=['技术需求书'],
                context_notes=[{'text':doc.elements[1].text,
                                'source_ref':doc.elements[1].source_ref.model_dump(mode='json')}])]
        return original(document,**kwargs)
    monkeypatch.setattr(module,'token_chunks',chunks)
    runner = WorkflowRunner(tmp_path,config(),FakeModelClient([]))
    try:
        located, material = module.long_excerpt(doc,[LocatedRange('original','C',doc,[])],runner)
        assert material.body==doc.elements[0].text
        assert doc.elements[1].text in material.supplement
        assert runner.material_tokens(material)>9000
        assert all(doc.elements[1].text in e.metadata['shared_context'] for e in located.document.elements)
    finally:
        asyncio.run(runner.aclose())
