import asyncio
import json
from pathlib import Path
import tempfile

from bid_outline.config import AppConfig, load_config
from bid_outline.model.client import FakeModelClient
from bid_outline.model.executor import ModelExecutor
from bid_outline.model.replay import ReplayCacheClient
from bid_outline.storage import ArtifactStore, RunRepository

GOOD = '[[结果开始]]\n判断：有\n相关主题：部署\n疑点：无\n引用线索：无\n[[结果结束]]'


def test_stage_number_retry_navigation_adoption_and_nested_replay():
    async def run():
        root = Path(tempfile.mkdtemp(prefix='bid-readable-audit-'))
        repo = RunRepository(root)
        executor = ModelExecutor(AppConfig(model='fake'), FakeModelClient(['invalid', GOOD]), repo, ArtifactStore(root))
        inputs = {'material': '部署', 'display_name': '第四章采购需求'}
        try:
            await executor.execute('P01', inputs, task_id='screen')
            await executor.flush_navigation()
            attempts = repo.list_attempts()
            assert attempts[0]['call_id'].startswith('01_screening/0001_P01_第四章采购需求_attempt01')
            assert attempts[1]['call_id'].startswith('01_screening/0002_P01_第四章采购需求_attempt02')
            assert 'failed' in (root / 'calls.md').read_text()
            summary = (root / 'calls' / attempts[1]['call_id'] / 'summary.md').read_text()
            assert '采用：True' in summary
            assert 'attempt 1' in summary
            replay = ReplayCacheClient(FakeModelClient([]), executor.config.base_url, [root])
            # Reuse the exact successful retry body, not the failed initial request.
            body = json.loads((root / 'calls' / attempts[1]['call_id'] / 'request.http.json').read_text())
            assert (await replay.generate(body)).reused_from
        finally:
            await executor.aclose()
            repo.close()
    asyncio.run(run())


def test_manifest_config_is_same_as_standalone_config():
    root = Path(tempfile.mkdtemp(prefix='bid-manifest-config-'))
    path = root / 'manifest.json'
    path.write_text(json.dumps({'layout_version': 2, 'request': {'input': 'x'}, 'config': {'model': 'test-model'}}))
    assert load_config(path).model == 'test-model'


def test_legacy_flat_call_cache_remains_readable():
    async def run():
        root = Path(tempfile.mkdtemp(prefix='bid-flat-cache-'))
        folder = root / 'calls' / ('f' * 32)
        folder.mkdir(parents=True)
        body = {'model': 'test', 'messages': [{'role': 'user', 'content': 'text'}]}
        (folder / 'meta.json').write_text(json.dumps({'base_url': 'http://localhost/v1', 'parse_status': 'valid', 'finish_reason': 'stop'}))
        (folder / 'request.http.json').write_text(json.dumps(body))
        (folder / 'response.model.json').write_text(json.dumps({'content': GOOD}))
        client = ReplayCacheClient(FakeModelClient([]), 'http://localhost/v1', [root])
        assert (await client.generate(body)).content == GOOD
    asyncio.run(run())
