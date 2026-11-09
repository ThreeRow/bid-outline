"""Deterministic candidate selection, never a claim of semantic extraction."""
import re
import hashlib
import json
from dataclasses import dataclass
from itertools import groupby

from bid_outline.domain import Document


RULES = {
    'A': ('投标人须知', '供应商须知', '须知前附表', '投标文件格式', '响应文件格式',
          '投标文件的格式', '响应文件的格式', '投标人提交文件须知',
          '投标文件内容', '文件构成', '文件组成', '文件的组成', '文件编制', '文件的编制', '技术部分中必须包括',
          '应提交的技术文件', '技术文件组成', '集中编制', '技术标部分', '商务技术文件'),
    'B': ('评标办法', '评标方法', '评审方法', '评审标准', '评分标准', '评分细则',
          '评标标准', '评定标办法', '定性评审', '技术评审', '打分细则'),
}
SUBMISSION = re.compile(
    r'(?:投标文件|响应文件|投标时|随投标|编制投标).{0,50}(?:提供|提交|列出|包括|包含|说明|承诺)'
    r'|(?:提供|提交|附入).{0,30}(?:投标文件|响应文件)'
    r'|(?:方案|计划).{0,12}(?:应包括|至少包括|需涵盖|内容包括)'
    r'|(?:提供|提交).{0,12}(?:承诺函|承诺书|方案|说明|证明材料)')


@dataclass
class LocatedRange:
    range_id: str
    category: str
    document: Document
    reasons: list[str]

    def record(self):
        return {'range_id': self.range_id, 'category': self.category, 'reasons': self.reasons,
                'element_ids': [e.element_id for e in self.document.elements],
                'source_refs': [e.source_ref.model_dump(mode='json') for e in self.document.elements]}


def normalized(text):
    return re.sub(r'\s+', '', text)


def submission_table(element):
    """Keep candidate rows with headers and inherited labels, not the whole demand table."""
    rows = element.metadata.get('table_rows', [])
    hits = [i for i, row in enumerate(rows) if SUBMISSION.search(normalized(row['text']))]
    if not hits:
        return element
    chosen = {j for i in hits for j in range(max(0, i-1), min(len(rows), i+2))}
    chosen.update(i for i, row in enumerate(rows) if row.get('header'))
    kept = [row for i, row in enumerate(rows) if i in chosen]
    source = element.source_ref.model_copy(deep=True)
    source.cell_ids = list(dict.fromkeys(cid for row in kept for cid in row['cell_ids']))
    source.reader_ref['context_cell_ids'] = list(dict.fromkeys(
        cid for row in kept for cid in row.get('inherited_cell_ids', [])))
    source.start = source.end = None
    source.precision = 'table'
    source.reader_ref['candidate_rows'] = [row['row'] for row in kept]
    return element.model_copy(update={
        'text': '\n'.join((row.get('inherited_text', '') + '\n' + row['text']).strip() for row in kept),
        'metadata': {**element.metadata, 'table_rows': kept}, 'source_ref': source})


def locate(document):
    """Select sections for A/B, local submission contexts for C; merge overlaps."""
    elements = sorted(document.elements, key=lambda e: e.order)
    selected = {category: {} for category in 'ABC'}
    for _, grouped in groupby(range(len(elements)), lambda i: tuple(elements[i].section_path)):
        indices = list(grouped)
        path = normalized(' / '.join(elements[indices[0]].section_path))
        for category, words in RULES.items():
            hits = [word for word in words if word in path]
            if hits:
                for i in indices:
                    selected[category].setdefault(i, set()).update('章节:' + h for h in hits)
        for i in indices:
            text = normalized(elements[i].text)
            for category, words in RULES.items():
                hits = [word for word in words if word in text]
                if hits:
                    # Body hits retain adjacent units even across a table boundary.
                    for j in range(max(0, i-1), min(len(elements), i+3)):
                        selected[category].setdefault(j, set()).update('正文:' + h for h in hits)
            if SUBMISSION.search(text):
                for j in range(max(0, i-1), min(len(elements), i+2)):
                    selected['C'].setdefault(j, set()).add('正文:提交或方案内容线索')
    output = []
    for category, positions in selected.items():
        for _, group in groupby(enumerate(sorted(positions)), lambda item: item[1]-item[0]):
            indices = [item[1] for item in group]
            subset = document.model_copy(update={'elements': [
                submission_table(elements[i]) if category == 'C' else elements[i] for i in indices]})
            key = hashlib.sha256(json.dumps([category, [e.element_id for e in subset.elements]]).encode()).hexdigest()[:20]
            output.append(LocatedRange('range_' + key,
                                       category, subset, sorted({r for i in indices for r in positions[i]})))
    return output
