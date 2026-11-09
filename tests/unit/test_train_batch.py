import asyncio
import json

import httpx
import pytest

from bid_outline.config import AppConfig
from bid_outline.model.train_client import TrainModelClient
from bid_outline.model.executor import ModelExecutor
from bid_outline.storage import ArtifactStore, RunRepository


def configuration():
    return AppConfig(provider='train',base_url='http://fake',model='fake',train_batch_size=2,
        max_in_flight=2,train_poll_interval_seconds=.001,train_poll_timeout_seconds=.02,
        transport_retries=0,thinking_default=False)


async def executor(path, server):
    cfg=configuration();client=TrainModelClient(cfg)
    await client.http.aclose()
    client.http=httpx.AsyncClient(transport=httpx.MockTransport(server))
    repo=RunRepository(path)
    return ModelExecutor(cfg,client,repo,ArtifactStore(path)),repo


def test_batch_out_of_order_and_one_failed_item_does_not_erase_success(tmp_path):
    async def run():
        submissions=[]
        def server(req):
            body=json.loads(req.content)
            if req.url.path.endswith('chat_train'):
                submissions.append(body)
                return httpx.Response(200,json={'status':1,'responseId':'ticket'})
            return httpx.Response(200,json={'status':1,'msg':'完成','data':[
                {'id':'two','answer':None,'is_success':False},
                {'id':'one','answer':'完整段落','is_success':True}]})
        e,repo=await executor(tmp_path,server)
        try:
            outputs=await asyncio.gather(e.execute('N01-A',{'material':'甲'},task_id='one'),
                e.execute('N01-B',{'material':'乙'},task_id='two'),return_exceptions=True)
            assert outputs[0]['text']=='完整段落' and isinstance(outputs[1],Exception)
            assert len(submissions)==1 and len(submissions[0]['query_list'])==2
            assert repo.get_task('one')['status']=='succeeded' and repo.get_task('two')['status']=='failed'
            assert len(list((tmp_path/'train_batches').glob('batch-*/ticket.json')))==1
        finally:
            await e.aclose();repo.close()
    asyncio.run(run())


def test_batch_poll_timeout_resumes_shared_ticket_without_resubmitting(tmp_path):
    async def run():
        submitted=[];finished=False
        def server(req):
            if req.url.path.endswith('chat_train'):
                submitted.append(1)
                return httpx.Response(200,json={'status':1,'responseId':'shared'})
            return httpx.Response(200,json={'status':1,'msg':'完成' if finished else '任务进行中',
                'data':[{'id':i,'is_success':True,'answer':i} for i in ('two','one')] if finished else []})
        e,repo=await executor(tmp_path,server)
        tasks=[('one','N01-A'),('two','N01-B')]
        try:
            result=await asyncio.gather(*(e.execute(t,{'material':i},task_id=i) for i,t in tasks),return_exceptions=True)
            assert all(isinstance(x,Exception) for x in result)
        finally:
            await e.aclose();repo.close()
        finished=True
        e,repo=await executor(tmp_path,server)
        try:
            result=await asyncio.gather(*(e.execute(t,{'material':i},task_id=i) for i,t in tasks))
            assert [x['text'] for x in result]==['one','two']
            assert len(submitted)==1
        finally:
            await e.aclose();repo.close()
    asyncio.run(run())


def test_uncertain_batch_submission_never_resubmits(tmp_path):
    async def run():
        submissions=[]
        def server(req):
            submissions.append(1)
            raise httpx.ConnectError('unknown outcome')
        for _ in range(2):
            e,repo=await executor(tmp_path,server)
            try:
                result=await asyncio.gather(e.execute('N01-A',{'material':'a'},task_id='one'),
                    e.execute('N01-B',{'material':'b'},task_id='two'),return_exceptions=True)
                assert all(isinstance(x,Exception) for x in result)
            finally:
                await e.aclose();repo.close()
        assert len(submissions)==1
    asyncio.run(run())


