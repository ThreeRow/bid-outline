import pytest
from bid_outline.model.parsers import parse_output, ParseError
from bid_outline.workflow.outline import make_version
from bid_outline.domain import OutlineNode, OutlineConstraint

def wrap(x): return '[[结果开始]]\n'+x+'\n[[结果结束]]'
def test_p16_actions_distinguish_noop_replace_clear():
 for action in ('无','清空'):
  r=parse_output('P16',wrap(f'处理结论：已修正\n替换目录：{action}\n问题处理说明：{action}\n仍需处理：无'))
  assert r['fields']['替换目录']==action
 node='[[节点开始]]\n节点：N01\n父节点：root\n同级顺序：1\n标题：新\n依据：r\n形成方式：有依据归纳\n说明：无\n[[节点结束]]'
 r=parse_output('P16',wrap('处理结论：已修正\n替换目录：\n'+node+'\n问题处理说明：替换\n仍需处理：无'),allowed_ids=['root','r'],id_groups={'nodes':['root'],'requirements':['r']})
 assert r['records']['节点']

import asyncio
import json
from pathlib import Path
import tempfile
from bid_outline.config import AppConfig
from bid_outline.domain import RequirementGroup, RequirementAssignment, ReviewFinding
from bid_outline.model.client import FakeModelClient, ModelResponse
from bid_outline.model.task_specs import unescape_material
from bid_outline.workflow.runner import WorkflowRunner
from bid_outline.workflow.source_lists import bind_source_list, source_list
from bid_outline.workflow.outline import replace_branch


def decoded(request):
 import re
 return {k:unescape_material(v) for k,v in re.findall(r'\[\[输入数据开始\]\]\n([^：\n]+)：\n(.*?)\n\[\[输入数据结束\]\]',request['messages'][1]['content'],re.S)}

def section(data,name):
 return json.JSONDecoder().raw_decode(data['完整材料包'].split(name+'：\n',1)[1])[0]

def rec(kind, **fields):
 return f'[[{kind}开始]]\n'+'\n'.join(f'{k}：{v}' for k,v in fields.items())+f'\n[[{kind}结束]]'

def evidence(mid='m', rid='r', start='清单开始', stop='清单结束', members='1|甲方案\n1|乙方案'):
 return {'局部编号':'S01','要求依据':rid,'性质':'非固定清单','原文表达':'按清单','结构含义':'保留','疑点':'无',
         '清单材料':mid,'清单起点':start,'清单终点':stop,'清单成员':members}


