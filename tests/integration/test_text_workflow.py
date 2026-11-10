import asyncio
import json

import pytest

from bid_outline.config import AppConfig
from bid_outline.document.locate import locate
from bid_outline.domain import Document, DocumentElement, SourceRef
from bid_outline.export import export_result
from bid_outline.model.client import FakeModelClient
from bid_outline.workflow.text_runner import TextWorkflowRunner as WorkflowRunner


def document(items):
    return Document(document_id='doc', source_hash='hash', reader_name='fixture', elements=[
        DocumentElement(element_id=f'e{i}', kind='paragraph', order=i, text=text, section_path=path,
            source_ref=SourceRef(document_id='doc', element_ids=[f'e{i}']))
        for i, (path, text) in enumerate(items)])


def config(**kwargs):
    return AppConfig(model='fake', context_tokens=30000, service_context_tokens=30000,
                     max_tokens=1000, text_batch_tokens=3000, **kwargs)


def test_location_selects_sections_and_submission_under_business_contract():
    doc = document([
        (['投标文件格式'], 'C1 技术条款总体响应表\nC2 总体技术方案\nC8 其他'),
        (['评定标办法', '技术标定性评审表'], '总体技术方案，完整性、先进性。'),
        (['采购需求', '商务要求'], '应在响应文件中提供质保期以后的售后服务方案承诺书。'),
        (['合同'], '须随投标文件提交培训课程计划。'),
        (['项目业务规则'], '系统需要识别评审标准设置不合理的情况。'),
    ])
    ranges = locate(doc)
    assert {r.category for r in ranges} == set('ABC')
    c = {eid for r in ranges if r.category == 'C' for eid in r.record()['element_ids']}
    assert {'e2', 'e3'} <= c
    assert all(r.record()['source_refs'] for r in ranges)


def test_ab_text_pipeline_resume_and_read_only_export(tmp_path):
    doc = document([
        (['投标文件格式'], 'C1 技术条款总体响应表\nC2 总体技术方案\nC3 范本适配方案\nC8 其他'),
        (['技术标定性评审表'], '总体技术方案：范本适配、工具提供；完整性、思路清晰度、先进性。'),
    ])
    replies = {
        'N01-A': ['技术条款总体响应表；总体技术方案；范本适配方案；其他。'],
        'N01-B': ['总体技术方案：范本适配、工具提供；完整性、思路清晰度、先进性。'],
        'N02-A': ['技术条款总体响应表\n总体技术方案\n范本适配方案\n其他'],
        'N02-B': ['总体技术方案\n  范本适配\n  工具提供\n  完整性\n  思路清晰度\n  先进性'],
        'N03': ['分析：对应标题合并。\n总体技术方案\n  完整性\n  先进性'],
        'N05': ['# 技术条款总体响应表\n# 总体技术方案\n## 完整性\n## 先进性\n# 范本适配方案\n# 其他'],
    }
    async def run():
        client = FakeModelClient(replies)
        runner = WorkflowRunner(tmp_path / 'run', config(), client)
        try:
            result = await runner.run(doc)
        finally:
            await runner.aclose()
        assert result['status'] == 'completed'
        assert [r['_task_type'] for r in client.requests] == ['N01-A', 'N01-B', 'N02-A', 'N02-B', 'N03', 'N05']
        assert '分析：对应标题合并。' in client.requests[-1]['messages'][-1]['content']
        assert '允许引用ID' not in client.requests[-1]['messages'][-1]['content']
        adopted = [s['artifact_id'] for s in result['stages']]
        resumed_client = FakeModelClient([])
        resumed = WorkflowRunner(tmp_path / 'run', config(), resumed_client)
        try:
            second = await resumed.run(resume=True)
        finally:
            await resumed.aclose()
        assert not resumed_client.requests
        assert second['directory_text'] == result['directory_text']
        assert adopted == [s['artifact_id'] for s in second['stages']]
        files = export_result(second, tmp_path / 'export')
        assert set(files) == {'backbone.md', 'unresolved.md', 'provenance.json'}
        assert '先进性' in (tmp_path / 'export/backbone.md').read_text()
        assert json.loads((tmp_path / 'run/manifest.json').read_text())['flow_version'] == 'text-directory-1'
    asyncio.run(run())


