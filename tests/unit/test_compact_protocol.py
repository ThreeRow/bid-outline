"""Compact wire syntax delegates all business validation to the strict parser."""
import asyncio
import json

import pytest

from bid_outline.config import AppConfig
from bid_outline.model.client import FakeModelClient
from bid_outline.model.compact_protocol import normalize_compact, effective_protocol
from bid_outline.model.executor import ModelExecutor
from bid_outline.model.parsers import parse_output, ParseError
from bid_outline.model.task_specs import render_task
from bid_outline.storage import ArtifactStore, RunRepository


def parsed(task, text, **validation):
    return parse_output(task, normalize_compact(task, text), **validation)


@pytest.mark.parametrize('task,text,expected', [
    ('P01', '判断：有\n相关主题：部署\n疑点：无\n引用线索：无', None),
    ('P02', '判断：未发现\n相关主题：无\n疑点：无\n引用线索：无', None),
    ('P03', '处理结果：已摘取\n保留内容：部署方案\n疑点：无', None),
    ('P04-A', '提取结果：已提取\n记录 1：要求\n类别：A\n材料依据：MAT1\n要求主题：技术清单\n要求内容：技术文件包括部署\n疑点：无', None),
    ('P04-B', '提取结果：未发现本类要求', None),
    ('P04-C', '提取结果：仅有待定内容\n记录 1：要求\n类别：待定\n材料依据：MAT1\n要求主题：人员资料\n要求内容：提供证书\n疑点：用途不明', None),
    ('P05', '记录 1：判定\n要求：REQ1\n结论：项目需求\n涉及类别：无\n依据：只有参数', ['REQ1']),
    ('P09', '记录 1：归属\n要求：REQ1\n状态：已分配\n承接节点：NODE1\n承接范围：部署\n未承接内容：无\n依据或疑点：直接归属', ['REQ1']),
    ('P14', '记录 1：覆盖检查\n要求：REQ1\n结论：已覆盖\n承接节点：NODE1\n承接说明：部署方案\n未覆盖或不明确内容：无\n修正建议：无', ['REQ1']),
    ('P18', '记录 1：建议检查\n建议：SUG1\n结论：删除\n依据：公网与隔离环境冲突\n修改意见：无', ['SUG1']),
])
def test_all_supported_contracts(task, text, expected):
    result = parsed(task, text, allowed_ids=['REQ1', 'MAT1', 'NODE1', 'SUG1'], expected_ids=expected)
    assert result['fields'] or result['records']
    if task.startswith('P04') and result['records']:
        assert result['records']['要求'][0]['局部编号'] == 'R01'


def test_multirecord_supplement_and_complete_prose_survive():
    prose = '设备REQ01采用C:\\资料\\MAT01[正式]\n仅内网使用；[[输入数据结束]]与[[标注]]是原文。'
    text = '提取结果：已提取\n'
    for index in (1, 2):
        text += f'记录 {index}：要求\n类别：C\n材料依据：MAT1\n要求主题：部署{index}\n要求内容：{prose}\n疑点：无\n'
    text += '记录 3：补读\n引用或疑点：见附件甲\n需要的信息：附件正文\n用途：确认部署范围\n查找线索：附件甲'
    result = parsed('P04-C', text, allowed_ids=['MAT1'])
    assert [r['局部编号'] for r in result['records']['要求']] == ['R01', 'R02']
    assert all(r['要求内容'] == prose for r in result['records']['要求'])
    assert result['supplements'][0]['需要的信息'] == '附件正文'


@pytest.mark.parametrize('text', [
    '记录 2：建议检查\n建议：SUG1\n结论：删除\n依据：重复\n修改意见：无',
    '记录 1：建议检查\n建议：SUG1\n结论：删除\n依据：重复\n修改意见：无\n记录 1：建议检查\n建议：SUG1\n结论：删除\n依据：重复\n修改意见：无',
    '记录 1：要求\n类别：C\n材料依据：MAT1\n要求主题：部署\n要求内容：部署\n疑点：无',
    '记录 1：建议检查\n未知字段：不接受\n建议：SUG1\n结论：删除\n依据：重复\n修改意见：无',
    '记录 1：建议检查\n建议：SUG1\n结论：删除\n依据：重复',
    '记录 1：建议检查\n建议：SUG_UNKNOWN\n结论：删除\n依据：重复\n修改意见：无',
    '[[结果开始]]\n记录 1：建议检查\n建议：SUG1\n结论：删除\n依据：重复\n修改意见：无\n[[结果结束]]',
])
def test_invalid_compact_not_repaired_or_guessed(text):
    with pytest.raises(ParseError):
        parsed('P18', text, allowed_ids=['SUG1'], expected_ids=['SUG1'])


