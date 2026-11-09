import asyncio
from pathlib import Path
import tempfile

from bid_outline.config import AppConfig
from bid_outline.model.client import FakeModelClient
from bid_outline.model.executor import ModelExecutor
from bid_outline.model.replay import ReplayCacheClient
from bid_outline.storage import ArtifactStore, RunRepository


GOOD = '[[结果开始]]\n判断：未发现\n相关主题：无\n疑点：无\n引用线索：无\n[[结果结束]]'


def test_explicit_reuse_matches_entire_request_and_is_audited():
    async def run():
        base = Path(tempfile.mkdtemp(prefix='bid-exact-replay-'))
        config = AppConfig(model='fake')
        old_repo = RunRepository(base / 'old')
        old = ModelExecutor(config, FakeModelClient([GOOD]), old_repo, ArtifactStore(base / 'old'))
        await old.execute('P01', {'material': '旧材料'}, task_id='old')
        await old.aclose()
        fallback = FakeModelClient([GOOD])
        client = ReplayCacheClient(fallback, config.base_url, [base / 'old'])
        repo = RunRepository(base / 'new')
        current = ModelExecutor(config, client, repo, ArtifactStore(base / 'new'))
        try:
            await current.execute('P01', {'material': '旧材料'}, task_id='same')
            assert not fallback.requests
            assert current.stats['reused_responses'] == 1
            assert current.stats['calls'] == 0
            assert repo.list_attempts()[0]['reused_from']['match'] == 'exact_http_body_and_service'
            await current.execute('P01', {'material': '新材料'}, task_id='different')
            assert len(fallback.requests) == 1
            assert current.stats['calls'] == 1
        finally:
            await current.aclose()
            repo.close()
            old_repo.close()
    asyncio.run(run())
