"""Offline replay of complete-section candidates and source-preserving model inputs."""
import argparse
import asyncio
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from bid_outline import __version__
from bid_outline.config import load_config
from bid_outline.domain import Document
from bid_outline.model.text_tasks import VERSION as PROMPT_VERSION
from bid_outline.workflow.collaborative_runner import CollaborativeRunner
from bid_outline.workflow.extraction import Extraction


class NoModel:
    async def generate(self, request):
        raise AssertionError('offline replay must never call a model')
    async def aclose(self):
        pass


def save(path, value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(value if isinstance(value,str) else json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')


class Planner(CollaborativeRunner):
    async def attempt(self, task, material, parents, name):
        assert task=='E00', 'only local gates may be recorded as pending offline'
        return {'text':'待核','artifact_id':parents[0]}

    async def organize_call(self, task, material, source, parents, name):
        chunk=self.artifacts.get(parents[0])
        self.inputs.append({'task':task,'name':name,'body':material.body,
            'context':material.context,'supplement':material.supplement,
            'query':self.messages(task,material)[0]['content'],
            'material_tokens':self.material_tokens(material),'request_fits':self.fits(task,material),
            'owned_refs':chunk['owned_refs']})
        return {'text':'无','no_content':True,'artifact_id':parents[0]}


def verify(located, inputs):
    elements={e.element_id:e for e in located.document.elements}
    spans=defaultdict(list)
    body_consistent=True
    for item in inputs:
        pieces=[]
        for ref in item['owned_refs']:
            assert len(ref['element_ids'])==1
            eid=ref['element_ids'][0]
            interval=ref['reader_ref']['element_text_range']
            a,b=interval['start'],interval['end']
            spans[eid].append((a,b))
            pieces.append(elements[eid].text[a:b])
        body_consistent &= item['body']=='\n\n'.join(pieces)
    issues=[]
    for eid,element in elements.items():
        cursor=0
        for a,b in sorted(spans[eid]):
            if a!=cursor or not a<=b<=len(element.text):
                issues.append({'element_id':eid,'expected_start':cursor,'actual':[a,b]})
            cursor=b
        if cursor!=len(element.text):
            issues.append({'element_id':eid,'covered_end':cursor,'source_end':len(element.text)})
    if not body_consistent:
        issues.append({'error':'actual input body differs from referenced canonical text'})
    return {'exact_source_coverage':not issues,'input_body_matches_source_slices':body_consistent,
            'source_elements':len(elements),'issues':issues}


async def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,default=ROOT/'reviews/document_reading_location_24_20261109')
    parser.add_argument('--config',type=Path,default=ROOT/'configs/train.text.example.json')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--samples',nargs='+',default=[f'S{n:02d}' for n in range(1,25)])
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    config=load_config(args.config).model_copy(update={'directory_assistance':False,'stop_after':'candidates',
        'temperature':0.0,'thinking_default':False,'thinking_by_task':{},'train_enable_thinking':False})
    results=[]
    budget=None
    for sid in args.samples:
        folder=args.output/sid
        source=args.input/sid/'extraction/document.json'
        doc=Document.model_validate(json.loads(source.read_text()))
        # Preserve existing adapter body; restore only the reading chapter catalog
        # for explicit ancestor identities, without rerunning document-reading.
        reading=json.loads((args.input/sid/'reading/document.json').read_text())
        doc.metadata['reader_sections']=reading['corrected']['sections']
        runner=Planner(folder/'planning',config,NoModel())
        if budget is not None:
            runner.executor.budget=budget
        else:
            budget=runner.executor.budget
        runner.manifest={}
        parent=runner.artifacts.put('document',doc.model_dump(mode='json'))
        runner.extraction=Extraction(runner,doc,parent)
        try:
            located,selection=await runner.extraction.locate()
            records=[]
            for n,block in enumerate(located):
                runner.inputs=[]
                await runner.organize(block,selection)
                check=verify(block,runner.inputs)
                chunk_folder=folder/f'{n+1:02d}_{block.category}'
                save(chunk_folder/'candidate.md','\n\n'.join(e.text for e in block.document.elements))
                save(chunk_folder/'inputs.json',runner.inputs)
                for i,item in enumerate(runner.inputs):
                    save(chunk_folder/f'input_{i+1:02d}.md',item['query'])
                plans=[json.loads(p.read_text()) for p in (folder/'planning/stages').glob('organization_plan_*.json')]
                plan=next(p for p in plans if p['range_id']==block.range_id)
                record={**block.record(),'candidate_chars':sum(len(e.text) for e in block.document.elements),
                    'input_count':len(runner.inputs),'material_tokens':plan['material_tokens'],
                    'input_material_tokens_sum':sum(x['material_tokens'] for x in runner.inputs),
                    'decision':plan['reason'],'source_sha256':hashlib.sha256(
                        '\n\n'.join(e.text for e in block.document.elements).encode()).hexdigest(),
                    'coverage':check,'budget_ok':all(x['request_fits'] for x in runner.inputs)}
                # Keep block index readable; source refs are already in candidates/inputs.
                record.pop('source_refs')
                save(chunk_folder/'summary.json',record)
                records.append(record)
            probe=[]
            if sid=='S24':
                table=next(e for e in doc.elements if e.element_id=='F000426')
                selected_b=[b for b in located if b.category=='B' and any(e.element_id==table.element_id for e in b.document.elements)]
                assert len(selected_b)==1
                assert next(e.text for e in selected_b[0].document.elements if e.element_id==table.element_id)==table.text
                for phrase in ['（1） 招标文件范本适配及审查规则训练；','（2） AI合规审查工具提供；',
                    '（3） 运维更新服务；','（4） 主要技术指标；']:
                    present=any(phrase in (folder/f'{i+1:02d}_B'/'candidate.md').read_text()
                        for i,b in enumerate(located) if b.category=='B')
                    probe.append({'phrase':phrase,'in_complete_B_candidate':present})
                assert all(x['in_complete_B_candidate'] for x in probe)
            row={'sample':sid,'blocks':len(records),'by_category':{c:sum(r['category']==c for r in records) for c in 'ABC'},
                'inputs':sum(r['input_count'] for r in records),
                'single_blocks':sum(r['decision']=='single' for r in records),
                'split_blocks':sum(r['decision']!='single' for r in records),
                'max_candidate_material_tokens':max((r['material_tokens'] for r in records),default=0),
                'all_selected_source_coverage_exact':all(r['coverage']['exact_source_coverage'] for r in records),
                'all_input_budgets_ok':all(r['budget_ok'] for r in records),
                'local_gates_pending':sum(x['task_type']=='E00' for x in runner.unprocessed),
                'non_gate_failures':[x for x in runner.unprocessed if x['task_type']!='E00'],
                'reader_status':doc.metadata.get('reader_status'),
                'S24_probes':probe}
            save(folder/'blocks.json',records)
            save(folder/'summary.json',row)
            assert runner.executor.stats['calls']==0
            results.append(row)
            print(json.dumps(row,ensure_ascii=False),flush=True)
        finally:
            await runner.aclose()
    save(args.output/'summary.json',{'version':__version__,'prompt_version':PROMPT_VERSION,
        'model_calls':0,'directory_assistance':False,'local_gates':'pending; weak evidence retained',
        'scope':'complete rule-selected chapters and input coverage only; no semantic evaluation',
        'config':config.model_dump(mode='json'),'results':results})


if __name__=='__main__':asyncio.run(main())
