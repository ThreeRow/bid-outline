import asyncio
import json
from pathlib import Path
import tempfile
import threading

import pytest
from bid_outline.config import AppConfig
from bid_outline.model.client import FakeModelClient, ModelResponse
from bid_outline.model.executor import ModelExecutor, CallLimitExceeded
from bid_outline.storage import RunRepository, ArtifactStore

GOOD='[[结果开始]]\n判断：未发现\n相关主题：无\n疑点：无\n引用线索：无\n[[结果结束]]'

def make(client=None, **config):
 path=Path(tempfile.mkdtemp(prefix='bid-efficient-execution-'))
 repo=RunRepository(path)
 executor=ModelExecutor(AppConfig(model='fake',**config),client or FakeModelClient([GOOD]*50),repo,ArtifactStore(path))
 return path,repo,executor


def test_normal_calls_do_not_scan_or_read_historical_request_files(monkeypatch):
 async def run():
  path,repo,e=make()
  original=Path.read_text
  def no_history(self,*a,**kw):
   if self.name=='request.json': pytest.fail('normal task read historical request')
   return original(self,*a,**kw)
  def no_scan(*args,**kwargs): pytest.fail('normal task scanned history')
  monkeypatch.setattr(Path,'rglob',no_scan)
  monkeypatch.setattr(Path,'read_text',no_history)
  try:
   for i in range(20): await e.execute('P01',{'material':str(i)},task_id=str(i))
   assert e.request_scan_count==1 and e.request_read_count==0
   assert e.recorded_count==20 and e.reserved_calls==20
   await e.flush_navigation()
   assert (path/'calls.md').read_text().count('| yes |')==20
  finally: await e.aclose();repo.close()
 asyncio.run(run())


def test_slow_audit_allows_event_loop_and_cancellation_drains_durable_request(monkeypatch):
 async def run():
  import bid_outline.model.executor as module
  path,repo,e=make()
  entered,release=threading.Event(),threading.Event()
  original=module.write_once
  def slow(path,value,**kwargs):
   if path.name=='request.json':
    entered.set()
    assert release.wait(5)
   return original(path,value,**kwargs)
  monkeypatch.setattr(module,'write_once',slow)
  task=asyncio.create_task(e.execute('P01',{'material':'slow'},task_id='slow'))
  try:
   assert await asyncio.to_thread(entered.wait,5)
   ticks=[]
   asyncio.get_running_loop().call_soon(ticks.append,'alive')
   await asyncio.sleep(0)
   assert ticks==['alive'] and not e.client.requests
   task.cancel()
   await asyncio.sleep(0)
   assert not task.done()  # fsync worker still owns its operation
   release.set()
   with pytest.raises(asyncio.CancelledError): await task
   assert not e.io.pending and not e.client.requests
   assert e.recorded_count==1 and len(e.requests_by_task['slow'])==1
   assert repo.list_attempts('slow')[0]['error_kind']=='cancelled'
   assert repo.list_attempts('slow')[0]['sent'] is False
  finally:
   release.set()
   await e.aclose();repo.close()
 asyncio.run(run())


def test_partial_request_failure_never_indexes_as_complete_and_startup_reports_corruption(monkeypatch):
 async def run():
  import bid_outline.model.executor as module
  path,repo,e=make()
  original=module.write_once
  def broken(path,value,**kwargs):
   if path.name=='request.json':
    path.write_text('{partial')
    raise OSError('partial write')
   return original(path,value,**kwargs)
  monkeypatch.setattr(module,'write_once',broken)
  try:
   with pytest.raises(OSError,match='partial write'): await e.execute('P01',{'material':'x'},task_id='x')
   assert not e.client.requests and e.recorded_count==0
   assert not e.requests_by_task and e.reserved_calls==1
  finally:
   monkeypatch.setattr(module,'write_once',original)
   await e.aclose();repo.close()
  other=RunRepository(path)
  try:
   with pytest.raises(OSError,match='corrupt startup request audit'):
    ModelExecutor(AppConfig(model='fake'),FakeModelClient([]),other,ArtifactStore(path))
  finally: other.close()
 asyncio.run(run())