@pytest.mark.parametrize('reverse', [False,True])
def test_real_p07_p08_two_batch_list_order_and_recovery(reverse):
 async def run():
  directory=Path(tempfile.mkdtemp(prefix='bid-sequence-protocol-'))
  class Client:
   def __init__(self): self.calls=[]; self.round=0
   async def aclose(self): pass
   async def generate(self,request):
    self.calls.append(request); task=request['_task_type']; data=decoded(request)
    if task=='P07':
     return ModelResponse(wrap(rec('结构依据',**evidence(rid=data['要求ID']))))
    assert task=='P08'; self.round+=1
    req=section(data,'本批要求')[0]['requirement_id']; old=section(data,'当前骨架')
    rows=[]
    if old:
     n=old['nodes'][0]
     rows.append(rec('节点',节点=n['node_id'],父节点='ROOT',同级顺序='2' if reverse else '1',标题='甲方案',依据=n['basis_requirement_ids'][0],形成方式='原文明示',说明='无'))
    rows.append(rec('节点',节点='N01',父节点='ROOT',同级顺序='1' if self.round==1 or reverse else '2',标题='甲方案' if self.round==1 else '乙方案',依据=req,形成方式='原文明示',说明='无'))
    return ModelResponse(wrap('状态：已形成候选骨架\n骨架：\n'+'\n'.join(rows)+'\n调整说明：增补\n待核查事项：无'))
  client=Client(); config=AppConfig(prompt_view='legacy-json', model='fake',context_tokens=100000,service_context_tokens=100000,chunk_chars=1,format_retries=0)
  r=WorkflowRunner(directory,config,client)
  def setup(r):
   r.requirements=[RequirementGroup(requirement_id='r'+str(i),categories=['A'],topic=t,content=t,material_refs=['m']) for i,t in enumerate(['甲方案','乙方案'])]
   r._requirement_order={'r0':(0,), 'r1':(1,)}
   r.materials={'m':{'material_id':'m','text':'清单开始\n甲方案\n乙方案\n清单结束','kind':'refined_text','uncertainties':'无','source_order':[0,0,0]}}
  setup(r)
  try:
   await r.build_skeleton()
   if reverse:
    assert r.unprocessed and 'relative order' in r.unprocessed[0]['error']
    assert [n.title for n in r.version.nodes]==['甲方案']
   else:
    assert not r.unprocessed
    sequence=next(c for c in r.version.constraints if c.kind=='source_list_sequence')
    assert len(sequence.affected_node_ids)==2 and sequence.source_identity
    with pytest.raises(ValueError,match='order'):
     r.commit([n.model_copy(update={'order':3-n.order}) for n in r.version.nodes],'reverse')
    # B/C insertion changes numeric positions, not the relative A order.
    inserted=[r.version.nodes[0],r.version.nodes[1].model_copy(update={'order':3}),OutlineNode(node_id='extra',title='额外',order=2)]
    r.commit(inserted,'insert')
    original=r.repository.get_outline_version(sequence_version:=r.version.parent_version_id)
    assert original['constraints']
   version=r.version.version_id
  finally: await r.aclose()
  if not reverse:
   replay=WorkflowRunner(directory,config,FakeModelClient([])); setup(replay)
   try:
    await replay.build_skeleton()
    assert replay.version.version_id==sequence_version
    assert len(next(c for c in replay.version.constraints if c.kind=='source_list_sequence').affected_node_ids)==2
   finally: await replay.aclose()
 asyncio.run(run())


def test_distinct_lists_same_material_have_independent_order_and_constraint_identity():
 materials={'m':{'text':'甲起\n甲方案\n甲终\n乙起\n乙方案\n乙终'}}
 reqs=[RequirementGroup(requirement_id='r',categories=['A'],topic='格式',content='格式',material_refs=['m'])]
 nodes=[OutlineNode(node_id='a',title='甲方案',basis_requirement_ids=['r']),OutlineNode(node_id='b',title='乙方案',order=2,basis_requirement_ids=['r'])]
 ca=bind_source_list(evidence(start='甲起',stop='甲终',members='1|甲方案'),materials,reqs,nodes,[])
 cb=bind_source_list(evidence(start='乙起',stop='乙终',members='1|乙方案'),materials,reqs,nodes,[ca])
 assert ca.source_identity!=cb.source_identity
 base=make_version(nodes,None,'initial',[ca,cb])
 reordered=[nodes[0].model_copy(update={'order':2}),nodes[1].model_copy(update={'order':1})]
 assert make_version(reordered,base,'independent')
 assert make_version(nodes,None,'initial',[ca]).version_id!=base.version_id
 # Partial subsequent evidence must preserve all confirmed members.
 whole={'m':{'text':'清单开始\n甲方案\n乙方案\n清单结束'}}
 combined=bind_source_list(evidence(),whole,reqs,nodes,[])
 partial=bind_source_list(evidence(members='1|甲方案'),whole,reqs,nodes,[combined])
 assert partial.sequence_members==combined.sequence_members


@pytest.mark.parametrize('text,members', [('清单开始\n甲方案\n甲方案\n清单结束','1|甲方案'),('清单开始\n甲方案\n乙方案\n清单结束','1|乙方案\n1|甲方案')])
def test_ambiguous_or_contradictory_list_evidence_is_not_guessed(text,members):
 with pytest.raises(ValueError): source_list(evidence(members=members),{'m':{'text':text}})


