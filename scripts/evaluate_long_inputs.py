"""Replay 24 reading artifacts offline; optionally compare long C processing on train."""
import argparse
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from bid_outline.config import load_config
from bid_outline.document.locate import LocatedRange
from bid_outline.document.source_index import SourceIndex, Unit
from bid_outline.document.token_chunks import token_chunks
from bid_outline.domain import Document
from bid_outline.model.client import FakeModelClient, make_model_client
from bid_outline.model.text_tasks import TextMaterial
from bid_outline.workflow.collaborative_runner import CollaborativeRunner
from bid_outline.workflow.extraction import Extraction
from bid_outline.workflow.text_runner import TextWorkflowRunner


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


class Planner(CollaborativeRunner):
    async def attempt(self, task, material, parents, name):
        assert task == 'E00', 'offline replay must not invoke semantic tasks'
        return {'text':'待核', 'artifact_id':parents[0]}

    async def organize_call(self, task, material, source, parents, name):
        self.planned.append({'task':task, 'name':name, 'material_tokens':self.material_tokens(material),
            'request_fits':self.fits(task, material), 'body':material.body, 'context':material.context, 'supplement':material.supplement})
        return {'text':'无', 'no_content':True, 'artifact_id':parents[0]}


async def replay(args, config):
    rows, real_candidates = [], {}
    for sid in args.samples:
        doc = Document.model_validate(json.loads((args.input/sid/'extraction/document.json').read_text()))
        runner = Planner(args.output/sid, config, FakeModelClient([]))
        runner.manifest = {}
        source_id = runner.artifacts.put('document', doc.model_dump(mode='json'))
        runner.extraction = Extraction(runner, doc, source_id)
        try:
            ranges, parent = await runner.extraction.locate()
            versions = {}
            for mode in ('old3000','adaptive'):
                runner.planned = []
                for located in ranges:
                    if mode == 'old3000':
                        await TextWorkflowRunner.organize(runner, located, parent)
                    else:
                        await runner.organize(located, parent)
                save(args.output/sid/(mode+'_inputs.json'), runner.planned)
                versions[mode] = {'inputs':len(runner.planned),
                    'material_tokens':sum(x['material_tokens'] for x in runner.planned),
                    'budget_blocked_inputs':sum(not x['request_fits'] for x in runner.planned)}
            plans = [json.loads(p.read_text()) for p in sorted((args.output/sid/'stages').glob('organization_plan_*.json'))]
            row = {'sample':sid, **versions, 'positions':len(plans),
                'single_positions':sum(p['reason']=='single' for p in plans),
                'split_positions':sum(p['reason']!='single' for p in plans)}
            rows.append(row)
            if sid in args.real_samples:
                real_candidates[sid] = (doc, ranges)
            print(json.dumps(row, ensure_ascii=False), flush=True)
        finally:
            await runner.aclose()
    save(args.output/'planning_summary.json', {'model_calls':0,
        'scope':'rule positions only; E01 disabled; E00 undecided hits retained; no semantic validation',
        'config':config.model_dump(mode='json'), 'results':rows})
    return real_candidates


