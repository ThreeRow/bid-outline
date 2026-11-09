import asyncio
from pathlib import Path
import tempfile

from bid_outline.config import AppConfig
from bid_outline.model.client import FakeModelClient
from bid_outline.workflow.runner import WorkflowRunner

NEGATIVE='[[结果开始]]\n判断：未发现\n相关主题：无\n疑点：无\n引用线索：无\n[[结果结束]]'


def test_explicit_dependencies_survive_hidden_ids_and_literal_ids_do_not_add_edges():
 async def run():
  path=Path(tempfile.mkdtemp(prefix='bid-explicit-deps-'))
  client=FakeModelClient([NEGATIVE]*3)
  r=WorkflowRunner(path,AppConfig(model='fake'),client)
  first=r.artifacts.put('source',{'text':'实际来源'})
  second=r.artifacts.put('source',{'text':'改变的实际来源'})
  unrelated=r.artifacts.put('source',{'text':'无关'})
  r.materials={'M_literal':{'artifact_id':unrelated}}
  try:
   a=await r.call('P01','正文隐藏全部材料ID',input_artifacts=[first])
   b=await r.call('P01','正文隐藏全部材料ID',input_artifacts=[first])
   assert a==b and len(client.requests)==1
   c=await r.call('P01','正文隐藏全部材料ID',input_artifacts=[second])
   assert c['task_id']!=a['task_id'] and len(client.requests)==2
   literal=await r.call('P01','正文型号M_literal',input_artifacts=[first])
   assert r.artifacts.envelope(literal['result_ref']['artifact_id'])['parents']==[first]
   assert unrelated not in r.repository.get_task(literal['task_id'])['dependencies']
  finally: await r.aclose()
 asyncio.run(run())
