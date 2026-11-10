"""Train wire contract, polling identity, durable resume and thinking adapter."""
import asyncio
import json
from pathlib import Path
import tempfile

import httpx
import pytest

from bid_outline.config import AppConfig
from bid_outline.model.client import make_model_client, ModelClientError
from bid_outline.model.train_client import TrainModelClient, train_query
from bid_outline.model.executor import ModelExecutor, TaskExecutionError, OutputTruncated
from bid_outline.storage import ArtifactStore, RunRepository

GOOD='判断：未发现\n相关主题：无\n疑点：无\n引用线索：无'


def config(**updates):
    return AppConfig(provider='train',base_url='http://train.test/v1',model='service-selected',
                     prompt_protocol='compact-v1',train_poll_interval_seconds=.001,
                     train_poll_timeout_seconds=.05,format_retries=0,train_poll_retries=1,**updates)


def terminal(query_id='t', answer=GOOD, **extra):
    return {'status':1,'msg':'任务完成','data':[{'id':query_id,'answer':answer,'is_success':True,**extra}]}


async def executor(handler, cfg=None, directory=None):
    cfg=cfg or config()
    directory=directory or Path(tempfile.mkdtemp(prefix='bid-train-'))
    repo=RunRepository(directory)
    client=TrainModelClient(cfg)  # Mock transport: exercise wire protocol without a production tokenizer.
    await client.http.aclose()
    client.http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ModelExecutor(cfg,client,repo,ArtifactStore(directory)),repo,directory


def test_train_config_normalizes_and_disallows_openai_options():
    assert config().base_url=='http://train.test'
    assert config().train_enable_thinking is True
    with pytest.raises(ValueError):config(extra_body={'chat_template_kwargs':{'enable_thinking':True}})
    with pytest.raises(ValueError):AppConfig(provider='train',base_url='http://test/api/v2')
    with pytest.raises(ValueError):config(train_level=3)
    assert AppConfig().provider=='openai'


def test_query_preserves_independent_roles_and_retry_feedback():
    messages=[{'role':'system','content':'独立任务边界'},{'role':'user','content':'原文C:\\a[1]'},
              {'role':'user','content':'完整格式诊断'}]
    query=train_query(messages)
    assert query.index('独立任务边界')<query.index('原文C:\\a[1]')<query.index('完整格式诊断')
    with pytest.raises(ValueError):train_query([{'role':'tool','content':'bad'}])


def test_text_task_uses_train_ticket_and_accepts_unstructured_answer():
    async def run():
        seen = []
        answer = '先整理评审内容。\n总体技术方案\n    完整性\n    先进性'
        def server(request):
            body = json.loads(request.content)
            seen.append(request.url.path)
            if request.url.path.endswith('/chat_train'):
                query = body['query_list'][0]
                assert query['enable_thinking'] == 0
                assert '允许引用ID' not in query['query']
                return httpx.Response(200, json={'status': 1, 'responseId': 'text-job'})
            return httpx.Response(200, json=terminal(answer=answer))
        e, repo, path = await executor(server, config(thinking_by_task={'N02-B': False}))
        try:
            result = await e.execute('N02-B', {'material': '总体技术方案，完整性、先进性'}, task_id='t')
            assert result['text'] == answer
            assert seen == ['/api/v2/chat_train', '/api/v2/get_resp_data']
            call = path / 'calls' / repo.list_attempts()[0]['call_id']
            assert (call / 'train.ticket.json').exists()
            assert json.loads((call / 'meta.json').read_text())['output_protocol'] == 'text-v1'
        finally:
            await e.aclose()
            repo.close()
    asyncio.run(run())


