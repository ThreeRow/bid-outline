import asyncio
import json

import pytest

from bid_outline.config import AppConfig
from bid_outline.domain import Document, DocumentElement, SourceRef
from bid_outline.document.anchors import fields, match_anchors
from bid_outline.document.source_index import SourceIndex
from bid_outline.export import export_result
from bid_outline.model.client import FakeModelClient
from bid_outline.workflow import WorkflowRunner


def document(items):
    return Document(document_id='doc', source_hash='hash', reader_name='fixture', elements=[
        DocumentElement(element_id=f'e{i}', kind='paragraph', order=i, text=text, section_path=path,
            source_ref=SourceRef(document_id='doc', element_ids=[f'e{i}']))
        for i, (path, text) in enumerate(items)])


def config(**updates):
    return AppConfig(model='fake', context_tokens=30000, service_context_tokens=30000,
                     max_tokens=1000, text_batch_tokens=3000, **updates)


async def execute(path, doc, responses, **updates):
    client = FakeModelClient(responses)
    runner = WorkflowRunner(path, config(**updates), client)
    try:
        result = await runner.run(doc)
    finally:
        await runner.aclose()
    return result, client


def test_position_first_no_global_symbol_or_weak_word_scan(tmp_path):
    async def run():
        doc = document([(['公司简介'], '★至少包括：培训方案。')])
        result, client = await execute(tmp_path, doc, [], directory_assistance=False, stop_after='candidates')
        assert not client.requests and not result['located_ranges']
    asyncio.run(run())


@pytest.mark.parametrize('symbol', list('★☆▲△■'))
def test_symbols_are_location_signals_with_local_gate(tmp_path, symbol):
    async def run():
        doc = document([(['采购需求'], symbol+'响应时间不超过2秒。')])
        result, client = await execute(tmp_path, doc, {'E00': ['保留\n技术指标']},
            directory_assistance=False, stop_after='candidates')
        assert [r['_task_type'] for r in client.requests] == ['E00']
        assert result['located_ranges'][0]['category'] == 'C'
        assert symbol in client.requests[0]['messages'][-1]['content']
    asyncio.run(run())


def test_local_rejection_does_not_erase_other_hits_and_failure_is_not_rejection(tmp_path):
    async def run():
        doc = document([(['采购需求'], '至少包括：报价材料。'),
                        (['采购需求'], '投标人承诺提供故障响应。')])
        result, _ = await execute(tmp_path/'reject', doc, {'E00': ['排除']},
            directory_assistance=False, stop_after='candidates')
        assert len(result['located_ranges'][0]['element_ids']) == 1
        result, _ = await execute(tmp_path/'failed', doc, {'E00': ['']},
            directory_assistance=False, stop_after='candidates')
        assert result['status'] == 'incomplete'
        assert len(result['located_ranges'][0]['element_ids']) == 2
    asyncio.run(run())


def test_directory_model_extends_positions_but_only_to_real_titles(tmp_path):
    async def run():
        doc = document([(['特别栏目'], '技术方案须说明部署和培训。')])
        result, client = await execute(tmp_path, doc, {'E01': ['A：特别栏目']}, stop_after='candidates')
        assert len(client.requests) == 1 and result['located_ranges'][0]['category'] == 'A'
        result, _ = await execute(tmp_path/'bad', doc, {'E01': ['A：虚构标题', 'A：仍不存在']}, stop_after='candidates')
        assert result['status']=='incomplete' and not result['located_ranges']
    asyncio.run(run())


