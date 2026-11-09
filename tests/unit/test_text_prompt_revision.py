"""Review the actual model query as well as the template-to-workflow contract."""
import asyncio
import json
from pathlib import Path

import httpx
import pytest

from bid_outline.config import AppConfig
from bid_outline.model.budget import TokenBudget
from bid_outline.model.executor import ModelExecutor
from bid_outline.model.task_specs import render_task
from bid_outline.model.text_tasks import TASKS, TextMaterial, input_data
from bid_outline.model.train_client import TrainModelClient, train_query
from bid_outline.storage import ArtifactStore, RunRepository


@pytest.mark.parametrize('task', TASKS)
def test_prompt_is_self_contained_and_raw_material_is_unchanged(task):
    body = '原文包含 C:\\目录[1]、无负偏离\n---\n以及{context}字样。'
    context = '截取内容所属章节：\n第四章 / 技术需求'
    messages = render_task(task, input_data(TextMaterial(body, context)))
    assert len(messages) == 1 and messages[0]['role'] == 'user'
    query = train_query(messages)
    assert query == messages[0]['content']
    instructions, data = query.split('\n\n---\n\n', 1)
    assert instructions.startswith('你是招投标领域的专家。')
    assert context in instructions and data == body
    assert '{context}' not in instructions
    assert '系统说明' not in instructions and '用户消息' not in instructions
    for phrase in ('不生成目录', '不要设计目录', '不输出Python', '不编写方案正文', '不能编造通用目录'):
        assert phrase not in instructions


def test_task_definitions_precede_category_aliases_and_example_topics_are_absent():
    for task in ('E01', 'E02'):
        text = render_task(task, {'material': '输入'})[0]['content']
        assert text.index('格式') < text.index('A类')
        assert text.index('评审') < text.index('B类')
        assert text.index('技术需求') < text.index('C类')
    for task in ('N01-A', 'N01-B', 'N01-C', 'E00'):
        text = render_task(task, {'material': '输入'})[0]['content']
        assert all(alias not in text for alias in ('A类', 'B类', 'C类'))
    text = render_task('N02-B', {'material': '施工部署、质量保证'})[0]['content']
    assert all(topic not in text for topic in ('范本适配', '工具提供', '运维更新', '主要技术指标'))


@pytest.mark.parametrize('batch_size', [1, 2])
def test_train_wire_budget_and_persisted_query_are_identical(tmp_path, batch_size):
    async def run():
        cfg = AppConfig(provider='train', base_url='http://fake', model='fake',
            thinking_default=False, train_batch_size=batch_size,
            train_poll_interval_seconds=.001, train_poll_timeout_seconds=1)
        submitted = []
        def server(request):
            if request.url.path.endswith('chat_train'):
                payload = json.loads(request.content)
                submitted.append(payload)
                return httpx.Response(200, json={'status': 1, 'responseId': 'job'})
            ids = [q['id'] for payload in submitted for q in payload['query_list']]
            return httpx.Response(200, json={'status': 1, 'msg': '完成', 'data': [
                {'id': ident, 'answer': '技术内容', 'is_success': True} for ident in ids]})
        client = TrainModelClient(cfg)
        await client.http.aclose()
        client.http = httpx.AsyncClient(transport=httpx.MockTransport(server))
        repo = RunRepository(tmp_path)
        executor = ModelExecutor(cfg, client, repo, ArtifactStore(tmp_path))
        seen = []
        class CapturingTokenizer:
            def apply_chat_template(self, messages, **kwargs):
                seen.append(messages)
                return list(messages[0]['content'])
        budget = TokenBudget(cfg)
        budget.tokenizer = CapturingTokenizer()
        inputs = input_data(TextMaterial('必须响应参数。', '截取内容所属章节：采购需求'))
        expected = render_task('N01-C', inputs)[0]['content']
        assert budget.count(render_task('N01-C', inputs), task_type='N01-C') == len(expected)
        assert seen[-1] == [{'role': 'user', 'content': expected}]
        try:
            await executor.execute('N01-C', inputs, task_id='one')
            assert submitted[0]['query_list'][0]['query'] == expected
            attempt = repo.list_attempts()[0]
            request = json.loads((tmp_path / 'calls' / attempt['call_id'] / 'request.json').read_text())
            assert request['messages'] == [{'role': 'user', 'content': expected}]
        finally:
            await executor.aclose()
            repo.close()
    asyncio.run(run())


@pytest.mark.parametrize('answer,empty', [('无', True), (' \n无\n ', True),
    ('无负偏离要求', False), ('说明：无', False), ('无。', False), ('未发现', False)])
def test_only_exact_none_is_no_content(tmp_path, answer, empty):
    from bid_outline.model.client import FakeModelClient
    async def run():
        cfg = AppConfig(model='fake')
        repo = RunRepository(tmp_path)
        executor = ModelExecutor(cfg, FakeModelClient([answer]), repo, ArtifactStore(tmp_path))
        try:
            result = await executor.execute('N01-C', {'material': '原文'}, task_id='one')
            assert result['no_content'] is empty
            assert result['text'] == answer
        finally:
            await executor.aclose()
            repo.close()
    asyncio.run(run())
