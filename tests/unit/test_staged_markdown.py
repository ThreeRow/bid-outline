import asyncio
import json

import httpx
import pytest

from bid_outline.config import AppConfig, load_config
from bid_outline.domain import OutlineNode, OutlineConstraint, RequirementGroup
from bid_outline.model.budget import TokenBudget
from bid_outline.model.client import FakeModelClient
from bid_outline.model.executor import ModelExecutor
from bid_outline.model.markdown_directory import normalize
from bid_outline.model.parsers import ParseError, parse_output
from bid_outline.model.references import prepare_call_inputs
from bid_outline.model.task_specs import render_task
from bid_outline.model.train_client import TrainModelClient
from bid_outline.storage import ArtifactStore, RunRepository
from bid_outline.workflow.common import present
from bid_outline.workflow.outline import make_version, nodes_from_records


REQ = 'R_' + '1'*20
ROOT = 'N_' + '2'*20
CHILD = 'N_' + '3'*20
requirements = [{'requirement_id': REQ, 'topic': '部署', 'content': '提供部署与培训方案',
                 'categories': ['B'], 'material_refs': []}]


def config(**kw):
    return AppConfig(provider='train', base_url='http://train.test', model='fake',
                     prompt_protocol='compact-v1', directory_protocol='markdown-v1',
                     thinking_default=False, thinking_by_task={'P08': True, 'P11': True, 'P16': True}, **kw)


def inputs(task='P08', existing=False, depth=1):
    root = {'node_id': ROOT, 'parent_id': None, 'title': '技术方案', 'order': 1,
            'basis_requirement_ids': [REQ], 'origin_kind': '有依据归纳'}
    child = {'node_id': CHILD, 'parent_id': ROOT, 'title': '部署', 'order': 1,
             'basis_requirement_ids': [REQ], 'origin_kind': '原文明示'}
    if task == 'P08':
        material = present(本批要求=requirements, 当前骨架={'nodes': [root, child]} if existing else None)
        scope = '仅提供材料'
    else:
        material = present(本章=root, 绝对层级=depth, 保留结构=[child] if existing else [], 要求=requirements)
        scope = ROOT
    return prepare_call_inputs({'material': material, 'scope': scope, 'short_ids': True,
                                'allowed_ids': [REQ, ROOT, CHILD] if task != 'P08' or existing else [REQ]})


def parse(task, text, data=None):
    data = data or inputs(task)
    # The executor performs alias-aware strict validation and restoration.
    repo = object.__new__(ModelExecutor)
    repo.config = config()
    from bid_outline.model.client import ModelResponse
    return repo._parse(task, ModelResponse(text), data)


def test_task_thinking_precedence_and_configuration_validation():
    c = config()
    assert c.thinking_for('P03') is False and c.thinking_for('P08') is True
    assert c.thinking_for('P06') is False
    assert AppConfig(train_enable_thinking=False, provider='train').thinking_for('P08') is False
    assert AppConfig(extra_body={'chat_template_kwargs': {'enable_thinking': False}}).thinking_for('P08') is False
    with pytest.raises(ValueError, match='unknown thinking'):
        AppConfig(thinking_by_task={'P99': True})
    with pytest.raises(ValueError):
        AppConfig(thinking_by_task={'P03': 'off'})


def test_budget_and_train_wire_use_same_task_switch():
    async def run():
        c = config()
        client = TrainModelClient(c)
        budget = TokenBudget(c)
        class Tokenizer:
            def apply_chat_template(self, messages, **kw):
                return [1] * (10 if kw['enable_thinking'] else 12)
        budget.tokenizer = Tokenizer()
        for task, enabled, count in [('P03', False, 12), ('P08', True, 10), ('P03', False, 12)]:
            request = {'_task_type': task, 'messages': [{'role': 'user', 'content': '正文'}],
                       'temperature': 0, 'max_tokens': 8192}
            wire = client.wire_request(request)['query_list'][0]
            report = budget.check(request['messages'], 8192, task_type=task)
            assert wire['enable_thinking'] == int(enabled)
            assert report['input_tokens'] == count and report['enable_thinking'] is enabled
        await client.aclose()
    asyncio.run(run())


