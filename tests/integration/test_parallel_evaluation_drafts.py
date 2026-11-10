"""N02-B volumes overlap in time but are fused in source order."""
import asyncio

import pytest

from bid_outline.model.client import FakeModelClient, ModelClientError, ModelResponse
from bid_outline.workflow import WorkflowRunner
from test_collaborative_extraction import config, document


@pytest.mark.parametrize('third_result', ['success', 'none', 'failure'])
def test_independent_b_volumes_are_bounded_parallel_and_fused_in_order(tmp_path, third_result):
    async def run():
        class Client(FakeModelClient):
            def __init__(self):
                super().__init__([])
                self.second_finished = asyncio.Event()
                self.finished = []
                self.active = self.peak = 0
                self.merges = []

            async def generate(self, request):
                self.requests.append(request)
                task = request['_task_type']
                body = request['messages'][-1]['content'].split('本次资料：\n', 1)[1]
                if task == 'N01-B':
                    return ModelResponse('评审分册' + next(n for n in '123' if n in body))
                if task == 'E03-B':
                    # Exercise the real preserved-volume path after consolidation
                    # fails, rather than mocking away the workflow's organization.
                    raise ModelClientError('保留独立评审分册供后续生成')
                if task == 'N02-B':
                    number = body[-1]
                    self.active += 1
                    self.peak = max(self.peak, self.active)
                    try:
                        if number == '1':
                            await asyncio.wait_for(self.second_finished.wait(), 2)
                        elif number == '2':
                            self.second_finished.set()
                        self.finished.append(number)
                        if number == '3' and third_result == 'failure':
                            raise ModelClientError('第三分册失败')
                        return ModelResponse('无' if number == '3' and third_result == 'none' else '目录' + number)
                    finally:
                        self.active -= 1
                if task == 'N03':
                    self.merges.append(body)
                    return ModelResponse('累计目录12' if len(self.merges) == 1 else '累计目录123')
                if task == 'N05':
                    return ModelResponse(body)
                raise AssertionError(task)

        client = Client()
        runner = WorkflowRunner(tmp_path, config(directory_assistance=False, workers=3,
            max_in_flight=2, transport_retries=0), client)
        doc = document([(['技术评审甲'], '要求1'), (['技术评审乙'], '要求2'), (['技术评审丙'], '要求3')])
        try:
            result = await runner.run(doc)
            assert client.peak == 2
            assert client.finished.index('2') < client.finished.index('1')
            assert runner.executor.stats['peak_in_flight'] == 2
            assert len(client.merges) == (2 if third_result == 'success' else 1)
            assert client.merges[0].index('目录1') < client.merges[0].index('目录2')
            if third_result == 'success':
                assert client.merges[1].index('累计目录12') < client.merges[1].index('目录3')
            assert result['directory_text'] == ('累计目录123' if third_result == 'success' else '累计目录12')
            # All three logical tasks still receive independent audit/dependencies.
            tasks = [t for t in runner.repository.list_tasks() if t['task_type'] == 'N02-B']
            assert len(tasks) == 3 and all(t['dependencies'] for t in tasks)
            if third_result == 'failure':
                assert any(x['task_type'] == 'N02-B' for x in result['unprocessed_ranges'])
            # The deliberate earlier consolidation failure remains visible.
            assert result['status'] == 'incomplete'
        finally:
            await runner.aclose()
    asyncio.run(run())