@pytest.mark.parametrize('response', ['', {'content': '半截', 'finish_reason': 'length'}])
def test_failed_text_is_not_absence_and_preserves_scope(tmp_path, response):
    async def run():
        runner = WorkflowRunner(tmp_path, config(), FakeModelClient({'N01-A': [response]}))
        try:
            result = await runner.run(document([(['投标文件格式'], '技术方案')]))
        finally:
            await runner.aclose()
        assert result['status'] == 'incomplete'
        assert result['unprocessed_ranges']
        assert result['directory_text'] == ''
    asyncio.run(run())


def test_final_budget_failure_keeps_full_draft(tmp_path):
    draft = '# 方案\n' + '完整目录内容' * 2500
    async def run():
        runner = WorkflowRunner(tmp_path, config(), FakeModelClient({
            'N01-A': ['总体方案'], 'N02-A': [draft]}))
        try:
            result = await runner.run(document([(['投标文件格式'], '总体方案')]))
        finally:
            await runner.aclose()
        assert result['status'] == 'incomplete'
        assert result['draft_text'] == draft
        assert result['directory_text'] == ''
        assert result['unprocessed_ranges'][-1]['task_type'] == 'N05'
    asyncio.run(run())


def test_no_candidates_makes_no_model_calls(tmp_path):
    async def run():
        client = FakeModelClient([])
        runner = WorkflowRunner(tmp_path, config(), client)
        try:
            result = await runner.run(document([(['公告'], '报名地点及联系方式')]))
        finally:
            await runner.aclose()
        assert not client.requests
        assert result['status'] == 'completed_with_issues'
        assert result['directory_text'] == ''
    asyncio.run(run())


def test_c_only_generates_directory(tmp_path):
    async def run():
        client = FakeModelClient({'N01-C': ['须提供应急方案'], 'N04': ['应急方案'], 'N05': ['# 应急方案']})
        runner = WorkflowRunner(tmp_path / 'c', config(), client)
        try:
            result = await runner.run(document([(['采购需求'], '须在投标文件中提供应急方案。')]))
        finally:
            await runner.aclose()
        assert result['status'] == 'completed'
        assert [r['_task_type'] for r in client.requests] == ['N01-C', 'N04', 'N05']
        assert result['directory_text'] == '# 应急方案'
        assert result['stages'][-1]['parents'] == [result['stages'][-2]['artifact_id']]
    asyncio.run(run())


def test_c_table_localization_retains_only_candidate_rows_and_context():
    doc = document([(['采购需求'], '整张需求表')])
    element = doc.elements[0]
    element.metadata['table_rows'] = [
        {'row': i, 'text': '须在投标文件中提供组网说明' if i == 5 else f'普通参数{i}',
         'cell_ids': [f'c{i}'], 'header': i == 0} for i in range(12)]
    element.text = '\n'.join(r['text'] for r in element.metadata['table_rows'])
    c = next(r for r in locate(doc) if r.category == 'C')
    assert c.document.elements[0].source_ref.reader_ref['candidate_rows'] == [0, 4, 5, 6]
    assert '普通参数11' not in c.document.elements[0].text
    assert '组网说明' in c.document.elements[0].text


def test_c_failure_retains_ab_directory_and_marks_incomplete(tmp_path):
    async def run():
        client = FakeModelClient({'N01-A': ['技术方案'], 'N01-C': ['补充应急方案'],
            'N02-A': ['技术方案'], 'N04': [''], 'N05': ['# 技术方案']})
        runner = WorkflowRunner(tmp_path, config(), client)
        try:
            result = await runner.run(document([
                (['投标文件格式'], '技术方案'),
                (['采购需求'], '投标文件中须提供应急方案')]))
        finally:
            await runner.aclose()
        assert result['status'] == 'incomplete'
        assert result['directory_text'] == '# 技术方案'
        assert result['unprocessed_ranges'][0]['task_type'] == 'N04'
    asyncio.run(run())