def test_startup_recovers_response_with_missing_sqlite_attempt(monkeypatch):
 async def run():
  path,repo,e=make()
  original=repo.commit_task
  repo.commit_task=lambda *a: (_ for _ in ()).throw(OSError('crash before commit'))
  with pytest.raises(OSError): await e.execute('P01',{'material':'x'},task_id='x')
  repo.commit_task=original
  # Controlled crash fixture: durable calls survived but transaction index did not.
  with repo.connection:
   repo.connection.execute('DELETE FROM attempts')
   repo.connection.execute('DELETE FROM tasks')
  await e.aclose();repo.close()
  other=RunRepository(path);client=FakeModelClient([])
  recovered=ModelExecutor(AppConfig(model='fake'),client,other,ArtifactStore(path))
  try:
   await recovered.execute('P01',{'material':'x'},task_id='x')
   assert recovered.request_scan_count==1 and recovered.request_read_count==1
   assert not client.requests and other.get_task('x')['status']=='succeeded'
   assert other.list_attempts('x')[0]['reason']=='response_recovery'
  finally: await recovered.aclose();other.close()
 asyncio.run(run())


def test_reserved_cap_with_event_gated_http_and_close_drains():
 async def run():
  entered=asyncio.Event(); release=asyncio.Event(); count=0
  class Client:
   def __init__(self): self.closed=False
   async def generate(self,request):
    nonlocal count
    count+=1
    if count==2: entered.set()
    await release.wait()
    return ModelResponse(GOOD)
   async def aclose(self): self.closed=True
  client=Client();path,repo,e=make(client,max_in_flight=2,max_run_calls=2)
  tasks=[asyncio.create_task(e.execute('P01',{'material':str(i)},task_id=str(i))) for i in range(6)]
  try:
   await asyncio.wait_for(entered.wait(),5)
   assert e.stats['in_flight']==2 and e.reserved_calls==2
   release.set()
   results=await asyncio.gather(*tasks,return_exceptions=True)
   assert sum(isinstance(v,CallLimitExceeded) for v in results)==4 and count==2
   await e.aclose()
   assert client.closed and not e.io.pending and e.stats['in_flight']==0
   assert (path/'calls.md').read_text().count('| yes |')==2
  finally: release.set();repo.close()
 asyncio.run(run())


def test_navigation_generation_survives_updates_during_flush(monkeypatch):
 async def run():
  import bid_outline.model.executor as module
  path,repo,e=make();await e.execute('P01',{'material':'x'},task_id='x')
  entered,release=threading.Event(),threading.Event();original=module.publish
  def slow(*args):
   entered.set();assert release.wait(5);return original(*args)
  monkeypatch.setattr(module,'publish',slow)
  task=asyncio.create_task(e.flush_navigation())
  try:
   assert await asyncio.to_thread(entered.wait,5)
   e.navigation_generation+=1
   release.set();await task
   assert e.navigation_published<e.navigation_generation
   monkeypatch.setattr(module,'publish',original)
   await e.flush_navigation()
   assert e.navigation_published==e.navigation_generation
  finally: release.set();await e.aclose();repo.close()
 asyncio.run(run())


def test_adoption_artifact_failure_is_not_success_and_saved_response_recovers(monkeypatch):
 async def run():
  path,repo,e=make();original=e.artifacts.put
  def fail(kind,*args,**kwargs):
   if kind=='model_result': raise OSError('adoption artifact failed')
   return original(kind,*args,**kwargs)
  monkeypatch.setattr(e.artifacts,'put',fail)
  try:
   with pytest.raises(OSError,match='adoption artifact'): await e.execute('P01',{'material':'x'},task_id='x')
   assert repo.get_task('x')['status']=='failed' and 'accepted_artifact_id' not in repo.get_task('x')
   assert len(e.client.requests)==1 and not e.io.pending
   monkeypatch.setattr(e.artifacts,'put',original)
   await e.execute('P01',{'material':'x'},task_id='x')
   assert repo.get_task('x')['status']=='succeeded' and len(e.client.requests)==1
  finally: await e.aclose();repo.close()
 asyncio.run(run())


def test_close_navigation_failure_still_closes_client_and_drains(monkeypatch):
 async def run():
  import bid_outline.model.executor as module
  class Client(FakeModelClient):
   def __init__(self): super().__init__([GOOD]);self.closed=False
   async def aclose(self): self.closed=True
  client=Client();path,repo,e=make(client)
  await e.execute('P01',{'material':'x'},task_id='x')
  def fail(*args): raise OSError('navigation failed')
  monkeypatch.setattr(module,'publish',fail)
  try:
   with pytest.raises(OSError,match='navigation'): await e.aclose()
   assert client.closed and not e.io.pending
   assert repo.get_task('x')['status']=='succeeded'  # derived navigation cannot undo adoption
  finally: repo.close()
 asyncio.run(run())
