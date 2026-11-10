"""Position evidence admits a complete structural block, never selected lines."""
import asyncio
import json
from pathlib import Path

from bid_outline.document.section_blocks import SectionBlocks
from bid_outline.document.source_index import SourceIndex
from test_collaborative_extraction import document, execute


def test_direct_scoring_chapter_keeps_unmatched_numbered_children(tmp_path):
    async def run():
        text = ('序号：1\n评审项目：总体技术方案\n评审标准：具体如下：\n'
                '（1）范本适配及规则训练；\n（2）AI审查工具提供；\n'
                '（3）运维更新服务；\n（4）主要技术指标；\n评审依据：完整性。')
        doc = document([(['评标办法'],text)])
        doc.elements[0].kind='table'
        doc.elements[0].metadata['table_rows']=[{'text':'1 | 总体技术方案 | 不同形式的原始表格行',
            'cell_ids':['c1'],'row':0,'header':False}]
        result,client = await execute(tmp_path,doc,{'N01-B':['整理完成']},
            directory_assistance=False,stop_after='organized')
        query = client.requests[0]['messages'][-1]['content']
        assert text in query
        assert result['located_ranges'][0]['element_ids']==['e0']
        assert [r['_task_type'] for r in client.requests]==['N01-B']
    asyncio.run(run())


def test_clear_chapters_and_directory_model_need_no_body_keywords(tmp_path):
    async def run():
        doc=document([(['技术需求书'],'（1）甲；\n（2）乙。'),(['特别栏目'],'丙\n丁')])
        result,client=await execute(tmp_path,doc,{'E01':['B：特别栏目']},stop_after='candidates')
        assert len(result['located_ranges'])==2
        assert [(x['category'],x['element_ids']) for x in result['located_ranges']]==[('C',['e0']),('B',['e1'])]
        assert [r['_task_type'] for r in client.requests]==['E01']
    asyncio.run(run())


def test_broad_hint_recovers_whole_subsection_not_unrelated_sibling(tmp_path):
    async def run():
        doc=document([(['合同条款','履约条件'],'投标人承诺提供维护方案。'),
                      (['合同条款','履约条件'],'（1）甲；\n（2）乙；'),
                      (['合同条款','其他约定'],'完全无关的普通文字。')])
        result,_=await execute(tmp_path,doc,[],directory_assistance=False,stop_after='candidates')
        assert result['located_ranges'][0]['element_ids']==['e0','e1']
        assert len(result['located_ranges'])==1
    asyncio.run(run())


def test_reject_only_weak_broad_location_does_not_create_candidate(tmp_path):
    async def run():
        doc=document([(['合同条款'],'至少包括：报价材料。'),(['合同条款'],'无关键词的文字。')])
        result,_=await execute(tmp_path,doc,{'E00':['排除']},directory_assistance=False,stop_after='candidates')
        assert not result['located_ranges']
    asyncio.run(run())


def test_parent_and_child_overlap_collapse_per_category(tmp_path):
    async def run():
        doc=document([(['投标文件格式'],'格式概述'),(['投标文件格式','技术文件'],'甲'),
                      (['投标文件格式','技术文件'],'乙')])
        result,_=await execute(tmp_path,doc,{'E01':['A：投标文件格式\nA：投标文件格式 / 技术文件\nC：投标文件格式 / 技术文件']},
                               stop_after='candidates')
        a=[r for r in result['located_ranges'] if r['category']=='A']
        c=[r for r in result['located_ranges'] if r['category']=='C']
        assert len(a)==len(c)==1
        assert a[0]['element_ids']==['e0','e1','e2']
        assert c[0]['element_ids']==['e1','e2']
    asyncio.run(run())


def test_reader_identity_keeps_duplicate_titles_distinct_and_e01_ambiguous(tmp_path):
    async def run():
        doc=document([(['技术需求书'],'甲'),(['技术需求书'],'乙')])
        for n,e in enumerate(doc.elements):
            e.source_ref.reader_ref['fragment']={'section_id':f'S{n}','chapter_id':f'S{n}'}
        result,_=await execute(tmp_path,doc,{'E01':['C：技术需求书','C：技术需求书']},stop_after='candidates')
        assert result['status']=='incomplete'
        assert [(r['range_id'],r['element_ids']) for r in result['located_ranges']]==[
            ('reader:S0:C',['e0']),('reader:S1:C',['e1'])]
    asyncio.run(run())


