"""Event-gated overlap, stable adoption and repair conflict regressions."""
import asyncio
import json
from pathlib import Path
import re
import tempfile

import pytest
from bid_outline.config import AppConfig
from bid_outline.domain import OutlineNode, OutlineConstraint, RequirementGroup, RequirementAssignment, ReviewFinding, ExtensionSuggestion
from bid_outline.model.client import ModelResponse, FakeModelClient
from bid_outline.model.task_specs import unescape_material
from bid_outline.workflow.runner import WorkflowRunner
from bid_outline.workflow.repair_scope import RepairScope, repair_waves


def wrap(body): return '[[结果开始]]\n'+body+'\n[[结果结束]]'
def record(kind, **fields): return f'[[{kind}开始]]\n'+'\n'.join(f'{k}：{v}' for k,v in fields.items())+f'\n[[{kind}结束]]'
def node(key,parent,title,basis,order=1):
 return record('节点',节点=key,父节点=parent,同级顺序=str(order),标题=title,依据=basis,形成方式='有依据归纳',说明='无')
def data(request):
 return {k:unescape_material(v) for k,v in re.findall(r'\[\[输入数据开始\]\]\n([^：\n]+)：\n(.*?)\n\[\[输入数据结束\]\]',request['messages'][1]['content'],re.S)}
def section(d,key): return json.JSONDecoder().raw_decode(d['完整材料包'].split(key+'：\n',1)[1])[0]

class GateClient:
 def __init__(self,target,handler,reverse=False):
  self.target=target;self.handler=handler;self.reverse=reverse
  self.entered=asyncio.Event();self.second_finished=asyncio.Event();self.requests=[];self.active=0;self.peak=0;self.count=0
 async def generate(self,request):
  self.requests.append(request)
  if request['_task_type']==self.target:
   self.count+=1; ordinal=self.count;self.active+=1;self.peak=max(self.peak,self.active)
   if self.count==2: self.entered.set()
   await self.entered.wait()  # Sequential scheduling would deadlock the test.
   if self.reverse and ordinal==1: await self.second_finished.wait()
   result=self.handler(request)
   if ordinal==2: self.second_finished.set()
   self.active-=1
   return ModelResponse(wrap(result))
  return ModelResponse(wrap(self.handler(request)))
 async def aclose(self): pass


def runner(client,**kw):
 return WorkflowRunner(Path(tempfile.mkdtemp(prefix='bid-parallel-workflow-')),
                       AppConfig(prompt_view='legacy-json', model='fake',context_tokens=100000,service_context_tokens=100000,max_in_flight=2,repair_rounds=1,format_retries=0,**kw),client)

def requirements(): return [RequirementGroup(requirement_id=f'r{i}',categories=['B'],topic=f'主题{i}',content=f'完整条件{i}') for i in range(2)]


def test_p07_overlaps_but_p08_consumes_in_original_order():
 async def run():
  p08=[]
  def handler(q):
   d=data(q)
   if q['_task_type']=='P07': return record('结构依据',局部编号='S01',要求依据=d['要求ID'],性质='非固定清单',原文表达='结构',结构含义='结构',疑点='无')
   assert q['_task_type']=='P08'
   req=section(d,'本批要求')[0]['requirement_id'];p08.append(req)
   old=section(d,'当前骨架')
   if old:
    assert len(p08)==2 and old['nodes']
    n=old['nodes'][0];text=node(n['node_id'],'ROOT','基础',n['basis_requirement_ids'][0])
   else: text=node('N01','ROOT','基础',req)
   return '状态：已形成候选骨架\n骨架：\n'+text+'\n调整说明：保留\n待核查事项：无'
  client=GateClient('P07',handler,reverse=True);r=runner(client,chunk_chars=1)
  r.requirements=requirements();r._requirement_order={'r0':(0,),'r1':(1,)}
  try:
   await asyncio.wait_for(r.build_skeleton(),5)
   assert client.peak==2 and p08==['r0','r1'] and not r.unprocessed
   assert r.executor.stats['peak_in_flight']<=2
  finally: await r.aclose()
 asyncio.run(run())