def test_wire_thinking_id_selection_partial_not_adopted_and_full_audit():
    async def run():
        seen=[]
        def server(request):
            body=json.loads(request.content);seen.append((request.url.path,body))
            if request.url.path.endswith('/chat_train'):
                q=body['query_list'][0]
                assert q['id']=='t' and q['enable_thinking']==1 and q['res_type']=='str'
                assert q['level']==0 and q['timeout']==600
                assert q['max_tokens']==20480
                assert 'model' not in body and 'messages' not in body
                assert '用户消息' in q['query'] and '系统说明' in q['query']
                return httpx.Response(200,json={'status':1,'responseId':'job'})
            assert body=={'response_id':'job'}
            if len(seen)==2:
                return httpx.Response(200,json={'status':1,'msg':'任务进行中','data':[{'id':'t','answer':'partial','is_success':True}]})
            result=terminal(answer='<think>推理内容</think>\n'+GOOD)
            result['data'].insert(0,{'id':'unrelated','answer':'WRONG','is_success':True})
            return httpx.Response(200,json=result)
        e,repo,path=await executor(server)
        try:
            result=await e.execute('P01',{'material':'无相关条款'},task_id='t')
            assert result['fields']['判断']=='未发现'
            call=repo.list_attempts()[0]['call_id'];folder=path/'calls'/call
            wire=json.loads((folder/'request.http.json').read_text())
            assert wire['query_list'][0]['enable_thinking']==1
            assert json.loads((folder/'train.ticket.json').read_text())['response_id']=='job'
            saved=json.loads((folder/'response.model.json').read_text())
            assert saved['reasoning']=='推理内容' and saved['content']==GOOD
            assert saved['raw']['result']['data'][1]['answer'].startswith('<think>')
            assert len(list(folder.glob('train.poll-*.response.json')))==2
            assert repo.get_task('t')['status']=='succeeded' and e.stats['calls']==1
        finally:await e.aclose();repo.close()
    asyncio.run(run())


def test_poll_transport_retry_never_resubmits():
    async def run():
        paths=[]
        def server(request):
            paths.append(request.url.path)
            if request.url.path.endswith('/chat_train'):return httpx.Response(200,json={'status':1,'responseId':'job'})
            if len(paths)==2:raise httpx.ReadTimeout('temporary',request=request)
            return httpx.Response(200,json=terminal())
        e,repo,_=await executor(server)
        try:
            await e.execute('P01',{'material':'x'},task_id='t')
            assert paths.count('/api/v2/chat_train')==1
            assert paths.count('/api/v2/get_resp_data')==2
        finally:await e.aclose();repo.close()
    asyncio.run(run())


def test_timeout_restarts_polling_same_durable_ticket_after_process_reopen():
    async def run():
        submissions=[]
        def pending(request):
            if request.url.path.endswith('/chat_train'):
                submissions.append(1);return httpx.Response(200,json={'status':1,'responseId':'saved-job'})
            return httpx.Response(200,json={'status':1,'msg':'任务进行中','data':[]})
        e,repo,path=await executor(pending)
        try:
            with pytest.raises(ModelClientError,match='deadline'):
                await e.execute('P01',{'material':'x'},task_id='t')
            assert repo.get_task('t')['status']=='failed'
        finally:await e.aclose();repo.close()
        def complete(request):
            assert request.url.path=='/api/v2/get_resp_data'
            assert json.loads(request.content)=={'response_id':'saved-job'}
            return httpx.Response(200,json=terminal())
        resumed,repo,_=await executor(complete,directory=path)
        try:
            answer=await resumed.execute('P01',{'material':'x'},task_id='t')
            assert answer['fields']['判断']=='未发现' and submissions==[1]
            assert resumed.stats['calls']==0 and resumed.stats['resumed_provider_jobs']==1
        finally:await resumed.aclose();repo.close()
    asyncio.run(run())


def test_ambiguous_submission_is_not_retried_or_resubmitted_on_resume():
    async def run():
        count=0
        def server(request):
            nonlocal count
            count+=1;raise httpx.ReadTimeout('receipt lost',request=request)
        e,repo,_=await executor(server)
        try:
            with pytest.raises(ModelClientError):await e.execute('P01',{'material':'x'},task_id='t')
            with pytest.raises(TaskExecutionError,match='outcome uncertain'):
                await e.execute('P01',{'material':'x'},task_id='t')
            assert count==1 and repo.get_task('t')['status']=='failed'
        finally:await e.aclose();repo.close()
    asyncio.run(run())


@pytest.mark.parametrize('bad',[
    {'status':1,'msg':'完成','data':[]},
    {'status':1,'msg':'完成','data':[{'id':'t','answer':GOOD,'is_success':False}]},
    {'status':1,'msg':'完成','data':[{'id':'t','answer':GOOD,'is_success':True}]*2},
])
def test_terminal_item_identity_and_success_are_required(bad):
    with pytest.raises(ModelClientError):TrainModelClient._response(bad,{'query_id':'t','response_id':'r'})


