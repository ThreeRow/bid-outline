"""Small text pipeline; provenance and execution state are program-owned."""
import asyncio
import json
import re
from pathlib import Path

from bid_outline import __version__
from bid_outline.document.locate import locate
from bid_outline.document.organization import route_organization, VERSION as ROUTING_VERSION
from bid_outline.document.token_chunks import token_chunks
from bid_outline.document.chunks import split_chunk
from bid_outline.document.text_chunks import expandable_tables
from bid_outline.domain import Document
from bid_outline.export.markdown import write_atomic
from bid_outline.model.executor import ModelExecutor, OutputTruncated
from bid_outline.model.budget import BudgetExceeded, TaskExecutionError
from bid_outline.model.task_specs import render_task
from bid_outline.model.text_tasks import TASKS, TextMaterial, input_data, CATEGORY_CONTENT
from bid_outline.storage import ArtifactStore, RunRepository
from .common import bounded_map, identity, now

FLOW_VERSION = 'text-directory-1'


class TextWorkflowRunner:
    def __init__(self, run_dir, config, client):
        self.run_dir = Path(run_dir)
        self.config = config
        self.repository = RunRepository(self.run_dir)
        self.artifacts = ArtifactStore(self.run_dir)
        self.executor = ModelExecutor(config, client, self.repository, self.artifacts)
        self.issues, self.unprocessed, self.stages = [], [], []

    def save_manifest(self):
        self.repository.save_run(self.manifest)
        write_atomic(self.run_dir / 'manifest.json', json.dumps(self.manifest, ensure_ascii=False, indent=2) + '\n')

    def messages(self, task, material):
        return render_task(task, input_data(material))

    def fits(self, task, material):
        try:
            self.executor.budget.check(self.messages(task, material), self.config.max_tokens, task_type=task)
            return True
        except BudgetExceeded:
            return False

    async def call(self, task, material, parents, name):
        inputs = input_data(material)
        task_id = identity('text_', [FLOW_VERSION, task, inputs, parents])
        result = await self.executor.execute(task, {**inputs, 'display_name': name},
                                             task_id=task_id, dependencies=parents)
        ref = self.repository.result_ref(task_id)
        item = {'task_type': task, 'task_id': task_id, 'artifact_id': ref['artifact_id'],
                'text': result['text'], 'no_content': result.get('no_content', False), 'parents': parents, 'name': name}
        self.stages.append(item)
        # A deterministic readable projection; adoption remains authoritative.
        write_atomic(self.run_dir / 'stages' / (task_id + '.md'), result['text'])
        return item

    async def attempt(self, task, material, parents, name):
        self.manifest['stage'] = task
        self.save_manifest()
        try:
            return await self.call(task, material, parents, name)
        except TaskExecutionError as exc:
            self.unprocessed.append({'task_type': task, 'scope': name, 'parents': parents,
                                     'error': f'{type(exc).__name__}: {exc}'})
            return None

    async def organize(self, located, parent):
        task = 'N01-' + located.category
        budget_task = self.organization_task(task)
        budget = self.executor.budget
        document = expandable_tables(located.document, budget.text_tokens, self.config.text_batch_tokens)
        elements = {e.element_id: e for e in document.elements}
        def material(chunk, body=None):
            notes = '\n'.join(n['text'] for n in chunk.context_notes)
            sections = '\n'.join(' / '.join(span['section_path']) for span in chunk.section_spans)
            headers = list(dict.fromkeys(row['text'] for ref in chunk.owned_refs for eid in ref.element_ids
                if elements[eid].kind == 'expanded_table_text'
                for row in elements[eid].metadata.get('table_rows', []) if row.get('header')))
            projected_context = '\n'.join(dict.fromkeys(elements[eid].metadata.get('shared_context', '')
                for ref in chunk.owned_refs for eid in ref.element_ids if elements[eid].metadata.get('shared_context')))
            context = ('本次寻找的内容：' + CATEGORY_CONTENT[located.category] + '\n截取内容所属章节：\n' +
                    (' / '.join(chunk.section_context) or '见所给材料的位置说明') +
                    ('\n共同条件/表头：\n' + projected_context if projected_context else '') +
                    ('\n涉及小节：\n' + sections if sections else '') +
                    ('\n辅助条件（仅供理解）：\n' + notes if notes else '') +
                    ('\n原表表头（辅助理解）：\n' + '\n'.join(headers) if headers else ''))
            return TextMaterial(chunk.text if body is None else body, context)
        chunks = token_chunks(document,
            allowance=lambda c: min(self.config.text_batch_tokens, budget.allowance(
                self.messages(budget_task, material(c, '')), self.config.max_tokens, task_type=budget_task)),
            text_tokens=budget.text_tokens,
            fits=lambda c: budget.text_tokens(c.text) <= self.config.text_batch_tokens and self.fits(budget_task, material(c)))
        async def process(chunk):
            source = self.artifacts.put('located_chunk', chunk.model_dump(mode='json'), parents=[parent])
            try:
                if budget.text_tokens(chunk.text) > self.config.text_batch_tokens:
                    raise BudgetExceeded('indivisible source exceeds text processing limit')
                result = await self.organize_call(task, material(chunk), chunk.text, [source], located.category + ':' + chunk.chunk_id)
                headings = [' / '.join(span['section_path']) for span in chunk.section_spans]
                return [{**result, 'source_heading': '；'.join(headings) or ' / '.join(chunk.section_context)}]
            except (BudgetExceeded, OutputTruncated) as exc:
                children = split_chunk(document, chunk)
                if children:
                    outputs = []
                    for child in children:
                        outputs.extend(await process(child))
                    return outputs
                else:
                    self.unprocessed.append({'task_type': task, 'scope': chunk.chunk_id,
                        'source_refs': [r.model_dump(mode='json') for r in chunk.owned_refs], 'error': str(exc)})
            except TaskExecutionError as exc:
                self.unprocessed.append({'task_type': task, 'scope': chunk.chunk_id, 'parents': [source], 'error': str(exc)})
            return []
        # Requests may finish out of order; flatten in planned source order.
        groups = await bounded_map(process, chunks, workers=self.config.workers, capacity=self.config.queue_capacity)
        return located.category, [item for group in groups for item in group]

    async def organize_call(self, task, material, source, parents, name):
        return await self.call(task, material, parents, name)

    def organization_task(self, task):
        return task

    def generation_batches(self, task, text, wrap=lambda text: text):
        """Pack complete paragraphs/lines, retaining all text and explicit oversize units."""
        budget = self.executor.budget
        def fits(body):
            return budget.text_tokens(body) <= self.config.text_batch_tokens and self.fits(task, wrap(body))
        if fits(text):
            return [text]
        units = []
        for paragraph in re.split(r'(?<=\n)(?=\n)', text):
            units.extend([paragraph] if fits(paragraph) else paragraph.splitlines(keepends=True))
        batches, current = [], ''
        for unit in units:
            if current and not fits(current + unit):
                batches.append(current)
                current = ''
            current += unit
        if current:
            batches.append(current)
        return batches

    async def run(self, document=None, *, resume=False):
        previous = self.repository.get_run()
        prompt_identity = identity('prompts_', {task: self.messages(task, '') for task in TASKS})
        if resume:
            if not previous or previous.get('flow_version') != FLOW_VERSION:
                raise ValueError('resume requires a matching text workflow run; create a new run')
            if previous['config_snapshot'] != self.config.model_dump(mode='json') or previous['code_version'] != __version__:
                raise ValueError('resume configuration/code differs; create a new run')
            if previous.get('prompt_identity') != prompt_identity:
                raise ValueError('resume prompts differ; create a new run')
            document = Document.model_validate(self.artifacts.get(previous['document_artifact_id']))
        elif previous:
            raise ValueError('run already exists; resume or create a new run')
        if document is None:
            raise ValueError('a document is required')
        doc_id = previous['document_artifact_id'] if previous else self.artifacts.put('document', document.model_dump(mode='json'))
        self.manifest = {'run_id': previous['run_id'] if previous else identity('run_', [str(self.run_dir.resolve()), now()]),
            'flow_version': FLOW_VERSION, 'code_version': __version__, 'config_snapshot': self.config.model_dump(mode='json'),
            'document_artifact_id': doc_id, 'status': 'running', 'stage': 'location',
            'prompt_identity': prompt_identity, 'input_hashes': [document.source_hash],
            'organization_routing_version': ROUTING_VERSION,
            'reader_name': document.reader_name, 'reader_version': document.reader_version,
            'model_identity': self.config.model,
            'started_at': previous['started_at'] if previous else now(), 'semantic_quality': 'not_evaluated',
            'prompt_status': 'provisional_awaiting_user_design'}
        self.save_manifest()
        final, current = None, None
        try:
            for diagnostic in document.diagnostics:
                self.issues.append({'kind': 'reader_diagnostic', 'detail': diagnostic})
                if isinstance(diagnostic, dict) and diagnostic.get('code', diagnostic.get('kind')) in {
                    'unrepresented_reader_block', 'reader_fragment_coverage_gap_or_overlap',
                    'missing_table_content', 'image_not_read'}:
                    self.unprocessed.append({'scope': 'upstream_reader', 'diagnostic': diagnostic})
            ranges = locate(document)
            records = [r.record() for r in ranges]
            location_id = self.artifacts.put('located_ranges', {'ranges': records}, parents=[doc_id])
            write_atomic(self.run_dir / 'stages/located_ranges.json', json.dumps(records, ensure_ascii=False, indent=2))
            organization_ranges, organization_plan = route_organization(document, ranges)
            organization_id = self.artifacts.put('organization_plan', organization_plan, parents=[location_id])
            write_atomic(self.run_dir / 'stages/organization_plan.json', json.dumps(organization_plan, ensure_ascii=False, indent=2))
            self.manifest['stage'] = 'organization'
            self.save_manifest()
            organized = {'A': [], 'B': [], 'C': []}
            async def organize(r):
                return await self.organize(r, organization_id)
            values = await bounded_map(organize, organization_ranges, workers=self.config.workers, capacity=self.config.queue_capacity)
            for category, items in values:
                organized[category].extend(items)
            drafts = []
            for category in 'AB':
                for index, item in enumerate(organized[category]):
                    task = 'N02-' + category
                    for part, text in enumerate(self.generation_batches(task, item['text'])):
                        if self.executor.budget.text_tokens(text) > self.config.text_batch_tokens:
                            self.unprocessed.append({'task_type': task, 'parents': [item['artifact_id']],
                                'error': 'indivisible organized text exceeds processing limit', 'scope': f'{category}:{index}:{part}'})
                            continue
                        draft = await self.attempt(task, text, [item['artifact_id']], f'{category}目录{index+1}-{part+1}')
                        if draft:
                            drafts.append((category, draft))
            for category, draft in drafts:
                if current is None:
                    current = draft
                    continue
                merged = await self.attempt('N03', '当前完整目录草稿：\n' + current['text'] +
                    f'\n\n新增{category}目录草稿：\n' + draft['text'],
                    [current['artifact_id'], draft['artifact_id']], '融合目录')
                if merged:
                    current = merged
            for item in organized['C']:
                # C can independently contain an explicit scheme requirement.
                prefix = '当前完整目录草稿：\n' + current['text'] if current else '当前目录：尚无A/B目录'
                parts = self.generation_batches('N04', item['text'], lambda text: prefix + '\n\n补充要求：\n' + text)
                for text in parts:
                    prefix = '当前完整目录草稿：\n' + current['text'] if current else '当前目录：尚无A/B目录'
                    parents = ([current['artifact_id']] if current else []) + [item['artifact_id']]
                    updated = await self.attempt('N04', prefix + '\n\n补充要求：\n' + text,
                        parents, 'C补充' if current else 'C独立生成')
                    if updated:
                        current = updated
            if current:
                final = await self.attempt('N05', current['text'], [current['artifact_id']], '最终目录')
                if final:
                    write_atomic(self.run_dir / 'stages/final.md', final['text'])
            else:
                self.issues.append({'kind': 'no_directory_draft', 'detail': '没有生成可用目录草稿；不能认定原文没有要求'})
            status = 'incomplete' if self.unprocessed else ('completed_with_issues' if self.issues or not final else 'completed')
            result = {'flow_version': FLOW_VERSION, 'status': status, 'semantic_quality': 'not_evaluated',
                'prompt_status': self.manifest['prompt_status'], 'directory_text': final['text'] if final else '',
                'draft_text': current['text'] if current else '', 'directory_state': 'final_text' if final else 'draft',
                'final_artifact_id': final['artifact_id'] if final else None,
                'draft_artifact_id': current['artifact_id'] if current else None,
                'completion_scope': '规则选出的候选范围，经文本整理生成目录；未进行全文漏提或目录语义检查。',
                'located_ranges': records, 'stages': self.stages, 'issues': self.issues,
                'organization_plan': organization_plan,
                'unprocessed_ranges': self.unprocessed}
            result_id = self.artifacts.put('run_result', result, parents=[doc_id, location_id, organization_id] + [s['artifact_id'] for s in self.stages])
            self.manifest.update(status=status, stage='finished', finished_at=now(), result_artifact_id=result_id,
                unprocessed_ranges=self.unprocessed, issue_count=len(self.issues), model_stats=self.executor.stats)
            self.save_manifest()
            return result
        except BaseException as exc:
            self.manifest.update(status='cancelled' if isinstance(exc, asyncio.CancelledError) else 'failed',
                                 error=str(exc), finished_at=now())
            self.save_manifest()
            raise

    async def aclose(self):
        await self.executor.aclose()
        self.repository.close()