@pytest.mark.parametrize('blocked', ['none','protected','closed','basis','assigned'])
@pytest.mark.parametrize('failed_review',[False,True])
def test_clear_action_keeps_root_blocks_obligations_and_rechecks_parent(blocked,failed_review):
 async def run():
  r=WorkflowRunner(Path(tempfile.mkdtemp(prefix='bid-clear-protocol-')),AppConfig(prompt_view='legacy-json', model='fake',context_tokens=100000,service_context_tokens=100000,repair_rounds=1),FakeModelClient([]))
  r.requirements=[RequirementGroup(requirement_id='r',topic='父义务',content='父级完整条件')]
  root=OutlineNode(node_id='root',title='根',basis_requirement_ids=['r'])
  child=OutlineNode(node_id='child',parent_id='root',title='无依据多余项',basis_requirement_ids=['r'] if blocked=='basis' else [])
  constraints=[OutlineConstraint(constraint_id='keep',kind='source_declared_structure',origin='文件明确',affected_node_ids=['child'])] if blocked=='protected' else []
  if blocked=='closed': constraints=[OutlineConstraint(constraint_id='closed',kind='no_add_children',origin='文件明确',parent_node_id='child',allowed_child_titles=[])]
  r.commit([root,child],'initial',constraints)
  r.assignments=[RequirementAssignment(requirement_id='r',outline_version_id=r.version.version_id,status='已分配',targets=[{'node_id':'child' if blocked=='assigned' else 'root','scope':'完整'}])]
  first=True; review=r.review; calls=[]
  async def check(affected=None):
   nonlocal first
   if first:
    first=False; return [ReviewFinding(finding_id='f',kind='无依据内容',node_ids=['child'],requirement_ids=['r'])]
   return await review(affected)
  r.review=check
  async def call(task, material, **kwargs):
   calls.append(task)
   if task=='P16': return {**parse_output('P16',wrap('处理结论：已修正\n替换目录：清空\n问题处理说明：删除无依据下级\n仍需处理：无')),'task_id':'clear'}
   if task=='P14':
    assert kwargs['expected']==['r']
    if failed_review: r.unprocessed.append({'task_type':'P14'}); return None
    return {'task_id':'coverage','records':{'覆盖检查':[{'要求':'r','承接节点':'root','结论':'已覆盖','未覆盖或不明确内容':'无'}]}}
   assert task=='P15'
   return {'task_id':'structure','fields':{'检查结论':'未发现问题'},'records':{}}
  r.call=call
  try:
   await r.review_and_repair()
   if blocked in ('protected','closed','assigned') or (blocked=='basis' and failed_review):
    assert len(r.version.nodes)==2 and r.unprocessed and r.findings[0].status=='open'
   else:
    assert [n.node_id for n in r.version.nodes]==['root']
    assert 'P14' in calls and r.assignments[0].outline_version_id==r.version.version_id
    assert r.findings[0].status==('open' if failed_review else 'superseded')
    assert r.version.commit_reason.endswith(':clear')
  finally: await r.aclose()
 asyncio.run(run())


def test_subchapter_list_relative_hierarchy_and_conflicting_evidence():
 materials={'m':{'text':'清单开始\n甲方案\n细项\n乙方案\n清单结束'}}
 reqs=[RequirementGroup(requirement_id='r',categories=['A'],topic='格式',content='格式',material_refs=['m'])]
 nodes=[OutlineNode(node_id='root',title='外部章节'),OutlineNode(node_id='a',parent_id='root',title='甲方案',basis_requirement_ids=['r']),
        OutlineNode(node_id='detail',parent_id='a',title='细项',basis_requirement_ids=['r']),OutlineNode(node_id='b',parent_id='root',title='乙方案',order=2,basis_requirement_ids=['r'])]
 ev=evidence(members='1|甲方案\n2|细项\n1|乙方案')
 constraint=bind_source_list(ev,materials,reqs,nodes,[])
 assert constraint.parent_node_id=='root'
 base=make_version(nodes,None,'subchapter',[constraint])
 with pytest.raises(ValueError,match='hierarchy'):
  make_version([n.model_copy(update={'parent_id':'root','order':3}) if n.node_id=='detail' else n for n in nodes],base,'wrong')
 with pytest.raises(ValueError,match='hierarchy'):
  bind_source_list(evidence(members='1|甲方案\n1|细项\n1|乙方案'),materials,reqs,nodes,[constraint])