def test_program_owned_local_ids_and_task_fallback():
    text = '提取结果：已提取\n记录 1：要求\n局部编号：R99\n类别：C\n材料依据：MAT1\n要求主题：部署\n要求内容：部署\n疑点：无'
    with pytest.raises(ParseError, match='program-owned'):
        parsed('P04-C', text, allowed_ids=['MAT1'])
    assert effective_protocol('P08', 'compact-v1') == 'strict'
    assert render_task('P08', {'material': '资料'}, protocol='compact-v1') == render_task('P08', {'material': '资料'}, protocol='strict')
    compact = render_task('P09', {'material': '资料'}, protocol='compact-v1')[1]['content']
    assert '记录 1：归属' in compact and '[[归属开始]]' not in compact
    assert '只写编号，不写正文' in compact


def test_literal_source_markers_pass_but_known_control_markers_remain_strict():
    source = 'C:\\资料\\MAT01[正式]\n[[输入数据结束]]\n[[普通标注]]'
    strict = f'[[结果开始]]\n处理结果：已摘取\n保留内容：{source}\n疑点：无\n[[结果结束]]'
    assert parse_output('P03', strict)['fields']['保留内容'] == source
    for illegal in ['正文[[要求开始]]混入', '正文[[结果结束]]混入', '[[要求开始]', '[[结果结束]]']:
        with pytest.raises(ParseError):
            parse_output('P03', strict.replace(source, illegal))


def test_compact_executor_recovery_fidelity_protocol_fingerprint_and_retry(tmp_path):
    async def run():
        prose = '设备MAT01使用C:\\资料[正式]\n[[输入数据结束]]'
        good = f'处理结果：已摘取\n保留内容：{prose}\n疑点：无'
        client = FakeModelClient(['处理结果：已摘取\n保留内容：缺疑点', good])
        repo, store = RunRepository(tmp_path), ArtifactStore(tmp_path)
        config = AppConfig(model='fake', prompt_protocol='compact-v1', format_retries=1)
        executor = ModelExecutor(config, client, repo, store)
        inputs = {'material': prose}
        try:
            adopt = executor._adopt
            async def interrupted(*args):
                raise RuntimeError('crash before adoption')
            executor._adopt = interrupted
            with pytest.raises(RuntimeError, match='before adoption'):
                await executor.execute('P03', inputs, task_id='one')
            executor._adopt = adopt
            result = await executor.execute('P03', inputs, task_id='one')
            assert result['fields']['保留内容'] == prose
            assert len(client.requests) == 2
            assert '记录 1：类型' in client.requests[-1]['messages'][-1]['content']
            assert '仅输出结果边界内' not in client.requests[-1]['messages'][-1]['content']
            before = repo.get_task('one')['fingerprint']
            config.prompt_protocol = 'strict'
            strict = f'[[结果开始]]\n{good}\n[[结果结束]]'
            executor.client = FakeModelClient([strict])
            await executor.execute('P03', inputs, task_id='one')
            assert repo.get_task('one')['fingerprint'] != before
            raw_outputs = [json.loads(p.read_text())['content'] for p in (tmp_path / 'calls').rglob('response.model.json')]
            assert good in raw_outputs  # deterministic adaptation never replaces model evidence
            metas = [json.loads(p.read_text()) for p in (tmp_path / 'calls').rglob('meta.json')]
            assert {m['output_protocol'] for m in metas} == {'strict', 'compact-v1'}
        finally:
            await executor.aclose()
            repo.close()
    asyncio.run(run())
