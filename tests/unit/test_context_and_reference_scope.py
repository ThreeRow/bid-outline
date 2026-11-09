import asyncio
import json
from pathlib import Path
import tempfile

import pytest
from bid_outline.config import AppConfig
from bid_outline.document import chunk_document, split_chunk
from bid_outline.document.lookup import reference_candidates
from bid_outline.domain import Document, DocumentElement, SourceRef
from bid_outline.model.client import FakeModelClient, ModelResponse
from bid_outline.workflow.runner import WorkflowRunner


def doc(items):
 return Document(document_id='d',source_hash='h',reader_name='test',elements=[DocumentElement(
  element_id=f'e{i}',kind=kind,text=text,order=i,section_path=path,
  source_ref=SourceRef(document_id='d',element_ids=[f'e{i}'],precision='parsed_text',start=50,end=50+len(text)))
  for i,(text,path,kind) in enumerate(items)])

def wrap(body): return '[[结果开始]]\n'+body+'\n[[结果结束]]'
POSITIVE=wrap('判断：有\n相关主题：方案\n疑点：无\n引用线索：无')
NEGATIVE=wrap('判断：未发现\n相关主题：无\n疑点：无\n引用线索：无')
REFINED=wrap('处理结果：已摘取\n保留内容：方案细节\n疑点：无')
EMPTY=wrap('提取结果：未发现本类要求')


def create(client, **kw):
 return WorkflowRunner(Path(tempfile.mkdtemp(prefix='bid-context-scope-')),
                       AppConfig(model='fake',context_tokens=100000,service_context_tokens=100000,format_retries=0,**kw),client)


@pytest.mark.parametrize('adaptive',[False,True])
def test_actual_p01_p03_receive_candidate_condition_and_owned_is_exclusive(adaptive):
 async def run():
  text='仅涉及数据迁移时：\n'+'1.说明迁移方案与验证措施。'*65
  document=doc([(text,['实施'],'paragraph')])
  size=300 if not adaptive else 2000
  initial=chunk_document(document,size)
  leaves=split_chunk(document,initial[0]) if adaptive else initial
  plan={'P01':([ModelResponse('partial',finish_reason='length')] if adaptive else [])+[POSITIVE]*len(leaves),
        'P03':[REFINED]*len(leaves), **{f'P04-{c}':[EMPTY]*len(leaves) for c in 'ABC'}}
  client=FakeModelClient(plan); r=create(client,chunk_chars=size)
  try:
   result=await r.run(document)
   assert not result['unprocessed_ranges']
   active=list(r._leaf_chunks.values())
   assert ''.join(c.text for c in active)==text
   assert any(c.context_notes for c in active)
   tail=active[-1]
   assert tail.context_refs[0].start==50 and tail.context_notes[0]['text']=='仅涉及数据迁移时：'
   for task in ('P01','P03'):
    calls=[q for q in client.requests if q['_task_type']==task]
    assert '仅涉及数据迁移时' in calls[-1]['messages'][1]['content']
    assert '适用范围待语义确认' in calls[-1]['messages'][1]['content']
    assert '延续来源' in calls[-1]['messages'][1]['content']
   material=next(m for m in result['materials'].values() if m['source_chunk_id']==tail.chunk_id)
   assert len(material['source_refs'])>len(material['owned_refs'])
  finally: await r.aclose()
 asyncio.run(run())


def test_adjacent_numbered_list_group_keeps_leader_but_never_crosses_chapter():
 document=doc([('仅离线场景：',['甲'],'paragraph'),('1.部署方案'+'甲'*200,['甲'],'paragraph'),
               ('2.恢复方案'+'乙'*200,['甲'],'paragraph'),('3.无关内容'+'丙'*200,['乙'],'paragraph')])
 chunks=chunk_document(document,230)
 third=next(c for c in chunks if 'e2' in {eid for ref in c.owned_refs for eid in ref.element_ids})
 assert third.context_notes and third.context_notes[0]['text']=='仅离线场景：'
 last=chunks[-1]
 assert last.section_context==['乙'] and not last.context_notes
 assert [eid for c in chunks for ref in c.owned_refs for eid in ref.element_ids]==['e0','e1','e2','e3']
 # A complete short list should be packed together despite preceding prose.
 short=doc([('普通说明'*20,['甲'],'paragraph'),('仅离线场景：',['甲'],'paragraph'),('1.部署',['甲'],'paragraph'),('2.恢复',['甲'],'paragraph')])
 packed=chunk_document(short,95)
 assert {eid for ref in packed[-1].owned_refs for eid in ref.element_ids}=={'e1','e2','e3'}