def test_category_summary_then_directory_resume_and_exports(tmp_path):
    async def run():
        doc = document([(['投标文件格式'], '总体技术方案'),
                        (['技术评审'], '方案完整性和先进性。'),
                        (['技术需求书'], '必须满足响应指标。')])
        responses = {'E01': ['无'], 'N01-A': ['格式整理'], 'N01-B': ['完整性\n先进性'],
            'N01-C': ['必须满足响应指标'], 'E03-A': ['A汇总'], 'E03-B': ['B汇总完整性先进性'],
            'E03-C': ['C汇总指标'], 'N02-B': ['目录B'], 'N03': ['融合目录'],
            'N04': ['补充目录'], 'N05': ['# 最终目录']}
        result, client = await execute(tmp_path, doc, responses)
        assert result['status']=='completed'
        assert result['organized']=={'A':'A汇总','B':'B汇总完整性先进性','C':'C汇总指标'}
        for task, expected in [('N03','A汇总'),('N02-B','B汇总完整性先进性'),('N04','C汇总指标')]:
            assert expected in next(r for r in client.requests if r['_task_type']==task)['messages'][-1]['content']
        assert not any(r['_task_type']=='N02-A' for r in client.requests)
        runner=WorkflowRunner(tmp_path, config(), FakeModelClient([]))
        try:
            second=await runner.run(resume=True)
            assert not runner.executor.client.requests
        finally:
            await runner.aclose()
        assert second['organized']==result['organized']
        assert {'A.md','B.md','C.md','backbone.md'} <= set(export_result(second,tmp_path/'export'))
    asyncio.run(run())


def test_organized_stage_never_generates_directory_and_no_id_contract(tmp_path):
    async def run():
        doc=document([(['项目需求'], '投标人承诺满足技术要求。')])
        result, client=await execute(tmp_path,doc,{'N01-C':['正常段落，非固定字段。'], 'E03-C':['整理后的技术依据']},
                                    directory_assistance=False,stop_after='organized')
        assert result['organized']['C']=='整理后的技术依据'
        assert [r['_task_type'] for r in client.requests]==['N01-C','E03-C']
        assert 'backbone.md' not in export_result(result,tmp_path/'exports')
    asyncio.run(run())


def test_multi_anchor_order_ambiguity_and_exact_source():
    source='起始。\n中 间。\n结束。\n起始。\n不同内容。\n结束。'
    record=fields('起始句：起始。\n中间锚点：中间。\n结束句：结束。')[0]
    # More than one end is still ambiguous; do not silently choose shortest.
    assert match_anchors(source,record)['status']=='multiple'
    unique=match_anchors('起始。\n中 间。\n结束。',record)
    assert unique['status']=='unique' and unique['matches'][0]['text']=='起始。\n中 间。\n结束。'
    assert match_anchors('开始\n结束\n中间',{'起始句':'开始','结束句':'结束','中间锚点':['中间']})['status']=='not_found'


def test_anchor_extraction_and_invalid_anchor_fallback(tmp_path):
    async def run():
        doc=document([(['采购需求'], '投标人承诺开始。\n必须保留条件。\n提供方案结束。')])
        replies={'E02':['起始句：投标人承诺开始。\n中间锚点：必须保留条件。\n结束句：提供方案结束。'],
                 'E03-C':['完整约束汇总']}
        result,client=await execute(tmp_path/'good',doc,replies,directory_assistance=False,
                                    extraction_mode='anchors',stop_after='organized')
        assert result['organized']['C']=='完整约束汇总'
        assert not any(r['_task_type']=='N01-C' for r in client.requests)
        replies={'E02':['起始句：编造\n结束句：编造','起始句：仍不存在\n结束句：仍不存在'],
                 'N01-C':['回退保留完整条件'],'E03-C':['回退汇总']}
        result,client=await execute(tmp_path/'bad',doc,replies,directory_assistance=False,
                                    extraction_mode='anchors',stop_after='organized')
        assert result['status']=='incomplete' and result['organized']['C']=='回退汇总'
        assert any(r['_task_type']=='N01-C' for r in client.requests)
    asyncio.run(run())


def test_projected_table_slice_keeps_original_mapping_through_token_splits(tmp_path):
    from bid_outline.document.token_chunks import token_chunks
    doc=document([(['技术需求书'],'表前\n'+'必须保留指标。'*100)])
    element=doc.elements[0];element.kind='table'
    index=SourceIndex(doc);unit=index.units[1]
    projected=index.document_for([unit])
    chunks=token_chunks(projected,allowance=lambda c:100,text_tokens=len,fits=lambda c:len(c.text)<=100)
    assert len(chunks)>1 and ''.join(c.text for c in chunks)==unit.text
    for chunk in chunks:
        ref=chunk.owned_refs[0]
        assert ref.reader_ref['projection_source_range']=={'start':unit.start,'end':unit.end}
        assert ref.reader_ref['original_element_id']=='e0'
        span=ref.reader_ref['element_text_range']
        assert 0<=span['start']<span['end']<=len(unit.text)


