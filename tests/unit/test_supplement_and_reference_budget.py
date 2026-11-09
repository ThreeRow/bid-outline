import asyncio
from pathlib import Path
import tempfile

from bid_outline.config import AppConfig
from bid_outline.model.client import FakeModelClient
from bid_outline.model.executor import ModelExecutor
from bid_outline.model.parsers import parse_output
from bid_outline.storage import ArtifactStore, RunRepository


def test_supplement_after_result_marker_is_accepted_only_as_supplement():
    text = '''[[结果开始]]
判断：待确认
相关主题：无
疑点：缺少附件
引用线索：无
[[结果结束]]
[[补读开始]]
引用或疑点：附件
需要的信息：附件正文
用途：确认要求
查找线索：附件
[[补读结束]]'''
    result = parse_output('P01', text)
    assert len(result['supplements']) == 1
    assert result['fields']['判断'] == '待确认'


def test_compact_p06_candidate_request_stays_under_budget():
    async def run():
        root = Path(tempfile.mkdtemp(prefix='bid-p06-compact-'))
        repo = RunRepository(root)
        # P06 response itself is valid; request material contains only compact refs.
        response = '''[[结果开始]]
结论：候选均不适用
采用候选：无
关联说明：候选与引用不匹配
仍缺少的信息：无
[[结果结束]]'''
        client = FakeModelClient([response])
        executor = ModelExecutor(AppConfig(model='fake', context_tokens=20000), client, repo, ArtifactStore(root))
        result = await executor.execute('P06', {
            'material': '引用需要：附件\n候选：e1；类型=paragraph；内容=' + '正文' * 100,
            'allowed_ids': ['e1'], 'id_groups': {'elements': ['e1']},
        }, task_id='p06')
        assert result['fields']['结论'] == '候选均不适用'
        material = client.requests[0]['messages'][1]['content'].split('完整材料包：\n', 1)[1].split('[[输入数据结束]]', 1)[0]
        assert material.count('正文') == 100
        await executor.aclose(); repo.close()
    asyncio.run(run())
