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
@pytest.mark.parametrize('provider', ['train','openai'])
def test_prompt_is_self_contained_and_raw_material_is_unchanged(task, provider):
    body = '原文包含 C:\\目录[1]、无负偏离\n---\n以及{context}字样。'
    context = '截取内容所属章节：\n第四章 / 技术需求'
    supplement = '附表中的共同条件：所有指标适用于同一包别。'
    messages = render_task(task, input_data(TextMaterial(body, context, supplement)), provider=provider)
    if provider=='train':
        assert len(messages)==1 and messages[0]['role']=='user'
        query = train_query(messages)
        assert query==messages[0]['content'] and query.startswith('你是招投标领域的专家。')
    else:
        assert [m['role'] for m in messages]==['system','user']
        assert messages[0]['content']=='你是招投标领域的专家。'
        query = messages[-1]['content']
    instructions, data = query.split('\n\n---\n\n',1)
    assert context not in instructions and supplement not in instructions
    assert context in data and body in data and supplement in data
    assert '{context}' not in instructions
    assert '系统说明' not in instructions and '用户消息' not in instructions
    for phrase in ('不生成目录', '不要设计目录', '不输出Python', '不编写方案正文', '不能编造通用目录'):
        assert phrase not in instructions


def test_task_definitions_precede_category_aliases_and_example_topics_are_absent():
    for task in ('E01', 'E02'):
        text = render_task(task, {'material': '输入'})[-1]['content']
        assert text.index('格式') < text.index('A类')
        assert text.index('评审') < text.index('B类')
        assert text.index('技术需求') < text.index('C类')
    for task in ('N01-A', 'N01-B', 'N01-C', 'E00'):
        text = render_task(task, {'material': '输入'})[-1]['content']
        assert all(alias not in text for alias in ('A类', 'B类', 'C类'))
    text = render_task('N02-B', {'material': '施工部署、质量保证'})[-1]['content']
    assert all(topic not in text for topic in ('范本适配', '工具提供', '运维更新', '主要技术指标'))