def long_excerpt(doc, ranges, runner):
    """Select a bounded source slice to isolate organization, without rerunning reader/gates."""
    budget = runner.executor.budget
    candidates = []
    for located in ranges:
        if located.category != 'C':
            continue
        def material(chunk, body=None):
            return runner.organization_material('C', located.document, chunk, body)
        def fits(chunk):
            value = material(chunk)
            return runner.material_tokens(value)<=10300 and runner.fits('N01-C', value)
        for chunk in token_chunks(located.document,
                allowance=lambda c:min(10300-runner.material_tokens(material(c,'')),
                    budget.allowance(runner.messages('N01-C',material(c,'')), runner.config.max_tokens,
                                     task_type='N01-C')),
                text_tokens=budget.text_tokens, fits=fits):
            value = material(chunk)
            if not 9200 <= runner.material_tokens(value) <= 10300 or not runner.fits('N01-C',value):
                continue
            elements = {e.element_id:e for e in located.document.elements}
            common = value.supplement
            units = []
            for ref in chunk.owned_refs:
                e = elements[ref.element_ids[0]]
                span = ref.reader_ref.get('element_text_range', {'start':0,'end':len(e.text)})
                a,b = span['start'],span['end']
                units.append(Unit(e.element_id+f':comparison:{a}:{b}', e, a,b,e.text[a:b],
                                  context=common))
            excerpt = SourceIndex(located.document).document_for(units)
            projected = token_chunks(excerpt, allowance=lambda c:10**15,
                text_tokens=budget.text_tokens, fits=lambda c:True)
            if len(projected)==1:
                actual = runner.organization_material('C', excerpt, projected[0])
                if 9200 <= runner.material_tokens(actual) <= 10300 and runner.fits('N01-C', actual):
                    candidates.append((LocatedRange('long-comparison', 'C', excerpt, []), actual))
    if not candidates:
        raise ValueError('no 9200–10300-token C excerpt fits the single-call comparison budget')
    return max(candidates, key=lambda x:runner.material_tokens(x[1]))


async def compare(args, config, candidates):
    reports = []
    for sid, (doc, ranges) in candidates.items():
        folder = args.output/sid/'real_comparison'
        runner = CollaborativeRunner(folder, config, make_model_client(config))
        runner.manifest = {}
        parent = runner.artifacts.put('document', doc.model_dump(mode='json'))
        runner.extraction = Extraction(runner, doc, parent)
        try:
            try:
                located, material = long_excerpt(doc, ranges, runner)
            except ValueError as exc:
                row = {'sample':sid, 'status':'skipped_no_long_excerpt', 'reason':str(exc),
                       'model_stats':runner.executor.stats, 'semantic_quality':'not_evaluated'}
                save(folder/'report.json', row)
                reports.append(row)
                print('SKIP '+json.dumps(row,ensure_ascii=False), flush=True)
                continue
            selected = runner.artifacts.put('comparison_source', located.document.model_dump(mode='json'), parents=[parent])
            save(folder/'source.json', located.document.model_dump(mode='json'))
            save(folder/'source.txt', material.body)
            save(folder/'source_context.txt', material.context)
            save(folder/'source_supplement.txt', material.supplement)
            # Baseline deliberately bypasses the soft threshold, never the hard budget.
            baseline = await runner.attempt('N01-C', material, [selected], '对照：长输入单次整理')
            _, outputs = await runner.organize(located, selected)
            save(folder/'single.txt', baseline['text'] if baseline else '')
            save(folder/'adaptive.txt', '\n\n'.join(x['text'] for x in outputs))
            row = {'sample':sid, 'source_tokens':runner.material_tokens(material),
                'single_available':bool(baseline), 'adaptive_volumes':len(outputs),
                'model_stats':runner.executor.stats, 'unprocessed':runner.unprocessed,
                'semantic_quality':'not_evaluated',
                'scope':'bounded C excerpt; comparison of organization only, E01/E00 not run on model'}
            save(folder/'report.json', row)
            reports.append(row)
            print('REAL '+json.dumps(row,ensure_ascii=False), flush=True)
        finally:
            await runner.aclose()
    save(args.output/'real_comparison.json', reports)


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,default=ROOT/'reviews/document_reading_location_24_20261109')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--config',type=Path,default=ROOT/'configs/train.text.example.json')
    parser.add_argument('--samples',nargs='+',default=[f'S{n:02d}' for n in range(1,25)])
    parser.add_argument('--real-samples',nargs='*',default=[],help='Explicitly request real train long-C comparisons for these samples')
    args = parser.parse_args()
    if set(args.real_samples)-set(args.samples):
        parser.error('--real-samples must be included in --samples')
    args.output.mkdir(parents=True,exist_ok=False)
    config = load_config(args.config).model_copy(update={'temperature':0.0, 'thinking_default':False,
        'thinking_by_task':{}, 'train_enable_thinking':False, 'directory_assistance':False, 'stop_after':'organized'})
    candidates = await replay(args,config)
    if args.real_samples:
        await compare(args,config,candidates)


if __name__=='__main__':
    asyncio.run(main())