def test_markdown_template_has_no_old_output_contract():
    for task in ('P08', 'P11', 'P12', 'P16'):
        messages = render_task(task, inputs(task), protocol='compact-v1', directory_protocol='markdown-v1')
        text = messages[1]['content']
        assert '[[节点开始]]' not in text and '[[结果开始]]' not in text
        assert '同级顺序：正整数' not in text and '新建节点只能' not in text
        assert 'Markdown标题' in text and '依据：REQ01' in text
    normal = render_task('P08', inputs(), protocol='compact-v1')[1]['content']
    assert '[[节点开始]]' in normal


def test_new_headings_get_program_ids_edges_and_real_requirement_support():
    p = parse('P08', '# 实施方案\n依据：REQ01\n## 部署\n依据：REQ01\n## 培训\n依据：REQ01')
    records = p['records']['节点']
    assert [r['节点'] for r in records] == ['N01', 'N02', 'N03']
    assert [r['父节点'] for r in records] == ['ROOT', 'N01', 'N01']
    assert [r['同级顺序'] for r in records] == ['1', '1', '2']
    assert all(r['依据'] == REQ for r in records)
    nodes = nodes_from_records(records, [], {REQ}, 'markdown-test')
    assert len(make_version(nodes, None, 'test').nodes) == 3


def test_existing_nodes_restore_ids_by_title_and_parent_without_model_ids():
    p = parse('P08', '# 技术方案\n## 部署\n## 培训\n依据：REQ01', inputs(existing=True))
    rows = p['records']['节点']
    assert rows[0]['节点'] == ROOT and rows[1]['节点'] == CHILD
    assert rows[1]['形成方式'] == '原文明示' and rows[1]['依据'] == REQ
    assert rows[2]['父节点'] == ROOT and rows[2]['节点'] not in {ROOT, CHILD}


@pytest.mark.parametrize('text', [
    '# 无依据标题',
    '# 部署\n依据：MAT01',
    '# 部署\n依据：REQ99',
    '## 部署\n依据：REQ01',
    '# 部署\n依据：REQ01\n### 培训\n依据：REQ01',
    '# 部署\n依据：REQ01\n# 部署\n依据：REQ01',
    '```markdown\n# 部署\n依据：REQ01\n```',
    '# 部署\n依据：REQ01\n依据：REQ01',
    '[[结果开始]]\n# 部署\n依据：REQ01',
])
def test_markdown_rejects_missing_basis_bad_references_and_malformed_tree(text):
    with pytest.raises(ParseError):
        parse('P08', text)


def test_branch_heading_depth_and_pending_notes():
    text = '# 部署\n依据：REQ01\n待确认：培训条件缺少原文'
    p = parse('P11', text)
    assert p['fields']['章节'] == ROOT
    assert p['records']['节点'][0]['父节点'] == ROOT
    assert p['fields']['未承接事项'] == '培训条件缺少原文'
    with pytest.raises(ParseError, match='four absolute'):
        parse('P11', '# 部署\n依据：REQ01\n## 子项\n依据：REQ01', inputs('P11', depth=3))


def test_p12_merge_returns_scoped_nodes_and_reports_uncertainty():
    result = parse('P12', '# 部署\n依据：REQ01\n# 培训\n依据：REQ01\n待确认：培训人数待原文确认')
    assert result['fields']['章节'] == ROOT
    assert result['fields']['未解决事项'] == '培训人数待原文确认'
    assert all(row['父节点'] == ROOT for row in result['records']['节点'])