def test_explicit_reference_returns_complete_local_and_ambiguous_scopes_without_top_five():
 document=doc([('附件一',['甲','附件一'],'heading'),('甲附件正文',['甲','附件一'],'paragraph'),
               ('附件一',['乙','附件一'],'heading'),('乙附件正文',['乙','附件一'],'paragraph'),
               ('后续无关',['乙','其他'],'paragraph')])
 found=reference_candidates(document,'见附件一',{'element_ids':['e3'],'section_path':['乙'],'order':3})
 assert found['strategy']=='explicit_section_or_attachment' and found['ambiguous']
 assert found['groups'][0]['element_ids']==['e2','e3']
 assert {e.element_id for e in found['elements']}=={'e0','e1','e2','e3'}
 many=doc([('迁移关键词'+str(i),[], 'paragraph') for i in range(9)])
 assert len(reference_candidates(many,'迁移关键词')['elements'])==9


@pytest.mark.parametrize('oversize',[False,True])
def test_supplement_adopts_whole_confirmed_section_or_explicitly_blocks_oversize(oversize):
 async def run():
  document=doc([('按附件甲提供方案',['主体'],'paragraph'),('附件甲',['附件甲'],'heading'),
                ('附件正文条件'*(20000 if oversize else 1),['附件甲'],'paragraph'),('其他章节内容',['其他'],'paragraph')])
  need='\n[[补读开始]]\n引用或疑点：附件甲\n需要的信息：完整附件\n用途：摘取\n查找线索：附件甲\n[[补读结束]]'
  client=FakeModelClient({'P03':[wrap('处理结果：已摘取\n保留内容：部分可用\n疑点：附件缺失'+need),REFINED],
                          'P06':[wrap('结论：已找到\n采用候选：e1\n关联说明：确认附件甲\n仍缺少的信息：无')]})
  r=create(client);r.document=document;r.document_artifact_id=r.artifacts.put('document',document.model_dump(mode='json'))
  try:
   result=await r.call('P03','初始完整材料',input_artifacts=[r.document_artifact_id],
                       source_location={'element_ids':['e0'],'section_path':['主体'],'order':0})
   if oversize:
    assert result['fields']['保留内容']=='部分可用' and result['supplement_material_ids']==[]
    blocked=r.unprocessed[0]
    assert blocked['task_type']=='reference_lookup' and blocked['candidate_scope']['groups'][0]['element_ids']==['e1','e2']
    assert '未截断' in blocked['error']
    # The small heading can now be confirmed independently; the indivisible
    # oversized body is still blocked, never truncated or sent to the model.
    assert [q['_task_type'] for q in client.requests]==['P03', 'P06']
   else:
    assert result['supplement_material_ids']==['e1','e2']
    assert '附件正文条件' in client.requests[-1]['messages'][1]['content']
    assert '其他章节内容' not in client.requests[-1]['messages'][1]['content']
    assert [q['_task_type'] for q in client.requests]==['P03','P06','P03']
    assert len(r.artifacts.envelope(result['result_ref']['artifact_id'])['parents'])>=3
  finally: await r.aclose()
 asyncio.run(run())


def test_long_candidate_condition_budget_is_explicitly_unprocessed():
 async def run():
  document=doc([('仅'+('条件'*500)+'时：'+'1.方案'*50,['甲'],'paragraph')])
  parent=chunk_document(document,5000)[0]
  children=split_chunk(document,parent)
  # Descend to a small owned tail; the complete large condition remains auxiliary.
  tail=children[-1]
  while split_chunk(document,tail): tail=split_chunk(document,tail)[-1]
  client=FakeModelClient([]);r=create(client)
  r.config.context_tokens=1200;r.config.max_tokens=300;r.config.safety_tokens=20
  from bid_outline.model.budget import TokenBudget
  r.executor.budget=TokenBudget(r.config)
  r.document=document;r.document_artifact_id=r.artifacts.put('document',document.model_dump(mode='json'))
  r._leaf_chunks={tail.chunk_id:tail}
  try:
   await r.process_chunk(tail)
   assert r.unprocessed and 'indivisible' in r.unprocessed[0]['error']
   assert not client.requests
  finally: await r.aclose()
 asyncio.run(run())


