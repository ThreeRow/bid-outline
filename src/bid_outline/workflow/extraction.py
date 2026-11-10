"""Position-first collaborative extraction, with plain-text business results."""
import json
import re

from bid_outline.document.anchors import fields, match_anchors
from bid_outline.document.locate import position_hits
from bid_outline.document.location_rules import LOCAL, DIRECT, VERSION, compact, position_categories
from bid_outline.document.source_index import SourceIndex
from bid_outline.document.section_blocks import SectionBlocks
from bid_outline.export.markdown import write_atomic
from bid_outline.model.text_tasks import TextMaterial, with_feedback, CATEGORY_CONTENT
from bid_outline.model.packing import pack_items
from .common import identity, bounded_map


class Extraction:
    def __init__(self, runner, document, source_id):
        self.runner, self.index, self.source_id = runner, SourceIndex(document), source_id
        self.config = runner.config
        self.blocks = SectionBlocks(self.index)
        self.model_positions = []
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
            found = self.blocks.matching(title, compact)
            if len(found) != 1:
                errors.append(f'{title}匹配{len(found)}个位置')
            else:
                matches.append((found[0].key, category))
        return matches, errors

    async def directory(self):
        overview = '\n'.join(' / '.join(node.path) for node in self.blocks.nodes.values())
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
                retry = await self.runner.attempt('E01', TextMaterial(page, feedback='上次定位问题：\n' + '\n'.join(errors)),
                    [self.source_id, result['artifact_id']], '目录定位澄清')
                if retry:
                    more, errors = self.parse_hints(retry['text'])
                    found.extend(more)
                    result = retry
            if errors:
                self.note('E01', '目录定位', '；'.join(errors))
            self.controls.append(result['artifact_id'])
            for key, category in found:
                self.model_positions.append((key, category))
                path = self.blocks.nodes[key].path
                for original in self.positions:
                    if original[:len(path)] == tuple(path):
                        self.positions[original].add(category)

    async def locate(self):
        self.save('section_index', {'sections': self.blocks.overview(), 'rules_version': VERSION})
        await self.directory()
        self.candidates = position_hits(self.index, self.positions)
        candidate_id = self.save('rule_candidates', {'positions': [
            {'path': list(path), 'categories': sorted(categories)} for path, categories in self.positions.items()],
            'candidates': self.candidates}, [self.source_id] + self.controls)
        selected = {}
        def select(key, category, reason):
            selected.setdefault((key, category), []).append(reason)
        for node in self.blocks.nodes.values():
            for category, pattern in DIRECT.items():
                if pattern.search(compact(node.path[-1])):
                    select(node.key, category, '明确章节名称规则')
        for key, category in self.model_positions:
            select(key, category, 'E01目录辅助定位')

        def covered(eid, category):
            return any((key, category) in selected for key in self.blocks.ancestry.get(eid, []))
        # Only generic chapter locations need local signal decisions. A direct
        # chapter remains complete even if one of its lines looks irrelevant.
        broad = [c for c in self.candidates if not covered(c['unit']['element_id'], c['category'])]
        # Merge identical local contexts for the same category; one decision
        # controls just these hits, never erases other hits in the section.
        groups = {}
        for candidate in broad:
            if not candidate['weak']:
                continue
            unit = self.index.units[candidate['index']]
            section_key = self.blocks.element_section[unit.element.element_id]
            context = '\n'.join(other.text for other in self.index.units[
                max(0, candidate['index']-1):candidate['index']+2]
                if self.blocks.element_section.get(other.element.element_id) == section_key)
            key = (candidate['category'], section_key, context)
            groups.setdefault(key, []).append(candidate)
        async def screen(item):
            (category, section_key, context), candidates = item
            path = self.blocks.nodes[section_key].path
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
        for c in broad:
            if decisions.get((c['category'], c['index'])) == '排除':
                continue
            key = self.blocks.element_section[c['unit']['element_id']]
            select(key, c['category'], '位置内线索：' + '、'.join(c['hits']))
        # Same-category descendants are already owned by a selected ancestor.
        # Keep their evidence but never process overlapping body twice.
        collapsed = {}
        for (key, category), reasons in selected.items():
            node = self.blocks.nodes[key]
            ancestors = []
            parent = node.parent
            while parent:
                ancestors.append(parent)
                parent = self.blocks.nodes[parent].parent
            owner = next((p for p in reversed(ancestors) if (p, category) in selected), key)
            collapsed.setdefault((owner, category), []).extend(reasons)
        located = [self.blocks.located(key, category, list(dict.fromkeys(reasons)))
                   for (key, category), reasons in sorted(collapsed.items(),
                       key=lambda item:(self.blocks.nodes[item[0][0]].positions[0], item[0][1]))]
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
                revised = await self.runner.attempt('E02', with_feedback(task_material, '上次匹配问题：' + error),
                    parents + [result['artifact_id']], name + ':修正锚点')
                if revised:
                    result = revised
                    continue
            self.note('E02', name, error + '；回退对应类别的完整块整理')
            return None

    async def summarize(self, category, items, *, force=False):
        items = [item for item in items if not item.get('no_content')]
        if not items:
            return []
        if len(items) == 1 and not force and not items[0].get('requires_consolidation'):
            return items
        task = 'E03-' + category
        def material(group):
            return '\n\n'.join(('来源区域：' + x['source_heading'] + '\n' if x.get('source_heading') else '')
                                 + x['text'] for x in group)

        def fits(group):
            body = material(group)
            return self.runner.perception_fits(task, body)

        current = items
        for level in range(self.config.summary_levels):
            groups = pack_items(current, fits)

            async def process(pair):
                n, group = pair
                if not fits(group):
                    self.note(task, category, '完整整理记录超过感知阈值或请求预算；保留完整分册')
                    return group, False
                result = await self.runner.attempt(task, material(group),
                    [x['artifact_id'] for x in group], f'{category}汇总:{level}:{n}')
                if result is None:
                    return group, False
                if result.get('no_content'):
                    return [], True
                return [{**result, 'consolidated': True}], True

            processed = await self.parallel(process, enumerate(groups))
            outputs = [item for group, _ in processed for item in group]
            if not all(success for _, success in processed):
                # Do not silently bypass or repeatedly re-summarize failed volumes.
                return outputs
            if not outputs or len(groups) == 1:
                return outputs
            if len(outputs) >= len(current) and sum(len(x['text']) for x in outputs) >= sum(len(x['text']) for x in current):
                self.note(task, category, '汇总不能继续安全归并；保留完整分册')
                return outputs
            current = outputs
        self.note(task, category, '达到汇总层数上限；保留完整分册')
        return current
