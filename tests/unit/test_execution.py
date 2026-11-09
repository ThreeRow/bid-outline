import asyncio
import json
from pathlib import Path
import tempfile

import httpx
import pytest

from bid_outline.config import AppConfig
from bid_outline.model.budget import BudgetExceeded
from bid_outline.model.client import FakeModelClient, ModelResponse, ModelClientError, VllmChatClient
from bid_outline.model.executor import ModelExecutor, OutputTruncated, TaskExecutionError
from bid_outline.storage import ArtifactStore, RunRepository

GOOD = '[[结果开始]]\n判断：有\n相关主题：部署\n疑点：无\n引用线索：无\n[[结果结束]]'


def setup(responses, **config):
    path = Path(tempfile.mkdtemp(prefix='bid-execution-test-'))
    repository = RunRepository(path)
    client = FakeModelClient(responses)
    executor = ModelExecutor(AppConfig(**config), client, repository, ArtifactStore(path))
    return path, repository, client, executor


def test_config_and_budget():
    with pytest.raises(ValueError):
        AppConfig(temperature=float('nan'))
    with pytest.raises(ValueError):
        AppConfig(max_tokens=True)
    with pytest.raises(ValueError):
        AppConfig(base_url='http://user:secret@localhost/v1')
    with pytest.raises(ValueError):
        AppConfig(extra_body={'messages': []})
    path, repo, client, executor = setup([GOOD], context_tokens=5000, max_tokens=4096, safety_tokens=512)
    with pytest.raises(BudgetExceeded):
        asyncio.run(executor.execute('P01', {'material': '字' * 3000}, task_id='large'))
    assert not client.requests
    assert repo.get_task('large')['status'] == 'failed'


def test_success_reuse_and_input_invalidation():
    path, repo, client, executor = setup([GOOD, GOOD])
    async def run():
        one = await executor.execute('P01', {'material': '部署'}, task_id='t')
        assert await executor.execute('P01', {'material': '部署'}, task_id='t') == one
        await executor.execute('P01', {'material': '新部署'}, task_id='t')
    asyncio.run(run())
    assert len(client.requests) == 2
    assert len(list((path / 'calls').rglob('request.json'))) == 2
    for attempt in repo.list_attempts():
        call = path / 'calls' / attempt['call_id']
        assert {'request.json', 'input.md', 'response.raw.json', 'output.txt', 'parsed.json', 'validation.json', 'meta.json'} <= {p.name for p in call.iterdir()}


def test_format_and_transport_retry_budgets_independent():
    path, repo, client, executor = setup([ModelClientError('busy', retryable=True), 'bad', GOOD], transport_retries=1, format_retries=1)
    asyncio.run(executor.execute('P01', {'material': '部署'}, task_id='t'))
    assert len(client.requests) == 3
    assert [a['reason'] for a in repo.list_attempts()] == ['initial', 'transport_retry', 'format_retry']


def test_truncation_never_retried_even_after_resume():
    path, repo, client, executor = setup([ModelResponse(GOOD, 'length'), GOOD])
    for _ in range(2):
        with pytest.raises(OutputTruncated):
            asyncio.run(executor.execute('P01', {'material': '部署'}, task_id='t'))
    assert len(client.requests) == 1


def test_reasoning_only_length_response_is_truncation_not_invalid_protocol():
    async def run():
        path, repo, _, executor = setup([])
        client = VllmChatClient(AppConfig(model='Qwen3.5-27B'))
        await client.http.aclose()
        raw = {'choices': [{'finish_reason': 'length', 'message': {
            'content': None, 'reasoning': 'thinking-only output'}}],
            'usage': {'completion_tokens': 4096, 'completion_tokens_details': {'reasoning_tokens': 4096}}}
        client.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=raw)))
        executor.client = client
        try:
            with pytest.raises(OutputTruncated):
                await executor.execute('P01', {'material': '技术方案'}, task_id='reasoning_only')
            attempt = repo.list_attempts()[0]
            assert attempt['error_kind'] == 'truncation'
            assert attempt['finish_reason'] == 'length'
            assert json.loads((path / 'calls' / attempt['call_id'] / 'response.raw.json').read_text()) == raw
            assert repo.get_task('reasoning_only')['status'] == 'failed'
        finally:
            await executor.aclose()
    asyncio.run(run())


def test_response_recovery_after_commit_failure():
    path, repo, client, executor = setup([GOOD])
    real_commit = repo.commit_task
    def fail(*args):
        raise OSError('disk failure')
    repo.commit_task = fail
    with pytest.raises(OSError):
        asyncio.run(executor.execute('P01', {'material': '部署'}, task_id='t'))
    assert repo.get_task('t')['status'] == 'failed'
    repo.commit_task = real_commit
    asyncio.run(executor.execute('P01', {'material': '部署'}, task_id='t'))
    assert len(client.requests) == 1
    assert repo.get_task('t')['status'] == 'succeeded'


def test_concurrency_and_cancellation():
    path, repo, client, executor = setup([], max_in_flight=2)
    entered_cancel = asyncio.Event()
    class SlowClient:
        async def generate(self, request):
            runtime = json.loads((path / 'runtime.json').read_text())
            assert 1 <= runtime['in_flight'] <= 2
            if request['_task_id'] == 'cancel': entered_cancel.set()
            await asyncio.sleep(.02)
            return ModelResponse(GOOD)
    executor.client = SlowClient()
    async def run():
        await asyncio.gather(*(executor.execute('P01', {'material': str(i)}, task_id=str(i)) for i in range(8)))
        task = asyncio.create_task(executor.execute('P01', {'material': '取消'}, task_id='cancel'))
        await entered_cancel.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())
    assert executor.stats['peak_in_flight'] == 2
    assert executor.stats['in_flight'] == 0
    assert repo.get_task('cancel')['status'] == 'cancelled'
    assert repo.list_attempts('cancel')[0]['error_kind'] == 'cancelled'
    runtime = json.loads((path / 'runtime.json').read_text())
    assert runtime['in_flight'] == 0
    assert runtime['peak_in_flight'] == 2
    assert runtime['calls'] == 9
    assert runtime['heartbeat_at'].endswith('+00:00')


