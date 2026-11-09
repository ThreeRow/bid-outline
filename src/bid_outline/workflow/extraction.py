"""Position-first collaborative extraction, with plain-text business results."""
import json
import re

from bid_outline.document.anchors import fields, match_anchors
from bid_outline.document.locate import LocatedRange, position_hits
from bid_outline.document.location_rules import LOCAL, VERSION, compact, position_categories
from bid_outline.document.source_index import SourceIndex
from bid_outline.export.markdown import write_atomic
from bid_outline.model.text_tasks import TextMaterial, with_feedback, CATEGORY_CONTENT
from .common import identity, bounded_map


class Extraction:
    def __init__(self, runner, document, source_id):
        self.runner, self.index, self.source_id = runner, SourceIndex(document), source_id
        self.config = runner.config
        self.positions = {tuple(path): set(position_categories(path)) for path in self.index.sections}
        self.candidates, self.selected, self.controls = [], [], []

    def save(self, kind, data, parents=None):
        aid = self.runner.artifacts.put(kind, data, parents=parents or [self.source_id])
        write_atomic(self.runner.run_dir / 'stages' / (kind + '.json'),
                     json.dumps(data, ensure_ascii=False, indent=2))
        return aid

    async def parallel(self, fn, items):
        return await bounded_map(fn, list(items), workers=self.config.workers, capacity=self.config.queue_capacity)

    def note(self, task, scope, error):
        self.runner.unprocessed.append({'task_type': task, 'scope': scope, 'error': error})

    def parse_hints(self, text):
        matches, errors = [], []
        if text.strip() == '无':
            return matches, errors
        for line in text.splitlines():
            m = re.fullmatch(r'\s*([ABC])\s*[:：]\s*(.+)', line)
            if not m:
                if line.strip():
                    errors.append('无法识别目录定位行:' + line[:100])
                continue
            category, title = m.groups()
            found = [path for path in self.index.sections if compact(title) in
                     (compact(' / '.join(path)), compact(path[-1]))]
            if len(found) != 1:
                errors.append(f'{title}匹配{len(found)}个位置')
            else:
                matches.append((tuple(found[0]), category))
        return matches, errors

    async def directory(self):
        overview = '\n'.join(' / '.join(path) for path in self.index.sections)
        # Missing directory is explicit; source-derived structural paths are still useful.
        if not overview:
            self.runner.issues.append({'kind': 'missing_section_index', 'detail': '无可定位章节；不退回全文盲匹配'})
            return
        if not self.config.directory_assistance:
            return
        for page in self.runner.generation_batches('E01', overview):
            result = await self.runner.attempt('E01', page, [self.source_id], '目录定位')
            if not result:
                continue
            found, errors = self.parse_hints(result['text'])
            if errors:
                retry = await self.runner.attempt('E01', TextMaterial(page, '上次定位问题：\n' + '\n'.join(errors) +
                    '\n请根据所给目录重新选择真实章节。'),
                    [self.source_id, result['artifact_id']], '目录定位澄清')
                if retry:
                    more, errors = self.parse_hints(retry['text'])
                    found.extend(more)
                    result = retry
            if errors:
                self.note('E01', '目录定位', '；'.join(errors))
            self.controls.append(result['artifact_id'])
            for path, category in found:
                # A selected parent chapter includes its descendant paths.
                for original in self.positions:
                    if original[:len(path)] == path:
                        self.positions[original].add(category)

    async def locate(self):
        self.save('section_index', {'sections': self.index.overview(), 'rules_version': VERSION})
        await self.directory()
        self.candidates = position_hits(self.index, self.positions)
        candidate_id = self.save('rule_candidates', {'positions': [
            {'path': list(path), 'categories': sorted(categories)} for path, categories in self.positions.items()],
            'candidates': self.candidates}, [self.source_id] + self.controls)
        # Merge identical local contexts for the same category; one decision
        # controls just these hits, never erases other hits in the section.
        groups = {}
        for candidate in self.candidates:
            if not candidate['weak']:
                continue
            context = self.index.context(candidate['index'])
            path = candidate['unit']['path']
            key = (candidate['category'], tuple(path), context)
            groups.setdefault(key, []).append(candidate)
        async def screen(item):
            (category, path, context), candidates = item
            excerpted = False
            if len(context) > self.config.local_context_chars:
                # This is explicitly a local excerpt for the gate, never the
                # full source adopted by organization or anchor extraction.
                unit = candidates[0]['unit']['text']
                signal = LOCAL[category].search(unit)
                center = context.find(unit) + (signal.start() if signal else 0)
                start = max(0, center - self.config.local_context_chars // 3)
                context = '【局部节选，边界可能省略条件；不足以排除时请待核】\n' + context[start:start+self.config.local_context_chars]
                excerpted = True
            material = TextMaterial(context, '本次寻找的内容：\n' + CATEGORY_CONTENT[category] +
                '\n截取内容所属章节：\n' + ' / '.join(path) + '\n本次发现的线索：\n' +
                '、'.join(dict.fromkeys(hit for c in candidates for hit in c['hits'])))
            result = await self.runner.attempt('E00', material, [candidate_id], '局部判断')
            state = result['text'].strip().splitlines()[0].strip() if result else '待核'
            if state not in {'保留', '排除', '待核'}:
                state = '待核'
            if excerpted and state == '排除':
                # The selected unit may contain other distant signals. A local
                # excerpt cannot authorize discarding the entire unsent unit.
                state = '待核'
            return candidates, state, result, None
        decisions = {}
        records = []
        for candidates, state, result, error in await self.parallel(screen, groups.items()):
            for c in candidates:
                decisions[(c['category'], c['index'])] = state
            record = {'category': candidates[0]['category'], 'unit_ids': [c['unit']['unit_id'] for c in candidates],
                'decision': state, 'artifact_id': result['artifact_id'] if result else None, 'error': error}
            records.append(record)
            if state == '待核':
                self.note('E00', record['unit_ids'], error or '局部判断待核或失败；未排除')
            if result:
                self.controls.append(result['artifact_id'])
        self.save('local_screening', records, [candidate_id] + self.controls)
        ranges = {'A': [], 'B': [], 'C': []}
        for c in self.candidates:
            if decisions.get((c['category'], c['index'])) == '排除':
                continue
            ranges[c['category']].extend(self.index.complete_units(c['index']))
        located = []
        for category, units in ranges.items():
            merged = []
            for unit in sorted(units, key=lambda u: (u.element.order, u.start, u.end)):
                if merged and merged[-1].element.element_id == unit.element.element_id and unit.start <= merged[-1].end:
                    previous = merged[-1]
                    previous.end = max(previous.end, unit.end)
                    previous.text = unit.element.text[previous.start:previous.end]
                    previous.key = f'{unit.element.element_id}:{previous.start}:{previous.end}'
                else:
                    merged.append(unit)
            if merged:
                doc = self.index.document_for(merged)
                key = identity('selected_', [VERSION, category, [u.record() for u in merged]])
                located.append(LocatedRange(key, category, doc, ['position_scoped_match']))
        self.selected = [r.record() for r in located]
        aid = self.save('selected_ranges', {'ranges': self.selected}, [candidate_id] + self.controls)
        return located, aid

    async def anchors(self, task_material, source, parents, name):
        result = await self.runner.attempt('E02', task_material, parents, name)
        if not result:
            return None
        for round_no in range(2):
            try:
                if result.get('no_content'):
                    return result
                records = fields(result['text'])
                matches = [match_anchors(source, r) for r in records]
                aid = self.save('anchor_matches_' + identity('', [name, result['artifact_id']]),
                    {'records': records, 'matches': matches}, parents + [result['artifact_id']])
                if all(m['status'] == 'unique' for m in matches):
                    # Keep only exact source substrings; provenance is block-level.
                    excerpts = sorted({(m['matches'][0]['start'], m['matches'][0]['end']) for m in matches})
                    text = '\n\n'.join(source[a:b] for a, b in excerpts)
                    return {**result, 'text': text, 'artifact_id': aid}
                error = '锚点未唯一匹配：' + ','.join(m['status'] for m in matches)
            except ValueError as exc:
                error = str(exc)
            if round_no == 0:
                revised = await self.runner.attempt('E02', with_feedback(task_material, '上次匹配问题：' + error +
                    '\n请根据所给原文重新提供定位句。'),
                    parents + [result['artifact_id']], name + ':修正锚点')
                if revised:
                    result = revised
                    continue
            self.note('E02', name, error + '；回退对应类别的完整块整理')
            return None

    async def summarize(self, category, items):
        items = [item for item in items if not item.get('no_content')]
        if not items:
            return []
        task = 'E03-' + category
        current = items
        for level in range(self.config.summary_levels):
            # Pack full text records, not line slices that can detach conditions.
            groups, group = [], []
            for item in current:
                candidate = group + [item]
                body = '\n\n'.join(x['text'] for x in candidate)
                if group and (not self.runner.fits(task, body) or
                              self.runner.executor.budget.text_tokens(body) > self.config.text_batch_tokens):
                    groups.append(group)
                    group = []
                group.append(item)
            if group:
                groups.append(group)
            outputs = []
            for n, group in enumerate(groups):
                body = '\n\n'.join(x['text'] for x in group)
                result = await self.runner.attempt(task, body, [x['artifact_id'] for x in group], f'{category}汇总:{level}:{n}')
                if result:
                    if not result.get('no_content'):
                        outputs.append(result)
                else:
                    # Preserve texts as volumes; never silently drop a failed group.
                    outputs.extend(group)
            if not outputs or (len(groups) == 1 and len(outputs) == 1):
                return outputs
            if len(outputs) >= len(current) and sum(len(x['text']) for x in outputs) >= sum(len(x['text']) for x in current):
                self.note(task, category, '汇总不能继续安全归并；保留完整分册')
                return outputs
            current = outputs
        self.note(task, category, '达到汇总层数上限；保留完整分册')
        return current