def test_shared_receipt_recovers_when_per_task_ticket_audit_fails(tmp_path):
    async def run():
        submitted=[]
        def server(req):
            if req.url.path.endswith('chat_train'):
                submitted.append(1)
                return httpx.Response(200,json={'status':1,'responseId':'saved'})
            return httpx.Response(200,json={'status':1,'msg':'完成','data':[
                {'id':i,'is_success':True,'answer':'结果'+i} for i in ('one','two')]})
        e,repo=await executor(tmp_path,server);original=e._write
        async def fail(path,*args,**kwargs):
            if path.name=='train.ticket.json':raise OSError('ticket audit failure')
            return await original(path,*args,**kwargs)
        e._write=fail
        try:
            outputs=await asyncio.gather(e.execute('N01-A',{'material':'a'},task_id='one'),
                e.execute('N01-B',{'material':'b'},task_id='two'),return_exceptions=True)
            assert all(isinstance(x,Exception) for x in outputs)
            assert not any(t['status']=='succeeded' for t in repo.list_tasks())
        finally:
            await e.aclose();repo.close()
        e,repo=await executor(tmp_path,server)
        try:
            outputs=await asyncio.gather(e.execute('N01-A',{'material':'a'},task_id='one'),
                e.execute('N01-B',{'material':'b'},task_id='two'))
            assert [x['text'] for x in outputs]==['结果one','结果two']
            assert len(submitted)==1
        finally:
            await e.aclose();repo.close()
    asyncio.run(run())


@pytest.mark.parametrize('rows', [
    [{'id':'one','is_success':True,'answer':'ok'}],
    [{'id':'one','is_success':True,'answer':'ok'}, {'id':'two','is_success':True,'answer':''}],
])
def test_missing_or_empty_member_is_not_a_success_for_that_task(tmp_path, rows):
    async def run():
        def server(req):
            return httpx.Response(200,json={'status':1,'responseId':'job'} if req.url.path.endswith('chat_train')
                else {'status':1,'msg':'完成','data':rows})
        e,repo=await executor(tmp_path,server)
        try:
            result=await asyncio.gather(e.execute('N01-A',{'material':'a'},task_id='one'),
                e.execute('N01-B',{'material':'b'},task_id='two'),return_exceptions=True)
            assert result[0]['text']=='ok' and isinstance(result[1],Exception)
            assert repo.get_task('two')['status']=='failed'
        finally:
            await e.aclose();repo.close()
    asyncio.run(run())


def test_cancel_batch_preserves_ticket_and_resume_shares_one_poll(tmp_path):
    async def run():
        entered=asyncio.Event();submits=[];polls=[]
        async def server(req):
            if req.url.path.endswith('chat_train'):
                submits.append(1)
                return httpx.Response(200,json={'status':1,'responseId':'cancel-ticket'})
            entered.set()
            await asyncio.Event().wait()
        e,repo=await executor(tmp_path,server)
        active=[asyncio.create_task(e.execute(t,{'material':i},task_id=i)) for i,t in [('one','N01-A'),('two','N01-B')]]
        await asyncio.wait_for(entered.wait(),5)
        await e.aclose()
        await asyncio.gather(*active,return_exceptions=True)
        repo.close()
        async def resumed(req):
            assert req.url.path.endswith('get_resp_data')
            polls.append(1)
            await asyncio.sleep(.001)
            return httpx.Response(200,json={'status':1,'msg':'完成','data':[
                {'id':i,'is_success':True,'answer':i} for i in ('one','two')]})
        e,repo=await executor(tmp_path,resumed)
        try:
            values=await asyncio.gather(*(e.execute(t,{'material':i},task_id=i) for i,t in [('one','N01-A'),('two','N01-B')]))
            assert [v['text'] for v in values]==['one','two']
            assert len(submits)==1 and len(polls)==1
        finally:
            await e.aclose();repo.close()
    asyncio.run(run())
