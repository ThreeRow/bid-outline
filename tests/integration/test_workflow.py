"""Fixed response plans exercise orchestration, not model semantic quality."""
import asyncio
from collections import Counter
import json
from pathlib import Path
import re
import tempfile

from bid_outline.config import AppConfig
from bid_outline.domain import Document, DocumentElement, SourceRef
from bid_outline.model.client import ModelResponse
from bid_outline.model.task_specs import unescape_material
from bid_outline.workflow.runner import WorkflowRunner


def wrapped(body):
    return '[[结果开始]]\n'+body+'\n[[结果结束]]'


def record(kind, **fields):
    return f'[[{kind}开始]]\n'+'\n'.join(f'{k}：{v}' for k,v in fields.items())+f'\n[[{kind}结束]]'


def node(ident, parent, title, basis):
    return record('节点', 节点=ident, 父节点=parent, 同级顺序='1', 标题=title,
                  依据=basis, 形成方式='原文明示', 说明='无')


def visible_ids(data):
    labels = ('要求ID', '已有目录节点ID', '材料ID', '原文元素ID', '建议ID',
              '仅引用元数据的要求ID（仅维持节点依据，不可据此判断要求内容）')
    if '允许引用ID' in data:
        return [] if data['允许引用ID'] == '无' else data['允许引用ID'].split('、')
    return list(dict.fromkeys(ident for key in labels for ident in data.get(key, '无').split('、') if ident != '无'))


class FixedPlanClient:
    """Predetermined business answers; only opaque runtime IDs are substituted."""
    def __init__(self, fail=None, negative=False):
        self.requests=[]
        self.fail=fail
        self.negative=negative
        self.root=None

    async def generate(self, request):
        self.requests.append(request)
        task=request['_task_type']
        if task==self.fail:
            return ModelResponse('[[结果开始]]\n缺少必需业务字段\n[[结果结束]]')
        blocks=re.findall(r'\[\[输入数据开始\]\]\n([^：\n]+)：\n(.*?)\n\[\[输入数据结束\]\]',request['messages'][1]['content'],re.S)
        data={k:unescape_material(v) for k,v in blocks}
        allowed=visible_ids(data)
        expected=[] if data['必须逐项响应ID']=='无' else data['必须逐项响应ID'].split('、')
        requirements=[x for x in allowed if x.startswith('REQ')]
        materials=[x for x in allowed if x.startswith('MAT')]
        nodes=[x for x in allowed if x.startswith('NODE')]
        if task in ('P01','P02'):
            body='判断：'+('未发现' if self.negative else '有')+'\n相关主题：技术方案\n疑点：无\n引用线索：无'
        elif task=='P03':
            body='处理结果：已摘取\n保留内容：技术部分保留“实施方案”章节；包含部署与培训方案。技术评审考察部署。\n疑点：无'
        elif task.startswith('P04-'):
            cat=task[-1]
            text={'A':'技术部分保留实施方案章节。','B':'技术评审考察部署方案。','C':'技术部分应包含培训方案。'}[cat]
            body='提取结果：已提取\n'+record('要求',局部编号='R01',类别=cat,材料依据=materials[0],要求主题={'A':'实施方案','B':'部署','C':'培训'}[cat],要求内容=text,疑点='无')
        elif task=='P07':
            body=record('结构依据',局部编号='S01',要求依据='、'.join(requirements),性质='非固定清单',原文表达='技术部分保留实施方案章节',结构含义='保留章节名称',疑点='无')
        elif task=='P08':
            body='状态：已形成候选骨架\n骨架：\n'+node('N01','ROOT','实施方案','、'.join(requirements))+'\n调整说明：依据清单\n待核查事项：无'
        elif task=='P09':
            assert len(nodes)==1
            self.root=nodes[0]
            body=record('归属',要求=expected[0],状态='已分配',承接节点=self.root,承接范围='完整要求及共同条件',未承接内容='无',依据或疑点='实施章节承接')
        elif task=='P11':
            self.root=data['负责范围']
            body=f'章节：{self.root}\n处理结果：已生成\n子目录：\n'+node('N01',self.root,'部署与培训方案','、'.join(requirements))+'\n未承接事项：无\n结构调整建议：无'
        elif task=='P14':
            body=record('覆盖检查',要求=expected[0],结论='已覆盖',承接节点='、'.join(nodes),承接说明='实施章及部署培训子目录直接表达',未覆盖或不明确内容='无',修正建议='无')
        elif task=='P15':
            body='检查结论：未发现问题'
        elif task=='P17':
            # Parent ID is copied from supplied outline data, never inferred semantically.
            material=data['完整材料包']
            outline=json.JSONDecoder().raw_decode(material.split('骨干：\n',1)[1])[0]
            self.root=next(n['node_id'] for n in outline if n['parent_id'] is None)
            body='结果：有建议\n'+record('建议',局部编号='E01',对应章节=self.root,建议主题='演练建议',建议内容='可考虑在条件允许时进行部署演练',来源性质='项目需求推理',相关依据=materials[0],适用理由与条件='仅在现场资源允许时')
        elif task=='P18':
            body=record('建议检查',建议=expected[0],结论='保留',依据='条件明确且未写为强制要求',修改意见='无')
        else:
            raise AssertionError(f'Unexpected task in fixed plan: {task}')
        return ModelResponse(wrapped(body))

    async def aclose(self):
        pass