def test_http_raw_error_and_metadata_removed():
    async def run():
        client = VllmChatClient(AppConfig(model='local-served-model'))
        await client.http.aclose()
        def handler(request):
            assert '_task_type' not in json.loads(request.content)
            return httpx.Response(503, text='upstream unavailable')
        client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with pytest.raises(ModelClientError) as caught:
            await client.generate({'messages': [], '_task_type': 'P01'})
        assert caught.value.raw == 'upstream unavailable'
        assert caught.value.retryable
        await client.aclose()
    asyncio.run(run())


def test_preflight_audit_failure_prevents_request(monkeypatch):
    path, repo, client, executor = setup([GOOD])
    import bid_outline.model.executor as module
    def fail(*args, **kwargs):
        raise OSError('audit unavailable')
    monkeypatch.setattr(module, 'write_once', fail)
    with pytest.raises(OSError):
        asyncio.run(executor.execute('P01', {'material': '部署'}, task_id='t'))
    assert not client.requests


def test_total_cap_and_resume_retry_cap():
    path, repo, client, executor = setup(['bad', 'bad', GOOD], format_retries=1)
    for _ in range(2):
        with pytest.raises(TaskExecutionError):
            asyncio.run(executor.execute('P01', {'material': '部署'}, task_id='t'))
    assert len(client.requests) == 2
    path, repo, client, executor = setup([GOOD, GOOD], max_run_calls=1)
    asyncio.run(executor.execute('P01', {'material': '部署'}, task_id='a'))
    with pytest.raises(TaskExecutionError):
        asyncio.run(executor.execute('P01', {'material': '部署'}, task_id='b'))
    assert len(client.requests) == 1


def test_concurrent_total_cap():
    path, repo, client, executor = setup([GOOD] * 5, max_run_calls=2)
    async def run():
        results = await asyncio.gather(*(executor.execute('P01', {'material': str(i)}, task_id=str(i)) for i in range(5)), return_exceptions=True)
        assert sum(isinstance(x, TaskExecutionError) for x in results) == 3
    asyncio.run(run())
    assert len(client.requests) == 2


def test_service_context_error_is_not_retried_on_resume():
    path, repo, client, executor = setup([ModelClientError('context too long', raw={'error': 'maximum context length'},
        status_code=400, context_exceeded=True), GOOD])
    for _ in range(2):
        with pytest.raises(BudgetExceeded):
            asyncio.run(executor.execute('P01', {'material': '部署'}, task_id='t'))
    assert len(client.requests) == 1
    attempt = repo.list_attempts('t')[0]
    assert attempt['context_exceeded']
    assert json.loads((path / 'calls' / attempt['call_id'] / 'response.raw.json').read_text()) == {'error': 'maximum context length'}


def test_audit_failure_after_response_remains_fatal_and_recovers(monkeypatch):
    path, repo, client, executor = setup([GOOD])
    import bid_outline.model.executor as module
    original = module.write_once
    def fail_output(path, value, **kwargs):
        if path.name == 'output.txt':
            raise OSError('output audit write failure')
        return original(path, value, **kwargs)
    monkeypatch.setattr(module, 'write_once', fail_output)
    with pytest.raises(OSError):
        asyncio.run(executor.execute('P01', {'material': '部署'}, task_id='t'))
    assert repo.get_task('t')['status'] == 'failed'
    assert 'accepted_artifact_id' not in repo.get_task('t')
    assert executor.stats['in_flight'] == 0
    monkeypatch.setattr(module, 'write_once', original)
    asyncio.run(executor.execute('P01', {'material': '部署'}, task_id='t'))
    assert len(client.requests) == 1
    attempt = repo.list_attempts('t')[0]
    for name in ('output.txt', 'parsed.json', 'validation.json'):
        assert (path / 'calls' / attempt['call_id'] / name).exists()
    assert repo.get_task('t')['status'] == 'succeeded'


def test_local_budget_forwards_thinking_template_parameters():
    from bid_outline.model.budget import TokenBudget
    budget = TokenBudget(AppConfig(extra_body={'chat_template_kwargs': {'enable_thinking': False}}))
    class LocalTokenizer:
        def apply_chat_template(self, messages, **kwargs):
            assert kwargs == {'tokenize': True, 'add_generation_prompt': True, 'enable_thinking': False}
            return [1, 2, 3]
    budget.tokenizer = LocalTokenizer()
    report = budget.check([{'role': 'user', 'content': 'test'}], 100)
    assert report['input_tokens'] == 3
    assert report['exact'] is False
    assert 'compatibility unverified' in report['method']


def test_http_retry_after_dates_and_nonfinite_values():
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime
    async def run():
        client = VllmChatClient(AppConfig(model='served'))
        await client.http.aclose()
        headers = [format_datetime(datetime.now(timezone.utc) + timedelta(seconds=60), usegmt=True), 'nan', 'inf']
        def handler(request):
            return httpx.Response(429, text='busy', headers={'Retry-After': headers.pop(0)})
        client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        delays = []
        for _ in range(3):
            with pytest.raises(ModelClientError) as error:
                await client.generate({'messages': []})
            delays.append(error.value.retry_after)
        assert 55 <= delays[0] <= 60
        assert delays[1:] == [0, 0]
        await client.aclose()
    asyncio.run(run())