def test_long_local_context_is_explicit_excerpt_not_full_unit_deletion(tmp_path):
    async def run():
        doc=document([(['采购需求'], '前文。'*100+'至少包括：技术内容。'+'后文。'*100)])
        result,client=await execute(tmp_path,doc,{'E00':['排除']},directory_assistance=False,
            local_context_chars=80,stop_after='candidates')
        assert '局部节选' in client.requests[0]['messages'][-1]['content']
        assert result['status']=='incomplete' and result['located_ranges']
    asyncio.run(run())


def test_resume_advances_endpoint_without_repeating_adopted_extraction(tmp_path):
    async def run():
        doc=document([(['投标文件格式'],'总体技术方案')])
        result,_=await execute(tmp_path,doc,{'E01':['无'],'N01-A':['格式内容'],'E03-A':['统一格式内容']},
                               stop_after='organized')
        assert result['organized']['A']=='统一格式内容'
        client=FakeModelClient({'N05':['# 方案目录']})
        runner=WorkflowRunner(tmp_path,config(stop_after='directory'),client)
        try:
            resumed=await runner.run(resume=True)
        finally:
            await runner.aclose()
        assert resumed['directory_text']=='# 方案目录'
        assert [r['_task_type'] for r in client.requests]==['N05']
    asyncio.run(run())


def test_compilation_subsection_preserves_numbered_contents_without_keyword_every_line(tmp_path):
    async def run():
        doc=document([(['投标文件格式','六、服务方案'],'1、平台应用培训及搭建技术支撑'),
                      (['投标文件格式','六、服务方案'],'2、安全保障及可扩展性的相关阐述')])
        result,client=await execute(tmp_path,doc,[],directory_assistance=False,stop_after='candidates')
        assert not client.requests
        assert len(result['located_ranges'][0]['element_ids'])==2
    asyncio.run(run())


def test_cross_table_continuation_keeps_first_line_without_importing_whole_table(tmp_path):
    doc=document([(['评标办法'],'方案描述核心业'),(['评标办法'],'务需求的响应。\n1、其他独立条目')])
    for element in doc.elements:element.kind='table'
    index=SourceIndex(doc)
    chosen=index.complete_units(0)
    assert ''.join(x.text for x in chosen)=='方案描述核心业务需求的响应。'
    assert all('其他独立条目' not in x.text for x in chosen)


def test_missing_paths_use_structural_heading_without_mutating_reader():
    doc=document([([], '第五章 采购需求'),([], '★应满足技术指标')])
    index=SourceIndex(doc)
    assert index.sections==[['第五章 采购需求']]
    assert index.units[-1].element.section_path==['第五章 采购需求']
    assert all(not e.section_path for e in doc.elements)


def test_category_merge_retains_each_blocks_actual_source_heading(tmp_path):
    async def run():
        doc=document([(['第一章 投标文件格式'], '技术方案'),(['附件 技术文件'], '服务方案')])
        result,client=await execute(tmp_path,doc,{'N01-A':['第一处内容','第二处内容'],'E03-A':['统一整理']},
                                    directory_assistance=False,stop_after='organized')
        request=next(r for r in client.requests if r['_task_type']=='E03-A')
        text=request['messages'][-1]['content']
        assert '来源区域：第一章 投标文件格式' in text
        assert '来源区域：附件 技术文件' in text
        assert text.index('第一处内容')<text.index('第二处内容')
        assert result['organized']['A']=='统一整理'
    asyncio.run(run())


def test_exact_none_is_audited_but_not_sent_to_summary_or_directory(tmp_path):
    async def run():
        doc=document([(['投标文件格式'],'技术方案'),(['技术评审'],'实施方案评分'),
                      (['采购需求'],'必须满足技术指标')])
        result,client=await execute(tmp_path,doc,{'E01':['无'],'N01-A':['无'],
            'N01-B':['无'],'N01-C':['无']})
        assert result['organized']=={'A':'','B':'','C':''}
        assert result['directory_text']=='' and result['draft_text']==''
        assert not any(r['_task_type'].startswith(('E03','N02')) or r['_task_type'] in ('N03','N04','N05')
                       for r in client.requests)
        assert all(stage['no_content'] for stage in result['stages'])
        assert all(stage['text']=='无' for stage in result['stages'])
        assert result['status']=='completed_with_issues'
    asyncio.run(run())