def test_same_title_in_different_branches_preserves_distinct_identities():
    data = inputs(existing=True)
    # Add a second parent with the same child title; identity must include parent.
    view = data['material_view']
    nodes = dict(view['__sections__'])['当前骨架']['nodes']
    nodes += [
        {'node_id': 'other_root', 'title': '另一方案', 'parent_id': None, 'order': 2,
         'basis_requirement_ids': [REQ]},
        {'node_id': 'other_child', 'title': '部署', 'parent_id': 'other_root', 'order': 1,
         'basis_requirement_ids': [REQ]},
    ]
    # Re-prepare from the extended typed material, with consistent visible refs.
    data = prepare_call_inputs({'material': present(**dict(view['__sections__'])),
                               'short_ids': True, 'allowed_ids': [REQ, ROOT, CHILD, 'other_root', 'other_child']})
    result = parse('P08', '# 技术方案\n## 部署\n# 另一方案\n## 部署', data)
    assert [r['节点'] for r in result['records']['节点']] == [ROOT, CHILD, 'other_root', 'other_child']


def test_clear_and_no_change_remain_explicit_actions():
    result = parse('P16', '清空下级：原下级重复且无承接义务')
    assert result['fields']['替换目录'] == '清空'
    assert result['fields']['处理结论'] == '部分修正'
    unchanged = parse('P16', '保持原目录')
    assert unchanged['fields']['替换目录'] == '无'
    with pytest.raises(ParseError):
        parse('P08', '清空下级：不需要')
    assert parse('P08', '信息不足：未提供技术编制依据')['fields']['状态'] == '信息不足'


def test_simple_reading_request_preserves_original_details():
    p = parse('P08', '信息不足：缺少固定清单\n补读：附件甲的技术方案章节顺序和禁止增删条款')
    assert p['supplements'][0]['需要的信息'] == '附件甲的技术方案章节顺序和禁止增删条款'
    assert p['supplements'][0]['查找线索'] == '附件甲的技术方案章节顺序和禁止增删条款'
    with pytest.raises(ParseError):
        parse('P12', '信息不足：缺少候选\n补读：附件甲')


def test_plain_titles_keep_backslashes_and_literal_ids():
    title = r'C:\方案[REQ01]与NODE01'
    p = parse('P08', '# ' + title + '\n依据：REQ01')
    assert p['records']['节点'][0]['标题'] == title
    assert p['records']['节点'][0]['依据'] == REQ


def test_markdown_cannot_bypass_protected_title_constraints():
    base_nodes = [OutlineNode(node_id=ROOT, title='技术方案', order=1, basis_requirement_ids=[REQ]),
                  OutlineNode(node_id=CHILD, title='部署', parent_id=ROOT, order=1, basis_requirement_ids=[REQ])]
    constraint = OutlineConstraint(constraint_id='c', kind='preserve_names_order_hierarchy',
                                   affected_node_ids=[ROOT, CHILD], basis='原文明示', origin='系统策略',
                                   requirement_ids=[REQ])
    base = make_version(base_nodes, None, 'base', [constraint])
    candidate = parse('P08', '# 技术方案\n## 任意改名\n依据：REQ01', inputs(existing=True))
    nodes = nodes_from_records(candidate['records']['节点'], base.nodes, {REQ}, 'candidate')
    with pytest.raises(ValueError, match='preserved node missing'):
        make_version(nodes, base, 'should reject')