def test_unclosed_thinking_is_truncated_not_success():
    response=TrainModelClient._response(terminal(answer='<think>unfinished'),{'query_id':'t','response_id':'r'})
    assert response.finish_reason=='length' and response.content==''
    response=TrainModelClient._response(terminal(answer=GOOD,finish_reason='length'),{'query_id':'t','response_id':'r'})
    assert response.finish_reason=='length'


def test_request_audit_failure_prevents_submission(monkeypatch):
    async def run():
        paths=[]
        def server(request):paths.append(request.url.path);return httpx.Response(200,json={})
        e,repo,_=await executor(server)
        original=e._write
        async def fail(path,*args,**kwargs):
            if path.name=='train.submit.request.json':raise OSError('audit blocked')
            return await original(path,*args,**kwargs)
        monkeypatch.setattr(e,'_write',fail)
        try:
            with pytest.raises(OSError):await e.execute('P01',{'material':'x'},task_id='t')
            assert not paths and repo.get_task('t')['status']=='failed'
        finally:await e.aclose();repo.close()
    asyncio.run(run())


def test_cancel_after_ticket_can_resume_without_new_submission():
    async def run():
        entered=asyncio.Event()
        async def server(request):
            if request.url.path.endswith('/chat_train'):return httpx.Response(200,json={'status':1,'responseId':'job'})
            entered.set();await asyncio.Event().wait()
        e,repo,path=await executor(server)
        task=asyncio.create_task(e.execute('P01',{'material':'x'},task_id='t'))
        await asyncio.wait_for(entered.wait(),5);task.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):await task
            assert e.stats['in_flight']==0 and list((path/'calls').rglob('train.ticket.json'))
        finally:await e.aclose();repo.close()
        def done(request):
            assert request.url.path.endswith('/get_resp_data')
            return httpx.Response(200,json=terminal())
        e,repo,_=await executor(done,directory=path)
        try:assert (await e.execute('P01',{'material':'x'},task_id='t'))['fields']['判断']=='未发现'
        finally:await e.aclose();repo.close()
    asyncio.run(run())


def test_doctor_and_docx_boundary_do_not_call_openai_for_train(monkeypatch):
    from argparse import Namespace
    from bid_outline.cli import doctor, invoke_reader
    root=Path(__file__).resolve().parents[2]
    args=Namespace(config=root/'configs/train.thinking.example.json',check_service=False)
    assert doctor(args)['train_enable_thinking'] is True
    assert doctor(args)['train_wire_max_tokens']==20480
    assert doctor(args)['local_generation_reserve_tokens']==8192
    args.check_service=True
    with pytest.raises(ValueError,match='health endpoint'):doctor(args)
    with pytest.raises(ValueError,match='--reader reading'):
        invoke_reader(Namespace(),config())


def test_pending_train_jobs_obey_executor_global_concurrency():
    async def run():
        both=asyncio.Event();release=asyncio.Event();submitted=set();polling=set()
        async def server(request):
            body=json.loads(request.content)
            if request.url.path.endswith('/chat_train'):
                ident=body['query_list'][0]['id'];submitted.add(ident)
                return httpx.Response(200,json={'status':1,'responseId':ident})
            ident=body['response_id'];polling.add(ident)
            if len(polling)==2:both.set()
            await release.wait()
            return httpx.Response(200,json=terminal(ident))
        from bid_outline.model.executor import CallLimitExceeded
        cfg=config(max_in_flight=2,max_run_calls=2)
        e,repo,_=await executor(server,cfg)
        jobs=[asyncio.create_task(e.execute('P01',{'material':str(i)},task_id='t'+str(i))) for i in range(3)]
        try:
            await asyncio.wait_for(both.wait(),5)
            assert e.stats['in_flight']==2 and len(submitted)==2
            release.set();results=await asyncio.gather(*jobs,return_exceptions=True)
            assert sum(isinstance(r,CallLimitExceeded) for r in results)==1
            assert e.stats['peak_in_flight']==2
        finally:release.set();await e.aclose();repo.close()
    asyncio.run(run())