def document(text='技术部分保留实施方案章节，技术评审考察部署，技术文件须提供培训方案。'):
    return Document(document_id='synthetic_contract_document',source_hash='synthetic-hash',reader_name='test-fixture',reader_version='test-v1',elements=[
        DocumentElement(element_id='source_element_1',kind='paragraph',order=0,text=text,
                        source_ref=SourceRef(document_id='synthetic_contract_document',element_ids=['source_element_1'],precision='block'))])


def config():
    return AppConfig(prompt_view='legacy-json', model='fake',context_tokens=100000,service_context_tokens=100000,chunk_chars=20000,format_retries=0,transport_retries=0,repair_rounds=0)


def test_full_fixed_pipeline_preserves_provenance_and_resume():
    async def scenario():
        run_dir=Path(tempfile.mkdtemp(prefix='bid-workflow-full-'))
        client=FixedPlanClient()
        runner=WorkflowRunner(run_dir,config(),client)
        result=await runner.run(document())
        assert result['status']=='completed_with_issues',result['unprocessed_ranges']
        assert any(i['kind']=='source_list_pending' for i in result['issues'])
        assert {r['categories'][0] for r in result['requirements']}==set('ABC')
        roots=[n for n in result['outline']['nodes'] if n['parent_id'] is None]
        assert [n['title'] for n in roots]==['实施方案']
        assert result['outline']['constraints'][0]['origin']=='系统策略'
        assert result['outline']['constraints'][0]['affected_node_ids']==[roots[0]['node_id']]
        material=next(iter(result['materials'].values()))
        assert material['kind']=='refined_text'
        assert material['source_refs'][0]['element_ids']==['source_element_1']
        assert all(r['material_refs']==[material['material_id']] for r in result['requirements'])
        assert result['extensions'][0]['review_status']=='保留'
        assert '演练建议' not in [n['title'] for n in result['outline']['nodes']]
        counts=Counter(r['_task_type'] for r in client.requests)
        assert counts=={'P01':1,'P03':1,'P04-A':1,'P04-B':1,'P04-C':1,'P07':1,'P08':1,'P09':3,'P11':1,'P14':3,'P15':1,'P17':1,'P18':1}
        assert '不证明全文提取无遗漏' in result['completion_scope']
        await runner.aclose()
        resumed_client=FixedPlanClient()
        resumed=WorkflowRunner(run_dir,config(),resumed_client)
        second=await resumed.run(resume=True)
        assert second['status']=='completed_with_issues'
        assert second['outline']['version_id']==result['outline']['version_id']
        assert resumed_client.requests==[],Counter(r['_task_type'] for r in resumed_client.requests)
        await resumed.aclose()
    asyncio.run(scenario())


def test_malformed_screen_is_incomplete_not_empty_business_result():
    async def scenario():
        client=FixedPlanClient(fail='P01')
        runner=WorkflowRunner(Path(tempfile.mkdtemp(prefix='bid-workflow-failure-')),config(),client)
        result=await runner.run(document())
        assert result['status']=='incomplete'
        assert result['unprocessed_ranges'][0]['task_type']=='P01'
        assert result['requirements']==[]
        assert [r['_task_type'] for r in client.requests]==['P01']
        await runner.aclose()
    asyncio.run(scenario())