def test_partly_empty_category_preserves_real_directory_and_summary_inputs(tmp_path):
    async def run():
        doc=document([(['第一章 投标文件格式'],'技术方案'),(['附件 技术文件'],'服务方案')])
        result,client=await execute(tmp_path,doc,{'N01-A':['无','售后服务方案'],
            'E03-A':['售后服务方案'],'N05':['售后服务方案']},directory_assistance=False)
        summary=next(r for r in client.requests if r['_task_type']=='E03-A')
        data=summary['messages'][0]['content'].split('\n\n---\n\n',1)[1]
        assert '来源区域：第一章 投标文件格式' not in data
        assert '来源区域：附件 技术文件' in data
        assert result['directory_text']=='售后服务方案'
        assert not any(r['_task_type']=='N02-A' for r in client.requests)
    asyncio.run(run())


def test_anchor_a_source_is_converted_to_directory_once(tmp_path):
    async def run():
        doc=document([(['投标文件格式'],'技术部分包括：总体技术方案、培训方案。')])
        answers={'E02':['起始句：技术部分包括：总体技术方案、培训方案。\n结束句：技术部分包括：总体技术方案、培训方案。'],
            'N02-A':['总体技术方案\n培训方案'],'E03-A':['总体技术方案\n培训方案'],
            'N05':['总体技术方案\n培训方案']}
        result,client=await execute(tmp_path,doc,answers,directory_assistance=False,extraction_mode='anchors')
        tasks=[r['_task_type'] for r in client.requests]
        assert tasks==['E02','N02-A','E03-A','N05']
        request=next(r for r in client.requests if r['_task_type']=='N02-A')
        instructions,body=request['messages'][0]['content'].split('\n\n---\n\n',1)
        assert '截取内容所属章节' in instructions
        assert body==doc.elements[0].text
        assert result['directory_text']=='总体技术方案\n培训方案'
    asyncio.run(run())


@pytest.mark.parametrize('answers', [['无'], ['起始句：虚构\n结束句：虚构','无']])
def test_anchor_none_first_or_after_repair_skips_organization(tmp_path, answers):
    async def run():
        doc=document([(['采购需求'],'必须响应技术参数。')])
        result,client=await execute(tmp_path,doc,{'E02':answers},directory_assistance=False,
                                    extraction_mode='anchors',stop_after='organized')
        assert result['organized']['C']==''
        assert all(r['_task_type']=='E02' for r in client.requests)
        if len(client.requests)>1:
            first=client.requests[0]['messages'][0]['content'].split('\n\n---\n\n',1)[1]
            instructions,body=client.requests[1]['messages'][0]['content'].split('\n\n---\n\n',1)
            assert body==first==doc.elements[0].text
            assert '上次匹配问题' in instructions
    asyncio.run(run())


def test_none_in_summary_stops_generation_and_none_at_final_keeps_existing_draft(tmp_path):
    async def run():
        doc=document([(['投标文件格式'],'技术方案')])
        result,client=await execute(tmp_path/'summary',doc,{'N01-A':['技术方案'],'E03-A':['无']},
                                    directory_assistance=False)
        assert result['organized']['A']=='' and result['directory_text']==''
        assert [r['_task_type'] for r in client.requests]==['N01-A','E03-A']
        result,client=await execute(tmp_path/'final',doc,{'N01-A':['技术方案'],'E03-A':['技术方案'],'N05':['无']},
                                    directory_assistance=False)
        assert result['draft_text']=='技术方案' and result['directory_text']==''
        assert result['status']=='incomplete'
        assert result['unprocessed_ranges'][-1]['task_type']=='N05'
    asyncio.run(run())


def test_directory_clarification_keeps_feedback_outside_directory_data(tmp_path):
    async def run():
        doc=document([(['特别栏目'],'技术方案')])
        result,client=await execute(tmp_path,doc,{'E01':['A：不存在的章节','A：特别栏目']},stop_after='candidates')
        assert result['located_ranges'][0]['category']=='A'
        instructions,body=client.requests[1]['messages'][0]['content'].split('\n\n---\n\n',1)
        assert body=='特别栏目' and '上次定位问题' in instructions
        assert '不存在的章节' not in body
    asyncio.run(run())
