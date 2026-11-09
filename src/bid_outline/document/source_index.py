"""Ordered local units with qualified offsets into the reader rendering."""
from dataclasses import dataclass, field
import re

from bid_outline.domain import Document, DocumentElement

ITEM_START = re.compile(r'^\s*(?:序号\s*[:：]\s*[★☆▲△■]?\s*\d+|[★☆▲△■]?\s*\d+[、.．）)]|[（(]\d+[）)])')


@dataclass
class Unit:
    key: str
    element: DocumentElement
    start: int
    end: int
    text: str
    context: str = ''
    row: int | None = None
    cells: list[str] = field(default_factory=list)

    def record(self):
        ref = self.element.source_ref.model_copy(deep=True)
        ref.reader_ref['element_text_range'] = {'start': self.start, 'end': self.end}
        if ref.precision == 'parsed_text' and ref.start is not None:
            ref.start, ref.end = ref.start + self.start, ref.start + self.end
        if self.element.kind == 'table':
            ref.reader_ref['text_slice_basis'] = 'expanded_table_text'
            ref.reader_ref['cell_ids_scope'] = 'table_context; not precise slice attribution'
        return {'unit_id': self.key, 'element_id': self.element.element_id, 'text': self.text,
                'path': self.element.section_path, 'context': self.context, 'row': self.row,
                'source_ref': ref.model_dump(mode='json')}


class SourceIndex:
    def __init__(self, document):
        self.document = document
        self.ordered_elements = []
        fallback = []
        for element in sorted(document.elements, key=lambda e: e.order):
            if element.section_path:
                fallback = element.section_path
            elif element.kind == 'paragraph' and (element.metadata.get('role') in {'heading', 'title'} or
                    re.match(r'^\s*第[一二三四五六七八九十百\d]+[章篇部分节]', element.text)) and len(element.text) < 100:
                fallback = [element.text]
            if not element.section_path and fallback:
                element = element.model_copy(update={'section_path': list(fallback),
                    'metadata': {**element.metadata, 'section_path_origin': 'structural_fallback'}})
            self.ordered_elements.append(element)
        self.element_positions = {e.element_id: n for n, e in enumerate(self.ordered_elements)}
        self.elements = {e.element_id: e for e in self.ordered_elements}
        self.sections = []
        self.units = []
        for element in self.ordered_elements:
            path = element.section_path
            if path and path not in self.sections:
                self.sections.append(path)
            headers = '\n'.join(r['text'] for r in element.metadata.get('table_rows', []) if r.get('header'))
            # Paragraphs/expanded table paragraphs are stable positions, not Word pages.
            for n, match in enumerate(re.finditer(r'[^\n]+(?:\n|$)', element.text)):
                text = match[0].rstrip('\r\n')
                if text.strip():
                    self.units.append(Unit(f'{element.element_id}:{match.start()}:{match.start()+len(text)}',
                        element, match.start(), match.start()+len(text), text, headers))

    def overview(self):
        return [{'path': path} for path in self.sections]

    def context(self, index, radius=1):
        unit = self.units[index]
        # Never pull an entire neighboring table across a chapter boundary.
        selected = [u for u in self.units[max(0, index-radius):index+radius+1]
                    if u.element.section_path == unit.element.section_path]
        return '\n'.join(u.text for u in selected)

    def full_range(self, index):
        """Expand to a bounded structural group, including the complete marked item."""
        target = self.units[index]
        if target.element.kind != 'table':
            # Several lines within a reader paragraph still form one complete item.
            return Unit(target.element.element_id + ':paragraph', target.element, 0,
                        len(target.element.text), target.element.text, target.context)
        indices = [index]
        # A paragraph is already a complete unit. Tables need surrounding row content;
        # use actual row identities when available instead of every row of the table.
        if target.element.kind == 'table':
            rows = target.element.metadata.get('table_rows', [])
            matching = [r for r in rows if target.text in r['text']]
            if len(matching) == 1:
                row = matching[0]
                # Match row text back to the rendering only when uniquely present.
                body = row['text']
                original = target.element.text
                if body and original.count(body) == 1:
                    start = original.index(body)
                    return Unit(target.key + ':row', target.element, start, start+len(body), body,
                        '\n'.join(filter(None, [target.context, row.get('inherited_text', '')])), row['row'], row['cell_ids'])
            # Single-cell long tables: extend through nearby content until next numbered item.
            for step in ((1,) if ITEM_START.match(target.text) else (-1, 1)):
                pos = index + step
                while 0 <= pos < len(self.units):
                    item = self.units[pos]
                    if item.element.element_id != target.element.element_id:
                        break
                    if ITEM_START.match(item.text):
                        if step == -1:
                            indices.append(pos)
                        break
                    indices.append(pos)
                    pos += step
        picked = [self.units[i] for i in sorted(indices)]
        start, end = picked[0].start, picked[-1].end
        return Unit(target.key + ':range', target.element, start, end,
                    target.element.text[start:end], target.context)

    def document_for(self, units):
        elements = []
        for order, unit in enumerate(units):
            ref = unit.record()['source_ref']
            # Unique projected identities prevent slices of one table colliding.
            ident = unit.key
            ref['reader_ref']['original_element_id'] = unit.element.element_id
            ref['reader_ref']['projection_source_range'] = {'start': unit.start, 'end': unit.end}
            ref['reader_ref']['element_text_range'] = {'start': 0, 'end': len(unit.text)}
            ref['element_ids'] = [ident]
            from bid_outline.domain import SourceRef
            elements.append(DocumentElement(element_id=ident, kind='paragraph', order=order,
                text=unit.text, section_path=unit.element.section_path,
                source_ref=SourceRef.model_validate(ref), metadata={'shared_context': unit.context}))
        return self.document.model_copy(update={'elements': elements})

    def complete_units(self, index):
        unit = self.full_range(index)
        output = [unit]
        element = unit.element
        # Preserve a bounded continuation, never the entire neighboring table.
        if (element.kind == 'table' and unit.end >= len(element.text.rstrip()) and
                unit.text and unit.text.rstrip()[-1] not in '。；;！？!?:：'):
            position = self.element_positions[element.element_id] + 1
            if position < len(self.ordered_elements):
                following = self.ordered_elements[position]
                if following.kind == 'table' and following.section_path == element.section_path:
                    first = next((u for u in self.units if u.element.element_id == following.element_id), None)
                    if first and not re.match(r'^\s*(?:序号|条款号|表头|项目|评分项目|参数项|[★☆▲△■]?\d+[、.．）)])', first.text):
                        output.append(first)
        return output