def test_p10_overlaps_then_one_central_p08():
 async def run():
  def handler(q):
   d=data(q)
   if q['_task_type']=='P10': return record('调整建议',要求=d['必须逐项响应ID'],处理='保持待定',目标或父节点='无',建议标题='无',位置说明='无',承接内容='未定',依据与限制='保留条件')
   assert q['_task_type']=='P08'
   assert len(section(d,'统一调整建议'))==2
   return '状态：信息不足\n骨架：无\n调整说明：无\n待核查事项：缺依据'
  client=GateClient('P10',handler,reverse=True);r=runner(client)
  r.requirements=requirements();r.commit([OutlineNode(node_id='root',title='基础',basis_requirement_ids=['r0'])],'initial')
  r.assignments=[RequirementAssignment(requirement_id=req.requirement_id,outline_version_id=r.version.version_id,status='未承接') for req in r.requirements]
  try:
   await asyncio.wait_for(r.resolve_unassigned(),5)
   assert client.peak==2 and [q['_task_type'] for q in client.requests].count('P08')==1
  finally: await r.aclose()
 asyncio.run(run())


def test_p13_independent_cross_title_candidates_overlap():
 async def run():
  def handler(q):
   d=data(q)
   if q['_task_type']=='P13': return f"结论：合理交叉\n涉及节点：{d['已有目录节点ID']}\n涉及要求：无\n依据：范围不同\n建议：保留"
   assert q['_task_type']=='P15';return '检查结论：未发现问题'
  client=GateClient('P13',handler);r=runner(client)
  nodes=[OutlineNode(node_id='a',title='甲章'),OutlineNode(node_id='b',title='乙章',order=2)]
  nodes += [OutlineNode(node_id=f'{p}{i}',parent_id=p,title=title,order=i+1) for p in ('a','b') for i,title in enumerate(('部署','运维'))]
  r.commit(nodes,'initial')
  try:
   assert await asyncio.wait_for(r.review(),5)==[]
   assert client.peak==2
  finally: await r.aclose()
 asyncio.run(run())


def test_same_chapter_p18_overlaps_after_sequential_p17_batches():
 async def run():
  p17=[]
  def handler(q):
   d=data(q)
   if q['_task_type']=='P17':
    p17.append(d)
    previous=section(d,'已有建议')
    assert len(previous)==len(p17)-1
    return '结果：有建议\n'+record('建议',局部编号='E01',对应章节='root',建议主题='建议'+str(len(p17)),建议内容='可选',来源性质='常规经验',相关依据='常规经验',适用理由与条件='条件允许')
   assert q['_task_type']=='P18'
   assert len(p17)==2
   return record('建议检查',建议=d['必须逐项响应ID'],结论='保留',依据='有条件',修改意见='无')
  client=GateClient('P18',handler,reverse=True);r=runner(client,chunk_chars=1)
  r.requirements=requirements();r.commit([OutlineNode(node_id='root',title='基础',basis_requirement_ids=['r0'])],'initial')
  r.assignments=[RequirementAssignment(requirement_id=req.requirement_id,outline_version_id=r.version.version_id,status='已分配',targets=[{'node_id':'root','scope':'全部'}]) for req in r.requirements]
  try:
   await asyncio.wait_for(r.extend(),5)
   assert client.peak==2 and len(r.extensions)==2 and all(s.review_status=='保留' for s in r.extensions)
  finally: await r.aclose()
 asyncio.run(run())


@pytest.mark.parametrize('failed_branch',[False,True])
def test_p16_disjoint_requests_overlap_and_ordered_commits_replay(failed_branch):
 async def run():
  def handler(q):
   d=data(q)
   if q['_task_type']=='P16':
    boundary=section(d,'边界父节点')['node_id']
    if failed_branch and boundary=='a': return 'invalid response'
    req=section(d,'范围内全部要求')[0]['requirement_id']
    return '处理结论：已修正\n替换目录：\n'+node('N01',boundary,'修复'+boundary,req)+'\n问题处理说明：完成\n仍需处理：无'
   if q['_task_type']=='P14': return record('覆盖检查',要求=d['必须逐项响应ID'],结论='已覆盖',承接节点='a' if d['必须逐项响应ID']=='r0' else 'b',承接说明='覆盖',未覆盖或不明确内容='无',修正建议='无')
   assert q['_task_type']=='P15';return '检查结论：未发现问题'
  client=GateClient('P16',handler,reverse=True);r=runner(client)
  path,config=r.run_dir,r.config
  async def setup(active):
   active.requirements=requirements()
   active.commit([OutlineNode(node_id='a',title='甲章',basis_requirement_ids=['r0']),OutlineNode(node_id='b',title='乙章',order=2,basis_requirement_ids=['r1'])],'initial')
   active.assignments=[RequirementAssignment(requirement_id=f'r{i}',outline_version_id=active.version.version_id,status='已分配',targets=[{'node_id':p,'scope':'全部'}]) for i,p in enumerate(('a','b'))]
   original=active.review;first=True
   async def review(affected=None):
    nonlocal first
    if first:
     first=False
     return [ReviewFinding(finding_id=f'f{i}',kind='coverage',node_ids=[p],requirement_ids=[f'r{i}']) for i,p in enumerate(('a','b'))]
    return await original(affected)
   active.review=review
  await setup(r)
  try:
   await asyncio.wait_for(r.review_and_repair(),5)
   assert client.peak==2 and r.executor.stats['peak_in_flight']<=2
   assert any(n.title=='修复b' for n in r.version.nodes)
   assert any(f.finding_id=='f0' and f.status=='open' for f in r.findings)==failed_branch
   version=r.version.version_id
  finally: await r.aclose()
  if not failed_branch:
   replay=WorkflowRunner(path,config,FakeModelClient([]));await setup(replay)
   try:
    await replay.review_and_repair()
    assert replay.version.version_id==version and not replay.executor.client.requests
   finally: await replay.aclose()
 asyncio.run(run())