def test_unbound_list_can_later_bind_below_external_chapter():
 materials={'m':{'text':'清单开始\n甲方案\n乙方案\n清单结束'}}
 reqs=[RequirementGroup(requirement_id='r',categories=['A'],topic='格式',content='格式',material_refs=['m'])]
 root=OutlineNode(node_id='root',title='外部章节')
 empty=bind_source_list(evidence(),materials,reqs,[root],[])
 assert not empty.affected_node_ids
 nodes=[root,OutlineNode(node_id='a',parent_id='root',title='甲方案',basis_requirement_ids=['r'])]
 bound=bind_source_list(evidence(),materials,reqs,nodes,[empty])
 assert bound.parent_node_id=='root' and bound.affected_node_ids==['a']


@pytest.mark.parametrize('status', ['无法在范围内修正','检查意见不成立'])
def test_non_success_cannot_clear(status):
 with pytest.raises(ParseError,match='successful'):
  parse_output('P16',wrap(f'处理结论：{status}\n替换目录：清空\n问题处理说明：原因\n仍需处理：待确认'))


def test_p07_list_fields_are_strict_and_material_typed():
 obj=evidence()
 for mutation in ({'清单终点':'无'},{'清单成员':'5|甲方案'},{'清单材料':'r'}):
  with pytest.raises(ParseError):
   parse_output('P07',wrap(rec('结构依据',**{**obj,**mutation})),allowed_ids=['r','m'],id_groups={'requirements':['r'],'materials':['m']})


def test_clear_checks_basis_assigned_elsewhere_and_rejects_uncovered_candidate():
 async def run():
  r=WorkflowRunner(Path(tempfile.mkdtemp(prefix='bid-clear-other-')),AppConfig(prompt_view='legacy-json', model='fake',context_tokens=100000,service_context_tokens=100000,repair_rounds=1),FakeModelClient([]))
  r.requirements=[RequirementGroup(requirement_id='r',topic='实际义务',content='必须承接')]
  r.commit([OutlineNode(node_id='root',title='待清理'),OutlineNode(node_id='child',parent_id='root',title='旧依据',basis_requirement_ids=['r']),
            OutlineNode(node_id='other',title='别章',order=2)],'initial')
  r.assignments=[RequirementAssignment(requirement_id='r',outline_version_id=r.version.version_id,status='已分配',targets=[{'node_id':'other','scope':'全部'}])]
  async def review(*args): return [ReviewFinding(finding_id='f',kind='其他',node_ids=['child'])]
  r.review=review; seen=[]
  async def call(task,material,**kwargs):
   if task=='P16': return {'task_id':'clear', 'fields':{'处理结论':'已修正','替换目录':'清空'},'records':{}}
   assert task=='P14'; seen.extend(kwargs['expected'])
   return {'records':{'覆盖检查':[{'要求':'r','结论':'未覆盖','承接节点':'无','未覆盖或不明确内容':'遗漏'}]}}
  r.call=call
  try:
   await r.review_and_repair()
   assert seen==['r'] and any(n.node_id=='child' for n in r.version.nodes)
   assert r.findings[0].status=='open' and any('uncovered' in x['error'] for x in r.unprocessed)
  finally: await r.aclose()
 asyncio.run(run())