def test_negative_nonkeyword_document_does_not_run_extraction_or_full_rescan():
    async def scenario():
        client=FixedPlanClient(negative=True)
        runner=WorkflowRunner(Path(tempfile.mkdtemp(prefix='bid-workflow-negative-')),config(),client)
        result=await runner.run(document('本页仅列邮寄地址和收件日期。'))
        assert result['status']=='completed_with_issues'
        assert result['unprocessed_ranges']==[]
        assert [r['_task_type'] for r in client.requests]==['P01']
        assert result['document_id']=='synthetic_contract_document'
        await runner.aclose()
    asyncio.run(scenario())


def test_one_extraction_failure_preserves_other_categories_and_incomplete_scope():
    async def scenario():
        client=FixedPlanClient(fail='P04-B')
        runner=WorkflowRunner(Path(tempfile.mkdtemp(prefix='bid-workflow-partial-')),config(),client)
        result=await runner.run(document())
        assert result['status']=='incomplete'
        assert {r['categories'][0] for r in result['requirements']}=={'A','C'}
        assert any(r['task_type']=='P04-B' for r in result['unprocessed_ranges'])
        assert result['outline']['nodes']
        assert Counter(r['_task_type'] for r in client.requests)['P04-B']==1
        await runner.aclose()
    asyncio.run(scenario())


def request_data(request):
    return {k:unescape_material(v) for k,v in re.findall(
        r'\[\[输入数据开始\]\]\n([^：\n]+)：\n(.*?)\n\[\[输入数据结束\]\]',
        request['messages'][1]['content'],re.S)}


def section(data, name):
    return json.JSONDecoder().raw_decode(data['完整材料包'].split(name+'：\n',1)[1])[0]


class AdjustmentPlan(FixedPlanClient):
    def __init__(self):
        super().__init__()
        self.skeleton_calls=0

    async def generate(self, request):
        task=request['_task_type']
        data=request_data(request)
        allowed=visible_ids(data)
        reqs=[x for x in allowed if x.startswith('REQ')]
        nodes=[x for x in allowed if x.startswith('NODE')]
        if task=='P08':
            self.skeleton_calls+=1
            if self.skeleton_calls==2:
                self.requests.append(request)
                body='状态：已形成候选骨架\n骨架：\n'+node(nodes[0],'ROOT','实施方案','、'.join(reqs))+'\n'+node('N02',nodes[0],'培训承接','、'.join(reqs))+'\n调整说明：统一最小新增\n待核查事项：无'
                return ModelResponse(wrapped(body))
        if task=='P09':
            self.requests.append(request)
            self.root=section(data,'骨架')['nodes'][0]['node_id']
            pending=self.skeleton_calls==1
            return ModelResponse(wrapped(record('归属',要求=data['必须逐项响应ID'],状态='未承接' if pending else '已分配',承接节点='无' if pending else self.root,承接范围='无' if pending else '全部要求',未承接内容='培训方案' if pending else '无',依据或疑点='需要最小补充' if pending else '新增结构可承接')))
        if task=='P10':
            self.requests.append(request)
            return ModelResponse(wrapped(record('调整建议',要求=data['必须逐项响应ID'],处理='新增子节点',目标或父节点=self.root,建议标题='培训承接',位置说明='实施方案之下',承接内容='培训要求',依据与限制='保留原A章名及顺序')))
        if task=='P11':
            self.requests.append(request)
            self.root=data['负责范围']
            preserved=section(data,'保留结构')[0]
            return ModelResponse(wrapped(f'章节：{self.root}\n处理结果：已生成\n子目录：\n'+
                node(preserved['node_id'],self.root,preserved['title'],'、'.join(reqs))+
                '\n未承接事项：无\n结构调整建议：无'))
        return await super().generate(request)


def test_unassigned_adjustment_is_centralized_and_reassigned():
    async def scenario():
        client=AdjustmentPlan()
        runner=WorkflowRunner(Path(tempfile.mkdtemp(prefix='bid-adjust-')),config(),client)
        result=await runner.run(document())
        assert result['status']=='completed_with_issues',result['issues']
        assert any(i['kind']=='source_list_pending' for i in result['issues'])
        counts=Counter(r['_task_type'] for r in client.requests)
        assert counts['P08']==2 and counts['P10']==3 and counts['P09']==6
        sequence=[r['_task_type'] for r in client.requests]
        assert sequence.index('P10')<max(i for i,t in enumerate(sequence) if t=='P08')<max(i for i,t in enumerate(sequence) if t=='P09')
        assert all(a['status']=='已分配' for a in result['assignments'])
        assert [n['title'] for n in result['outline']['nodes'] if n['parent_id'] is None]==['实施方案']
        await runner.aclose()
    asyncio.run(scenario())


