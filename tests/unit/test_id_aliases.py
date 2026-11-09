import asyncio
import json
from pathlib import Path
import tempfile

import pytest

from bid_outline.config import AppConfig
from bid_outline.model.client import FakeModelClient
from bid_outline.model.executor import ModelExecutor, TaskExecutionError
from bid_outline.storage import ArtifactStore, RunRepository


def test_short_alias_wire_and_global_restore_and_unknown_rejection():
    async def run():
        root = Path(tempfile.mkdtemp(prefix='bid-alias-test-'))
        rid = 'R_2701c902766923f511b6'
        nid = 'N_1234567890abcdef1234'
        response = '''[[结果开始]]
[[归属开始]]
要求：REQ01
状态：已分配
承接节点：NODE01
承接范围：REQ01由NODE01完整承接
未承接内容：无
依据或疑点：无
[[归属结束]]
[[结果结束]]'''
        client = FakeModelClient([response, response.replace('REQ01', 'REQ99')])
        repo = RunRepository(root)
        executor = ModelExecutor(AppConfig(model='fake', format_retries=0), client, repo, ArtifactStore(root))
        inputs = {'material': f'要求：{rid}；节点：{nid}', 'scope': nid,
                  'allowed_ids': [rid, nid], 'expected_ids': [rid], 'short_ids': True,
                  'id_groups': {'requirements': [rid], 'nodes': [nid]}}
        try:
            result = await executor.execute('P09', inputs, task_id='one')
            wire = json.dumps(client.requests[0]['messages'], ensure_ascii=False)
            assert rid in wire and nid in wire  # unstructured prose is never rewritten
            assert 'REQ01' in wire and 'NODE01' in wire
            record = result['records']['归属'][0]
            assert record['要求'] == rid and record['承接节点'] == nid
            assert record['承接范围'] == 'REQ01由NODE01完整承接'
            mapping = json.loads(next((root / 'calls').rglob('id_aliases.json')).read_text())
            assert mapping['alias_to_id']['REQ01'] == rid
            with pytest.raises(TaskExecutionError, match='REQ99'):
                await executor.execute('P09', inputs, task_id='two')
        finally:
            await executor.aclose()
            repo.close()
    asyncio.run(run())
