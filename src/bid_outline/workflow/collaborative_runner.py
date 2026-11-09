"""Position → local screening → complete blocks → category text → directory."""
import asyncio
import json

from bid_outline import __version__
from bid_outline.document.location_rules import VERSION as RULES_VERSION
from bid_outline.domain import Document
from bid_outline.export.markdown import write_atomic
from bid_outline.model.text_tasks import TASKS, TextMaterial
from .common import identity, now
from .extraction import Extraction
from .text_runner import TextWorkflowRunner

FLOW_VERSION = 'collaborative-directory-2'


class CollaborativeRunner(TextWorkflowRunner):
    def organization_task(self, task):
        return 'E02' if self.config.extraction_mode == 'anchors' else task

    async def organize_call(self, task, material, source, parents, name):
        if self.config.extraction_mode == 'anchors':
            result = await self.extraction.anchors(material, source, parents, name)
            if result:
                if task == 'N01-A' and not result.get('no_content'):
                    # An anchor response produces literal source, not an A directory.
                    return await self.call('N02-A', TextMaterial(result['text'],
                        material.context if isinstance(material, TextMaterial) else ''),
                        parents + [result['artifact_id']], name + ':根据回取原文生成格式目录')
                return result
        return await self.call(task, material, parents, name)

    async def run(self, document=None, *, resume=False):
        previous = self.repository.get_run()
        prompts = identity('prompts_', {task: self.messages(task, '') for task in TASKS})
        if resume:
            prior_config = dict(previous.get('config_snapshot', {})) if previous else {}
            prior_stop = prior_config.pop('stop_after', 'directory')
            new_config = self.config.model_dump(mode='json', exclude={'stop_after'})
            if not previous or any((previous.get('flow_version') != FLOW_VERSION,
                    previous.get('code_version') != __version__, previous.get('rules_version') != RULES_VERSION,
                    prior_config != new_config,
                    previous.get('prompt_identity') != prompts)):
                raise ValueError('resume flow/config/code/rules/prompts differ; create a new run')
            ranks = {'candidates': 0, 'organized': 1, 'directory': 2}
            if ranks[self.config.stop_after] < ranks[prior_stop]:
                raise ValueError('resume stage endpoint cannot move backwards')
            document = Document.model_validate(self.artifacts.get(previous['document_artifact_id']))
        elif previous:
            raise ValueError('run already exists; resume or create a new run')
        if document is None:
            raise ValueError('a document is required')
        source_id = previous['document_artifact_id'] if previous else self.artifacts.put('document', document.model_dump(mode='json'))
        self.manifest = {'run_id': previous['run_id'] if previous else identity('run_', [str(self.run_dir.resolve()), now()]),
            'flow_version': FLOW_VERSION, 'code_version': __version__, 'rules_version': RULES_VERSION,
            'config_snapshot': self.config.model_dump(mode='json'), 'prompt_identity': prompts,
            'document_artifact_id': source_id, 'stage': 'location', 'status': 'running',
            'started_at': previous['started_at'] if previous else now(), 'prompt_status': 'user_reviewed_draft',
            'reader_name': document.reader_name, 'reader_version': document.reader_version,
            'input_hashes': [document.source_hash], 'semantic_quality': 'not_evaluated'}
        self.save_manifest()
        self.extraction = Extraction(self, document, source_id)
        organized = {c: [] for c in 'ABC'}
        current = final = None
        try:
            for diagnostic in document.diagnostics:
                self.issues.append({'kind': 'reader_diagnostic', 'detail': diagnostic})
                if isinstance(diagnostic, dict) and diagnostic.get('code', diagnostic.get('kind')) in {
                    'unrepresented_reader_block', 'reader_fragment_coverage_gap_or_overlap', 'missing_table_content', 'image_not_read'}:
                    self.unprocessed.append({'scope': 'upstream_reader', 'diagnostic': diagnostic})
            ranges, selection_id = await self.extraction.locate()
            if self.config.stop_after != 'candidates':
                async def organize(located):
                    category, items = await self.organize(located, selection_id)
                    return located, category, items
                for located, category, items in await self.extraction.parallel(organize, ranges):
                    for item in items:
                        if item.get('no_content'):
                            continue
                        # A readable source label, not a model-owned ID contract.
                        item = {**item, 'text': '来源区域：' + item.get('source_heading', '') + '\n' + item['text']}
                        organized[category].append(item)
                async def summarize(category):
                    return category, await self.extraction.summarize(category, organized[category])
                for category, items in await self.extraction.parallel(summarize, 'ABC'):
                    organized[category] = items
                    text = '\n\n'.join(x['text'] for x in items)
                    write_atomic(self.run_dir / f'stages/organized/{category}.md', text)
            if self.config.stop_after == 'directory':
                drafts = []
                # A already is a directory; only B needs expansion after organization.
                drafts.extend(('A', item) for item in organized['A'])
                for item in organized['B']:
                    result = await self.attempt('N02-B', item['text'], [item['artifact_id']], '根据评审要求展开目录')
                    if result and not result.get('no_content'):
                        drafts.append(('B', result))
                current_kind = None
                for category, draft in drafts:
                    if current is None:
                        current = draft
                        current_kind = '根据格式编制要求形成的目录' if category == 'A' else '根据技术评审要求展开的目录'
                    else:
                        added_kind = '根据格式编制要求形成的目录' if category == 'A' else '根据技术评审要求展开的目录'
                        result = await self.attempt('N03', current_kind + '：\n' + current['text'] +
                            '\n\n' + added_kind + '：\n' + draft['text'], [current['artifact_id'], draft['artifact_id']], '融合目录')
                        if result and not result.get('no_content'):
                            current = result
                            current_kind = '已融合的累计目录草稿'
                        elif result:
                            self.unprocessed.append({'task_type': 'N03', 'scope': '融合目录',
                                'error': '已有非空草稿但融合回复无；保留原草稿及未合入内容',
                                'parents': [current['artifact_id'], draft['artifact_id']]})
                for item in organized['C']:
                    prefix = '当前目录：\n' + current['text'] if current else '当前尚无目录草稿'
                    result = await self.attempt('N04', prefix+'\n\n技术要求和响应事项：\n'+item['text'],
                        ([current['artifact_id']] if current else [])+[item['artifact_id']], 'C补充')
                    if result and not result.get('no_content'):
                        current = result
                    elif result and current:
                        self.unprocessed.append({'task_type': 'N04', 'scope': '根据技术要求补充目录',
                            'error': '已有目录但补充回复无；保留原草稿及未处理要求',
                            'parents': [current['artifact_id'], item['artifact_id']]})
                if current:
                    final = await self.attempt('N05', current['text'], [current['artifact_id']], '最终目录')
                    if final and final.get('no_content'):
                        self.unprocessed.append({'task_type': 'N05', 'scope': '最终目录',
                            'error': '已有目录草稿但最终回复无；保留草稿', 'parents': [current['artifact_id']]})
                        final = None
                    if final:
                        write_atomic(self.run_dir/'stages/final.md', final['text'])
                else:
                    self.issues.append({'kind': 'no_directory_draft', 'detail': '无可用草稿，不编造目录'})
            if not ranges:
                self.issues.append({'kind': 'no_selected_ranges', 'detail': '无已选完整范围，不代表全文没有要求'})
            status = 'incomplete' if self.unprocessed else 'completed_with_issues' if self.issues else 'completed'
            result = {'flow_version': FLOW_VERSION, 'stop_after': self.config.stop_after, 'status': status,
                'semantic_quality': 'not_evaluated', 'prompt_status': self.manifest['prompt_status'],
                'directory_text': final['text'] if final else '', 'draft_text': current['text'] if current else '',
                'directory_state': 'final_text' if final else 'draft',
                'organized': {c: '\n\n'.join(x['text'] for x in items) for c, items in organized.items()},
                'organized_artifacts': {c: [x['artifact_id'] for x in items] for c, items in organized.items()},
                'located_ranges': self.extraction.selected, 'stages': self.stages, 'issues': self.issues,
                'unprocessed_ranges': self.unprocessed,
                'completion_scope': '位置限定候选及文本整理；未作全文漏提或目录语义验收'}
            result_id = self.artifacts.put('run_result', result, parents=[source_id, selection_id] +
                [x['artifact_id'] for x in self.stages] + [a for items in organized.values() for a in [x['artifact_id'] for x in items]])
            self.manifest.update(status=status, stage='finished', finished_at=now(), result_artifact_id=result_id,
                unprocessed_ranges=self.unprocessed, issue_count=len(self.issues), model_stats=self.executor.stats)
            self.save_manifest()
            return result
        except BaseException as exc:
            self.manifest.update(status='cancelled' if isinstance(exc, asyncio.CancelledError) else 'failed',
                                 error=str(exc), finished_at=now())
            self.save_manifest()
            raise