class RepairPlan(FixedPlanClient):
    def __init__(self):
        super().__init__()
        self.repaired=False

    async def generate(self, request):
        task=request['_task_type']
        data=request_data(request)
        if task=='P14' and not self.repaired:
            self.requests.append(request)
            return ModelResponse(wrapped(record('覆盖检查',要求=data['必须逐项响应ID'],结论='部分覆盖',承接节点=self.root,承接说明='实施章节有位置但标题未明确',未覆盖或不明确内容='培训方案',修正建议='在本章明确部署和培训')))
        if task=='P16':
            self.requests.append(request)
            self.repaired=True
            reqs=[r['requirement_id'] for r in section(data,'范围内全部要求')]
            return ModelResponse(wrapped('处理结论：已修正\n替换目录：\n'+node('N01',section(data,'边界父节点')['node_id'],'部署与培训方案（修正）','、'.join(reqs))+'\n问题处理说明：补全培训承接\n仍需处理：无'))
        return await super().generate(request)


def test_partial_coverage_repairs_complete_branch_and_rechecks():
    async def scenario():
        client=RepairPlan()
        runner=WorkflowRunner(Path(tempfile.mkdtemp(prefix='bid-repair-')),config().model_copy(update={'repair_rounds':1}),client)
        result=await runner.run(document())
        assert result['status']=='completed_with_issues',result['issues']
        assert any(i['kind']=='source_list_pending' for i in result['issues'])
        counts=Counter(r['_task_type'] for r in client.requests)
        assert counts['P16']==1 and counts['P14']==6 and counts['P15']==2
        assert all(f['status']=='superseded' for f in result['findings'])
        titles=[n['title'] for n in result['outline']['nodes']]
        assert titles==['实施方案','部署与培训方案（修正）']
        assert next(iter(result['materials'].values()))['source_refs'][0]['element_ids']==['source_element_1']
        await runner.aclose()
    asyncio.run(scenario())


class SupplementPlan(FixedPlanClient):
    def __init__(self, partial):
        super().__init__()
        self.partial=partial

    async def generate(self, request):
        task=request['_task_type']
        if task=='P03':
            response=await super().generate(request)
            need=record('补读',引用或疑点='实施方案附件',需要的信息='实施方案附件完整内容',用途='确认实施方案要求',查找线索='实施方案')
            response.content=response.content.replace('[[结果结束]]',need+'\n[[结果结束]]')
            return response
        if task=='P06':
            self.requests.append(request)
            candidates=visible_ids(request_data(request))
            return ModelResponse(wrapped('结论：'+('部分找到' if self.partial else '候选均不适用')+'\n采用候选：'+(candidates[0] if self.partial else '无')+'\n关联说明：仅有上下文，缺少附件\n仍缺少的信息：完整附件'))
        return await super().generate(request)


def test_supplement_partial_and_rejected_candidates_are_bounded():
    async def scenario(partial):
        client=SupplementPlan(partial)
        runner=WorkflowRunner(Path(tempfile.mkdtemp(prefix='bid-supplement-')),config().model_copy(update={'supplement_rounds':1}),client)
        result=await runner.run(document())
        assert result['status']=='completed_with_issues'
        assert any(i['kind']=='unresolved_reference' for i in result['issues'])
        counts=Counter(r['_task_type'] for r in client.requests)
        assert counts['P06']==1 and counts['P03']==(2 if partial else 1)
        assert len(result['materials']) == (2 if partial else 1)
        if partial:
            source = result['materials']['source_element_1']
            assert source['kind'] == 'reader_source'
            assert source['source_refs'][0]['element_ids'] == ['source_element_1']
            assert source['artifact_id'].startswith('a-')
        assert next(iter(result['materials'].values()))['source_refs'][0]['element_ids']==['source_element_1']
        await runner.aclose()
    asyncio.run(scenario(True))
    asyncio.run(scenario(False))