def test_reader_catalog_parents_and_original_document_stay_intact():
    doc=document([(['采购需求'],'前言'),(['采购需求','实施方案'],'内容'),(['采购需求','其他'],'尾部')])
    doc.metadata['reader_sections']=[{'id':'s0','parent_id':None},{'id':'s1','parent_id':'s0'},
                                    {'id':'s2','parent_id':'s0'}]
    for e,sid in zip(doc.elements,['s0','s1','s2']):
        e.source_ref.reader_ref['fragment']={'section_id':sid,'chapter_id':'s0'}
    doc.elements[1].kind='table'
    doc.elements[1].metadata['table_rows']=[{'text':'different pipe text'}]
    before=doc.model_dump(mode='json')
    blocks=SectionBlocks(SourceIndex(doc))
    candidate=blocks.document_for('reader:s0')
    assert [e.text for e in candidate.elements]==['前言','内容','尾部']
    assert candidate.elements[1].kind=='expanded_table_text'
    assert candidate.elements[1].metadata['table_rows']==[]
    assert doc.model_dump(mode='json')==before


def test_real_s24_scoring_requirements_survive_candidate_and_model_input(tmp_path):
    root=Path(__file__).resolve().parents[2]
    path=root/'reviews/document_reading_location_24_20261109/S24/extraction/document.json'
    if not path.exists():
        import pytest
        pytest.skip('local reading artifact unavailable')
    from bid_outline.domain import Document
    from bid_outline.document.token_chunks import token_chunks
    doc=Document.model_validate(json.loads(path.read_text()))
    table=next(e for e in doc.elements if e.element_id=='F000426')
    blocks=SectionBlocks(SourceIndex(doc))
    key=blocks.element_section[table.element_id]
    candidate=blocks.document_for(key)
    assert next(e.text for e in candidate.elements if e.element_id==table.element_id)==table.text
    chunks=token_chunks(candidate,allowance=lambda c:1000,text_tokens=len,fits=lambda c:len(c.text)<=1000)
    owned=[]
    for c in chunks:
        for ref in c.owned_refs:
            if table.element_id in ref.element_ids:
                span=ref.reader_ref['element_text_range']
                owned.append((span['start'],span['end']))
    assert ''.join(table.text[a:b] for a,b in sorted(owned))==table.text
    for phrase in ['（1） 招标文件范本适配及审查规则训练；','（2） AI合规审查工具提供；',
                   '（3） 运维更新服务；','（4） 主要技术指标；']:
        assert phrase in table.text


def test_explicit_chapter_survives_weak_irrelevant_line_without_e00(tmp_path):
    async def run():
        doc=document([(['采购需求'],'至少包括：商务材料。'),(['采购需求'],'（1）甲；\n（2）乙。')])
        result,client=await execute(tmp_path,doc,[],directory_assistance=False,stop_after='candidates')
        assert not client.requests
        assert len(result['located_ranges'])==1
        assert result['located_ranges'][0]['element_ids']==['e0','e1']
    asyncio.run(run())


def test_fallback_repeated_heading_title_keeps_separate_blocks():
    doc=document([(['技术需求书'],'技术需求书'),(['技术需求书'],'甲'),
                  (['技术需求书'],'技术需求书'),(['技术需求书'],'乙')])
    doc.elements[0].metadata['role']='heading'
    doc.elements[2].metadata['role']='heading'
    blocks=SectionBlocks(SourceIndex(doc))
    assert len(blocks.nodes)==2
    assert [node.positions for node in blocks.nodes.values()]==[[0,1],[2,3]]


def test_same_top_level_independent_subsections_are_not_recombined(tmp_path):
    async def run():
        doc=document([(['合同条款','甲'],'投标人承诺提供甲方案。'),(['合同条款','甲'],'甲的无关键词子项'),
                      (['合同条款','乙'],'投标人承诺提供乙方案。'),(['合同条款','乙'],'乙的无关键词子项')])
        result,client=await execute(tmp_path,doc,{'N01-C':['甲整理','乙整理'],'E03-C':['统一要求']},
                                    directory_assistance=False,stop_after='organized')
        inputs=[r['messages'][-1]['content'] for r in client.requests if r['_task_type']=='N01-C']
        assert len(inputs)==2
        assert '甲的无关键词子项' in inputs[0] and '乙的无关键词子项' not in inputs[0]
        assert '乙的无关键词子项' in inputs[1] and '甲的无关键词子项' not in inputs[1]
        assert result['organized']['C']=='统一要求'
    asyncio.run(run())


def test_local_screening_keeps_duplicate_reader_sections_separate(tmp_path):
    async def run():
        doc=document([(['合同条款'],'至少包括：报价甲。'),(['合同条款'],'至少包括：响应乙。')])
        for n,e in enumerate(doc.elements):
            e.source_ref.reader_ref['fragment']={'section_id':f'S{n}','chapter_id':f'S{n}'}
        result,client=await execute(tmp_path,doc,{'E00':['排除','保留']},
                                    directory_assistance=False,stop_after='candidates')
        calls=[r['messages'][-1]['content'] for r in client.requests]
        assert len(calls)==2
        assert '报价甲' in calls[0] and '响应乙' not in calls[0]
        assert '响应乙' in calls[1] and '报价甲' not in calls[1]
        assert result['located_ranges'][0]['element_ids']==['e1']
    asyncio.run(run())