def test_specific_attachment_outranks_mentioned_ancestors_and_chapter_includes_descendants():
 document=doc([('第四章',['第四章'],'heading'),('项目需求',['第四章','项目需求'],'heading'),
  ('附件一',['第四章','项目需求','附件一'],'heading'),('附件正文',['第四章','项目需求','附件一'],'paragraph'),
  ('附件二',['第四章','项目需求','附件二'],'heading'),('其他附件正文',['第四章','项目需求','附件二'],'paragraph'),
  ('第五章',['第五章'],'heading'),('附件一',['第五章','附件一'],'heading')])
 specific=reference_candidates(document,'第四章 项目需求 附件一')
 assert {e.element_id for e in specific['elements']}=={'e2','e3'}
 chapter=reference_candidates(document,'参见第四章')
 assert {e.element_id for e in chapter['elements']}=={f'e{i}' for i in range(6)}
 assert len(chapter['groups'])==1


def test_long_internal_list_uses_its_own_leader_not_element_intro():
 text='不相关开场。\n\n迁移阶段应提供：\n1.方案'+('甲'*500)+'\n2.验证'+('乙'*500)+'\n\n后续独立说明。'
 document=doc([(text,['章'],'paragraph')])
 chunks=chunk_document(document,200)
 scoped=[c for c in chunks if any(n['text']=='迁移阶段应提供：' for n in c.context_notes)]
 assert scoped and all('不相关开场' not in n['text'] for c in scoped for n in c.context_notes)
 parent=chunk_document(document,2000)[0]
 children=split_chunk(document,parent)
 assert any(n['text']=='迁移阶段应提供：' for n in children[-1].context_notes)


def test_partial_parsed_ref_end_and_expired_condition_provenance():
 from bid_outline.domain import Chunk
 from bid_outline.document.paragraph_context import attach_paragraph_context
 text='仅迁移时：\n'+'方案'*80+'\n\n'+'独立内容'*80
 document=doc([(text,['章'],'paragraph')]); element=document.elements[0]
 ref=element.source_ref.model_copy(deep=True);ref.start=70;ref.end=90
 chunk=attach_paragraph_context(document,Chunk(chunk_id='partial',text=text[20:40],owned_refs=[ref]))
 assert all(n['candidate_scope']['end']==40 for n in chunk.context_notes)
 # A parent's condition should not remain evidence for a later independent child.
 parent=chunk_document(document,200)[0]
 while True:
  children=split_chunk(document,parent)
  if not children: break
  parent=children[-1]
 # Explicitly construct a full parent carrying an earlier candidate, then split
 # into a later source range beyond the paragraph boundary.
 whole=chunk_document(document,2000)[0]
 whole.context_notes=chunk.context_notes
 whole.context_refs=chunk.context_refs
 later=split_chunk(document,whole)[-1]
 assert not later.context_notes and not later.context_refs


def test_supplement_retry_budget_retains_previous_usable_result():
 async def run():
  document=doc([('附件甲',['附件甲'],'heading'),('附件正文',['附件甲'],'paragraph')])
  need='\n[[补读开始]]\n引用或疑点：附件甲\n需要的信息：附件\n用途：摘取\n查找线索：附件甲\n[[补读结束]]'
  client=FakeModelClient({'P03':[wrap('处理结果：已摘取\n保留内容：已有可用内容\n疑点：待补读'+need)],
                          'P06':[wrap('结论：已找到\n采用候选：e0\n关联说明：附件\n仍缺少的信息：无')]})
  r=create(client);r.document=document;r.document_artifact_id=r.artifacts.put('document',document.model_dump(mode='json'))
  from bid_outline.model.executor import BudgetExceeded
  original=r.executor.budget.check
  def budget(messages,max_tokens,**kwargs):
   if '补充材料' in messages[1]['content']: raise BudgetExceeded('combined retry too large')
   return original(messages,max_tokens,**kwargs)
  r.executor.budget.check=budget
  try:
   result=await r.call('P03','初始材料',input_artifacts=[r.document_artifact_id],resizable=True)
   assert result['fields']['保留内容']=='已有可用内容'
   assert result['supplement_material_ids']==[]  # fetched sources were not used in adopted result
   assert r.unprocessed[-1]['pending_supplement_ids']==['e0','e1']
   assert r.unprocessed[-1]['retained_result_ref']==result['result_ref']
   assert [q['_task_type'] for q in client.requests]==['P03','P06']
  finally: await r.aclose()
 asyncio.run(run())