class CrossChapterPlan(FixedPlanClient):
    async def generate(self, request):
        task=request['_task_type']
        data=request_data(request)
        allowed=visible_ids(data)
        reqs=[x for x in allowed if x.startswith('REQ')]
        if task=='P08':
            self.requests.append(request)
            second=node('N02','ROOT','培训方案','、'.join(reqs)).replace('同级顺序：1','同级顺序：2')
            return ModelResponse(wrapped('状态：已形成候选骨架\n骨架：\n'+node('N01','ROOT','实施方案','、'.join(reqs))+'\n'+second+'\n调整说明：两个明确组织目的\n待核查事项：无'))
        if task=='P09':
            self.requests.append(request)
            roots=[n['node_id'] for n in section(data,'骨架')['nodes'] if n['parent_id'] is None]
            return ModelResponse(wrapped(record('归属',要求=data['必须逐项响应ID'],状态='已分配',承接节点='、'.join(roots),承接范围='实施章组织部署，培训章组织培训；共同条件保持',未承接内容='无',依据或疑点='两个组织目的')))
        if task=='P11':
            self.requests.append(request)
            root=data['负责范围']
            return ModelResponse(wrapped(f'章节：{root}\n处理结果：已生成\n子目录：\n'+node('N01',root,'组织安排','、'.join(reqs))+'\n未承接事项：无\n结构调整建议：无'))
        if task=='P13':
            self.requests.append(request)
            branches=section(data,'分支')
            assert all(b['祖先路径'] and b['章节承接范围'] for b in branches)
            compared=[b['比较节点'] for b in branches]
            assert len(compared)==2 and {n['title'] for n in compared}=={'组织安排'}
            return ModelResponse(wrapped('结论：合理交叉\n涉及节点：'+'、'.join(n['node_id'] for n in compared)+'\n涉及要求：'+'、'.join(reqs)+'\n依据：实施与培训的组织目的不同\n建议：保留两个分支'))
        if task=='P17':
            self.requests.append(request)
            return ModelResponse(wrapped('结果：无建议'))
        return await super().generate(request)


def test_same_title_cross_chapter_is_reviewed_without_automatic_deduplication():
    async def scenario():
        client=CrossChapterPlan()
        runner=WorkflowRunner(Path(tempfile.mkdtemp(prefix='bid-cross-')),config(),client)
        result=await runner.run(document())
        assert result['status']=='completed_with_issues',result['unprocessed_ranges']
        assert any(i['kind']=='source_list_pending' for i in result['issues'])
        assert Counter(r['_task_type'] for r in client.requests)['P13']==1
        shared=[n for n in result['outline']['nodes'] if n['title']=='组织安排']
        assert len(shared)==2 and len({n['parent_id'] for n in shared})==2
        assert result['findings']==[]
        await runner.aclose()
    asyncio.run(scenario())


def test_direct_artifact_chain_is_queryable_without_searching_prompt_text():
    async def scenario():
        client=RepairPlan()
        runner=WorkflowRunner(Path(tempfile.mkdtemp(prefix='bid-direct-chain-')),config().model_copy(update={'repair_rounds':1}),client)
        try:
            result=await runner.run(document())
            tasks=runner.repository.list_tasks()
            by_type={}
            for task in tasks:
                by_type.setdefault(task['task_type'],[]).append(task)
                if task.get('accepted_artifact_id'):
                    ref=runner.repository.result_ref(task['task_id'])
                    assert ref['call_id'] and ref['artifact_id']==task['accepted_artifact_id']
                    envelope=runner.artifacts.envelope(ref['artifact_id'])
                    assert envelope['parents']==task['dependencies']
            def kinds(task):
                return {runner.artifacts.envelope(a)['kind'] for a in task['dependencies']}
            screen=by_type['P01'][0]['accepted_artifact_id']
            refine=by_type['P03'][0]
            assert screen in refine['dependencies'] and 'source_chunk' in kinds(refine)
            for category in 'ABC':
                assert 'refined_block' in kinds(by_type['P04-'+category][0])
            assert by_type['P07'][0]['accepted_artifact_id'] in by_type['P08'][0]['dependencies']
            repair=by_type['P16'][0]
            assert 'outline_version' in kinds(repair)
            assert any(runner.artifacts.envelope(a)['task_id'] in {t['task_id'] for t in by_type['P14']}
                       for a in repair['dependencies'])
            version=runner.repository.get_outline()
            assert repair['accepted_artifact_id'] in version['input_artifact_ids']
            assert any(runner.artifacts.envelope(a)['kind']=='outline_version' for a in version['input_artifact_ids'])
            refined=next(m for m in result['materials'].values() if m['kind']=='refined_text')
            parents=runner.artifacts.envelope(refined['artifact_id'])['parents']
            assert screen in parents and refine['accepted_artifact_id'] in parents
            # Audit identities are not semantic prompt content.
            assert all('result_ref' not in q['messages'][1]['content'] for q in client.requests)
        finally:
            await runner.aclose()
    asyncio.run(scenario())