def test_organization_known_truncation_splits_owned_source(tmp_path):
    async def run():
        client = FakeModelClient({'N01-A': [
            {'content': 'partial', 'finish_reason': 'length'}, '第一段整理', '第二段整理'],
            'N02-A': ['方案一', '方案二'], 'N03': ['方案一\n方案二'], 'N05': ['# 方案一\n# 方案二']})
        runner = WorkflowRunner(tmp_path, config(), client)
        doc = document([(['投标文件格式'], '方案说明。' * 100)])
        try:
            result = await runner.run(doc)
        finally:
            await runner.aclose()
        assert result['status'] == 'completed'
        calls = [r for r in client.requests if r['_task_type'] == 'N01-A']
        assert len(calls) == 3
        assert sum(len(r['messages'][-1]['content'].split('本次资料：\n', 1)[1].split('\n\n补充上下文：\n',1)[0]) for r in calls[1:]) == len(doc.elements[0].text)
    asyncio.run(run())


def test_generation_batching_preserves_text_and_resume_rejects_prompt_change(tmp_path, monkeypatch):
    async def run():
        runner = WorkflowRunner(tmp_path, config(), FakeModelClient([]))
        text = ('标题\n\n具体评审内容\n\n' * 150)
        batches = runner.generation_batches('N02-B', text)
        assert len(batches) > 1
        assert ''.join(batches) == text
        try:
            await runner.run(document([(['公告'], '联系方式')]))
        finally:
            await runner.aclose()
        resumed = WorkflowRunner(tmp_path, config(), FakeModelClient([]))
        original = resumed.messages
        monkeypatch.setattr(resumed, 'messages', lambda task, body: original(task, body) + [
            {'role': 'user', 'content': '修改后的提示'}])
        try:
            with pytest.raises(ValueError, match='prompts differ'):
                await resumed.run(resume=True)
        finally:
            await resumed.aclose()
    asyncio.run(run())


def test_long_single_table_cell_can_split_without_losing_text_or_claiming_cell_offsets():
    from bid_outline.document.text_chunks import expandable_tables
    from bid_outline.document.token_chunks import token_chunks
    doc = document([(['技术标定性评审表'], '评分项目。\n完整性和先进性。\n' * 80)])
    element = doc.elements[0]
    element.kind = 'table'
    element.source_ref.precision = 'table'
    element.source_ref.table_id = 'table1'
    element.source_ref.cell_ids = ['cell1']
    element.metadata['table_rows'] = [{'row': 0, 'text': element.text, 'cell_ids': ['cell1']}]
    expanded = expandable_tables(doc, len, 150)
    chunks = token_chunks(expanded, allowance=lambda c: 150, text_tokens=len, fits=lambda c: len(c.text) <= 150)
    assert len(chunks) > 1
    assert ''.join(c.text for c in chunks) == element.text
    assert element.kind == 'table'  # Frozen reader objects are unchanged.
    for chunk in chunks:
        ref = chunk.owned_refs[0]
        assert ref.table_id == 'table1'
        assert ref.reader_ref['text_slice_basis'] == 'expanded_table_text'
        assert ref.start is None and ref.end is None
        assert ref.reader_ref['element_text_range']['end'] > ref.reader_ref['element_text_range']['start']


def test_cli_uses_text_flow_and_export_is_read_only(tmp_path, monkeypatch, capsys):
    from bid_outline import cli
    doc = document([(['投标文件格式'], '总体技术方案')])
    monkeypatch.setattr(cli, 'read_document', lambda *args, **kwargs: doc)
    fixture = tmp_path / 'responses.json'
    fixture.write_text(json.dumps({'E01': ['A：投标文件格式'], 'N01-A': ['总体技术方案'],
        'E03-A': ['总体技术方案'], 'N05': ['# 总体技术方案']}))
    run_dir = tmp_path / 'run'
    assert cli.main(['run', '--input', str(tmp_path / 'input'), '--run', str(run_dir),
                     '--fake-responses', str(fixture)]) == 0
    assert json.loads(capsys.readouterr().out)['simulation'] is True
    assert cli.main(['export', '--run', str(run_dir)]) == 0
    capsys.readouterr()
    manifest, tasks, attempts = cli.read_state(run_dir)
    assert manifest['flow_version'] == 'collaborative-directory-5'
    assert {t['task_type'] for t in tasks} == {'E01', 'N01-A', 'N05'}
    assert len(attempts) == 3
    assert '# 总体技术方案' in (run_dir / 'exports/backbone.md').read_text()