def test_executor_markdown_retry_audit_recovery_and_policy_fingerprint(tmp_path):
    async def run():
        c = config(format_retries=1)
        repo = RunRepository(tmp_path)
        client = FakeModelClient(['# 新标题', '# 新标题\n依据：REQ01', '# 新标题\n依据：REQ01'])
        executor = ModelExecutor(c, client, repo, ArtifactStore(tmp_path))
        data = inputs()
        out = await executor.execute('P08', data, task_id='markdown')
        assert out['records']['节点'][0]['依据'] == REQ
        assert 'Markdown目录' in client.requests[1]['messages'][-1]['content']
        assert '[[结果开始]]' not in client.requests[1]['messages'][-1]['content']
        attempts = repo.list_attempts('markdown')
        for row in attempts:
            meta = json.loads((tmp_path/'calls'/row['call_id']/'meta.json').read_text())
            assert meta['enable_thinking'] is True and meta['budget']['enable_thinking'] is True
            assert meta['output_protocol'] == 'markdown-v1'
        assert await executor.execute('P08', data, task_id='markdown') == out
        assert len(client.requests) == 2
        c.thinking_by_task['P08'] = False
        await executor.execute('P08', data, task_id='markdown')
        assert len(client.requests) == 3
        latest = json.loads((tmp_path/'calls'/repo.list_attempts('markdown')[-1]['call_id']/'meta.json').read_text())
        assert latest['enable_thinking'] is False
        await executor.aclose()
        repo.close()
    asyncio.run(run())


def test_workflow_adopts_markdown_skeleton_then_generated_branch(tmp_path):
    from bid_outline.workflow.runner import WorkflowRunner
    from bid_outline.domain import RequirementAssignment

    async def run():
        client = FakeModelClient({
            'P07': ['[[结果开始]]\n[[结果结束]]'],
            'P08': ['# 技术方案\n依据：REQ01'],
            'P11': ['# 部署\n依据：REQ01\n# 培训\n依据：REQ01'],
        })
        r = WorkflowRunner(tmp_path, config(), client)
        r.requirements = [RequirementGroup.model_validate(requirements[0])]
        r._requirement_order = {REQ: (0,)}
        await r.build_skeleton()
        assert r.version and len(r.version.nodes) == 1
        root = r.version.nodes[0]
        r.assignments = [RequirementAssignment(
            requirement_id=REQ, outline_version_id=r.version.version_id, status='已分配',
            targets=[{'node_id': root.node_id, 'scope': '部署与培训'}])]
        await r.generate()
        assert not r.unprocessed, r.unprocessed
        assert [(n.title, n.parent_id) for n in r.version.nodes] == [
            ('技术方案', None), ('部署', root.node_id), ('培训', root.node_id)]
        assert r.version.nodes[0].node_id == root.node_id
        await r.aclose()
    asyncio.run(run())


def test_train_executor_transmits_stage_flags_without_mutating_shared_config(tmp_path):
    async def run():
        c = config()
        client = TrainModelClient(c)
        await client.http.aclose()
        jobs, flags = {}, []
        def handler(request):
            body = json.loads(request.content)
            if request.url.path.endswith('chat_train'):
                item = body['query_list'][0]
                flags.append(item['enable_thinking'])
                jobs[item['id']] = item
                return httpx.Response(200, json={'status': 1, 'responseId': item['id'], 'msg': 'success'})
            ticket = body['response_id']
            answer = ('处理结果：已摘取\n保留内容：提供部署方案\n疑点：无' if ticket == 'extract'
                      else '# 部署\n依据：REQ01')
            return httpx.Response(200, json={'status': 1, 'msg': '任务完成',
                'data': [{'id': ticket, 'answer': answer, 'is_success': True, 'message': '成功'}]})
        client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        repo = RunRepository(tmp_path)
        executor = ModelExecutor(c, client, repo, ArtifactStore(tmp_path))
        await asyncio.gather(
            executor.execute('P03', {'material': '提供部署方案'}, task_id='extract'),
            executor.execute('P08', inputs(), task_id='outline'))
        assert sorted(flags) == [0, 1]
        assert jobs['extract']['enable_thinking'] == 0 and jobs['outline']['enable_thinking'] == 1
        assert c.thinking_default is False
        for task in ('extract', 'outline'):
            meta = json.loads((tmp_path/'calls'/repo.list_attempts(task)[0]['call_id']/'meta.json').read_text())
            assert meta['enable_thinking'] is (task == 'outline')
        await executor.aclose()
        repo.close()
    asyncio.run(run())
