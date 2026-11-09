"""Regression evidence from the 2026-11-03 GPU run; no model calls or adoption."""

import asyncio
import json
from pathlib import Path
import tempfile

import pytest

from bid_outline.config import AppConfig
from bid_outline.model.client import FakeModelClient
from bid_outline.model.executor import ModelExecutor
from bid_outline.model.parsers import ParseError, parse_output, close_complete_final_record
from bid_outline.model.task_specs import PROMPTS, TASK_NAMES, render_task
from bid_outline.storage import ArtifactStore, RunRepository
from bid_outline.workflow.outline import nodes_from_records


CASES = json.loads((Path(__file__).parents[1] / 'fixtures/real_format_failures_20261103.json').read_text())


@pytest.mark.parametrize('case', CASES, ids=lambda case: case['source_call_id'])
def test_real_invalid_responses_are_not_silently_accepted(case):
    with pytest.raises(ParseError, match=case['expected_error']):
        parse_output(case['task_type'], case['response'], allowed_ids=case['allowed_ids'])


def test_prompt_instructions_are_outside_result_envelope():
    explanatory_lines = {
        '按相关结构单元输出：', '每个输入要求输出一条：', '每条建议输出一条：',
        '按需要重复以下记录；没有则不输出记录：', '必要时附补读记录',
        '按要求或相互关联的要求组输出：', '有建议时重复：', '有问题时重复：',
    }
    for task, name in TASK_NAMES.items():
        if task == 'P19':
            continue
        text = (PROMPTS / f'{task}_{name}.txt').read_text()
        body = text.split('[[结果开始]]', 1)[1].split('[[结果结束]]', 1)[0]
        assert not explanatory_lines.intersection(line.strip() for line in body.splitlines()), task


def test_real_p07_needs_only_removal_of_erroneous_template_instructions():
    case = CASES[1]
    corrected = '\n'.join(line for line in case['response'].splitlines()
                          if line not in {'按相关结构单元输出：', '必要时附补读记录'})
    result = parse_output('P07', corrected, allowed_ids=case['allowed_ids'])
    assert len(result['records']['结构依据']) == 4
    assert len(result['supplements']) == 3
    # Business text stays unchanged; this is a test-only corrected fixture.
    assert result['records']['结构依据'][0]['原文表达'] in case['response']


def test_directory_contract_aligned_between_parser_and_mapper():
    node = {'节点': 'N01', '父节点': 'ROOT', '同级顺序': '1', '标题': '部署',
            '依据': 'REQ', '形成方式': '原文明示', '说明': '无'}
    assert nodes_from_records([node], [], {'REQ'}, 'test')[0].node_id.startswith('N_')
    with pytest.raises(ValueError, match='unknown node'):
        nodes_from_records([{**node, '节点': 'N1'}], [], {'REQ'}, 'test')
    rendered = render_task('P08', {'material': 'test', 'allowed_ids': ['REQ', 'MAT'],
                                 'id_groups': {'requirements': ['REQ'], 'materials': ['MAT']}})
    assert '新建节点只能使用本次局部编号N01' in rendered[1]['content']
    assert '要求ID：\nREQ' in rendered[1]['content']
    text = '[[结果开始]]\n状态：已形成候选骨架\n骨架：\n[[节点开始]]\n' + '\n'.join(
        f'{k}：{v}' for k, v in {**node, '依据': 'MAT'}.items()) + '\n[[节点结束]]\n调整说明：首次\n待核查事项：无\n[[结果结束]]'
    with pytest.raises(ParseError, match='requirement IDs'):
        parse_output('P08', text, allowed_ids=['REQ', 'MAT'],
                     id_groups={'requirements': ['REQ'], 'materials': ['MAT'], 'nodes': []})


def test_one_complete_requirement_can_have_two_adjustment_targets():
    def record(target, scope):
        return ('[[调整建议开始]]\n要求：REQ01\n处理：归入已有节点\n'
                f'目标或父节点：{target}\n建议标题：无\n位置说明：无\n'
                f'承接内容：{scope}\n依据与限制：保留共同条件\n[[调整建议结束]]')
    text = '[[结果开始]]\n' + record('NODE01', '部署方案') + '\n' + record('NODE02', '实施计划') + '\n[[结果结束]]'
    result = parse_output('P10', text, allowed_ids=['REQ01', 'NODE01', 'NODE02'], expected_ids=['REQ01'])
    assert len(result['records']['调整建议']) == 2
    with pytest.raises(ParseError, match='coverage mismatch'):
        parse_output('P10', text, allowed_ids=['REQ01', 'REQ02', 'NODE01', 'NODE02'], expected_ids=['REQ01', 'REQ02'])


def test_only_complete_final_record_closure_can_be_repaired():
    text = ('[[结果开始]]\n提取结果：已提取\n[[要求开始]]\n局部编号：R01\n类别：A\n'
            '材料依据：MAT01\n要求主题：部署\n要求内容：应提供部署方案\n疑点：无\n[[结果结束]]')
    result = close_complete_final_record('P04-A', text, allowed_ids=['MAT01'])
    assert result['records']['要求'][0]['要求内容'] == '应提供部署方案'
    assert result['format_repairs'][0]['inserted'] == '[[要求结束]]'
    for invalid in (text.replace('疑点：无\n', ''), text.replace('MAT01', 'UNKNOWN'),
                    text.replace('[[结果结束]]', ''), text.replace('类别：A', '类别：B')):
        with pytest.raises(ParseError):
            close_complete_final_record('P04-A', invalid, allowed_ids=['MAT01'])


def test_format_retry_contains_actual_diagnostic_and_keeps_original_material():
    case = CASES[0]
    corrected = case['response'].replace('提取结果：已提取本类要求', '提取结果：已提取')

    async def run():
        root = Path(tempfile.mkdtemp(prefix='bid-format-feedback-'))
        repository = RunRepository(root)
        client = FakeModelClient([case['response'], corrected])
        executor = ModelExecutor(AppConfig(model='fake'), client, repository, ArtifactStore(root))
        try:
            material = '完整原始材料；条件和来源不得被重试删除。'
            result = await executor.execute(case['task_type'], {'material': material, 'allowed_ids': case['allowed_ids']}, task_id='test')
            assert result['fields']['提取结果'] == '已提取'
            first, second = [request['messages'] for request in client.requests]
            assert first == second[:len(first)]
            assert first != second
            assert material in second[1]['content']
            assert 'Invalid 提取结果: 已提取本类要求' in second[-1]['content']
            assert '提取结果只允许：已提取、未发现本类要求、仅有待定内容' in second[-1]['content']
            attempts = repository.list_attempts()
            assert attempts[1]['format_correction_of'] == attempts[0]['call_id']
            assert attempts[1]['budget']['input_tokens'] > attempts[0]['budget']['input_tokens']
            assert json.loads((root / 'calls' / attempts[1]['call_id'] / 'request.http.json').read_text())['messages'] == second
        finally:
            await executor.aclose()
            repository.close()
    asyncio.run(run())