def test_repair_wave_conflicts_include_ancestor_overlap_and_shared_constraints():
 from bid_outline.workflow.outline import make_version
 nodes=[OutlineNode(node_id='a',title='a'),OutlineNode(node_id='b',title='b',order=2),OutlineNode(node_id='child',parent_id='a',title='child')]
 constraint=OutlineConstraint(constraint_id='joint',kind='preserve',origin='文件明确',affected_node_ids=['a','b'])
 base=make_version(nodes,None,'initial',[constraint])
 def item(root,scope): return (RepairScope(root,root,[],[],[],[],set()),'',set(scope),[],[],{})
 assert repair_waves([item('a',['a','child']),item('child',['child'])],base)==[[0],[1]]
 assert repair_waves([item('a',['a','child']),item('b',['b'])],base)==[[0],[1]]


def test_runner_rejects_a_stale_duplicate_entry_and_retains_cross_finding(monkeypatch):
 async def run():
  import bid_outline.workflow.repair_scope as scopes
  original=scopes.repair_waves
  def duplicate(entries,base):
   entries.append(entries[0])  # Simulate a faulty scheduler yielding overlapping work.
   return original(entries,base)
  monkeypatch.setattr(scopes,'repair_waves',duplicate)
  r=runner(FakeModelClient([]));r.requirements=requirements()[:1]
  r.commit([OutlineNode(node_id='root',title='根',basis_requirement_ids=['r0'])],'initial')
  r.assignments=[RequirementAssignment(requirement_id='r0',outline_version_id=r.version.version_id,status='已分配',targets=[{'node_id':'root','scope':'全部'}])]
  first=True
  async def review(*args):
   nonlocal first
   if first:
    first=False
    return [ReviewFinding(finding_id='cross',kind='cross_branch',node_ids=['root'],requirement_ids=['r0'])]
   return []
  r.review=review
  async def call(task,material,**kwargs):
   assert task=='P16'
   return {'task_id':'repair','fields':{'处理结论':'已修正','替换目录':''},'records':{'节点':[
    {'节点':'N01','父节点':'root','同级顺序':'1','标题':'新方案','依据':'r0','形成方式':'有依据归纳'}]}}
  r.call=call
  try:
   await r.review_and_repair()
   assert len(r.version.nodes)==2
   assert any(i['kind']=='repair_rejected' and 'stale' in i['detail'] for i in r.issues)
   assert any(f.finding_id=='cross' and f.status=='open' for f in r.findings)
   assert any(i['kind']=='cross_branch_repair_pending' for i in r.issues)
  finally: await r.aclose()
 asyncio.run(run())


def test_cancelling_parallel_stage_stops_queued_work_and_drains_audit():
 async def run():
  entered=asyncio.Event();count=0
  class Client:
   async def generate(self,q):
    nonlocal count
    count+=1
    if count==2: entered.set()
    await asyncio.Event().wait()
   async def aclose(self): pass
  r=runner(Client(),chunk_chars=1,workers=2,queue_capacity=1)
  r.requirements=[RequirementGroup(requirement_id=f'r{i}',categories=['B'],topic='要求',content='完整内容') for i in range(6)]
  r._requirement_order={f'r{i}':(i,) for i in range(6)}
  task=asyncio.create_task(r.build_skeleton())
  try:
   await asyncio.wait_for(entered.wait(),5)
   task.cancel()
   with pytest.raises(asyncio.CancelledError): await task
   assert count==2 and r.executor.stats['in_flight']==0 and not r.executor.io.pending
  finally: await r.aclose()
 asyncio.run(run())