@pytest.mark.parametrize('batch_size', [1, 2])
@pytest.mark.parametrize('service_window', [20480, 40000])
def test_train_wire_budget_and_persisted_query_are_identical(tmp_path, batch_size, service_window):
    async def run():
        cfg = AppConfig(provider='train', base_url='http://fake', model='fake',
            thinking_default=False, train_batch_size=batch_size,
            context_tokens=16000, service_context_tokens=service_window, max_tokens=4096,
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
        expected = render_task('N01-C', inputs, provider='train')[0]['content']
        assert budget.count(render_task('N01-C', inputs, provider='train'), task_type='N01-C') == len(expected)
        assert seen[-1] == [{'role': 'user', 'content': expected}]
        try:
            await asyncio.gather(*(executor.execute('N01-C', inputs, task_id=f'one-{i}') for i in range(batch_size)))
            assert len(submitted)==1 and len(submitted[0]['query_list'])==batch_size
            assert all(q['query']==expected for q in submitted[0]['query_list'])
            assert all(q['max_tokens']==service_window for q in submitted[0]['query_list'])
            for attempt in repo.list_attempts():
                request = json.loads((tmp_path/'calls'/attempt['call_id']/'request.json').read_text())
                assert request['messages']==[{'role':'user','content':expected}]
                assert request['max_tokens']==4096  # Logical generation reserve only.
                meta=json.loads((tmp_path/'calls'/attempt['call_id']/'meta.json').read_text())
                assert meta['budget']['generation_tokens']==4096
                assert meta['budget']['context_tokens']==16000
                assert meta['budget']['service_context_tokens']==service_window
                wire=json.loads((tmp_path/'calls'/attempt['call_id']/'request.http.json').read_text())
                member=wire.get('logical_member',wire)
                assert member['query_list'][0]['max_tokens']==service_window
                assert (tmp_path/'calls'/attempt['call_id']/'input.md').read_text()==expected
            assert not expected.startswith('user\n')
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


def test_openai_wire_keeps_system_and_user_roles(tmp_path):
    from bid_outline.model.client import VllmChatClient
    async def run():
        cfg = AppConfig(provider='openai', base_url='http://fake/v1', model='served-model', thinking_default=False)
        payloads = []
        def server(request):
            payloads.append(json.loads(request.content))
            return httpx.Response(200,json={'choices':[{'message':{'content':'整理结果'},'finish_reason':'stop'}]})
        client = VllmChatClient(cfg)
        await client.http.aclose()
        client.http = httpx.AsyncClient(transport=httpx.MockTransport(server))
        repo = RunRepository(tmp_path)
        executor = ModelExecutor(cfg, client, repo, ArtifactStore(tmp_path))
        inputs = input_data(TextMaterial('★设备指标必须满足。','截取内容所属章节：技术条款','全部设备适用。'))
        expected = render_task('N01-C', inputs, provider='openai')
        class Tokenizer:
            def apply_chat_template(self, messages, **kwargs):
                assert [m['role'] for m in messages]==['system','user']
                assert messages==expected
                return list(''.join(m['content'] for m in messages))
        executor.budget.tokenizer = Tokenizer()
        try:
            await executor.execute('N01-C',inputs,task_id='openai-role-check')
            assert payloads[0]['messages']==expected
            assert payloads[0]['max_tokens']==cfg.max_tokens==8192
            audit = tmp_path/'calls'/repo.list_attempts()[0]['call_id']/'input.md'
            assert audit.read_text().startswith('## system\n\n你是招投标领域的专家。')
            assert '## user\n\n' in audit.read_text()
            instructions, data = expected[-1]['content'].split('\n\n---\n\n',1)
            assert '★设备指标' not in instructions and '全部设备适用' not in instructions
            assert '★设备指标必须满足。' in data and '全部设备适用。' in data
        finally:
            await executor.aclose()
            repo.close()
    asyncio.run(run())


def test_feedback_and_source_are_data_after_complete_instructions():
    from bid_outline.model.text_tasks import with_feedback
    material = with_feedback(TextMaterial('原文{context}原样保留','真实章节', '原表头'),
                             '起始句未唯一匹配：源文短句')
    query = render_task('E02',input_data(material),provider='train')[0]['content']
    instructions, data = query.split('\n\n---\n\n',1)
    assert '请结合所附上次处理反馈' in instructions
    assert all(x not in instructions for x in ['真实章节','原表头','源文短句'])
    assert all(x in data for x in ['真实章节','原表头','源文短句','原文{context}原样保留'])


def test_implicit_table_context_is_not_a_common_leader(tmp_path):
    from bid_outline.domain import Chunk, Document, DocumentElement, SourceRef
    from bid_outline.workflow import WorkflowRunner
    from bid_outline.model.client import FakeModelClient
    doc = Document(document_id='doc', source_hash='hash', reader_name='fixture', elements=[
        DocumentElement(element_id='e0',kind='paragraph',order=0,text='★响应时间≤2秒。',
            section_path=['采购需求'], source_ref=SourceRef(document_id='doc',element_ids=['e0']))])
    chunk = Chunk(chunk_id='scope',text=doc.elements[0].text,
        owned_refs=[doc.elements[0].source_ref], section_context=['采购需求'],
        context_notes=[{'text':'第8行，第4列：\n整段表格内容\n第41行，第4列：'},
                       {'text':'服务期内均应满足：'},
                       {'text':'仅适用于实施阶段：\n所有站点应满足相同条件。'}])
    runner = WorkflowRunner(tmp_path,AppConfig(model='fake'),FakeModelClient([]))
    try:
        material = runner.organization_material('C',doc,chunk)
        query = runner.messages('N01-C',material)[-1]['content']
        instructions, data = query.split('\n\n---\n\n',1)
        assert material.context=='截取内容所属章节：\n采购需求'
        assert '本次寻找的内容' not in query
        assert '第41行' not in query and '整段表格内容' not in query
        assert '服务期内均应满足：' in data and '服务期内均应满足：' not in instructions
        assert '仅适用于实施阶段：\n所有站点应满足相同条件。' in data
        assert chunk.context_notes[0]['text'].startswith('第8行')  # Raw source audit remains intact.
    finally:
        asyncio.run(runner.aclose())
