"""Chapter identity and complete source blocks; keywords never select body lines."""
from dataclasses import dataclass, field

from bid_outline.document.locate import LocatedRange


@dataclass
class Section:
    key: str
    path: list[str]
    parent: str | None
    positions: list[int] = field(default_factory=list)


class SectionBlocks:
    def __init__(self, index):
        self.index = index
        self.nodes = {}
        self.element_section = {}
        self.ancestry = {}
        catalog = {s['id']: s for s in index.document.metadata.get('reader_sections', [])}
        active = []
        for position, element in enumerate(index.ordered_elements):
            path = element.section_path
            if not path:
                continue
            fragment = element.source_ref.reader_ref.get('fragment', {})
            sid = fragment.get('section_id') or fragment.get('chapter_id')
            ids, seen = [], set()
            while sid and sid in catalog and sid not in seen:
                seen.add(sid)
                ids.insert(0, sid)
                sid = catalog[sid].get('parent_id')
            chain = []
            for depth, title in enumerate(path):
                if len(ids) == len(path):
                    explicit = ids[depth]
                elif depth == len(path)-1:
                    explicit = fragment.get('section_id') or fragment.get('chapter_id')
                elif depth == 0:
                    explicit = fragment.get('chapter_id')
                else:
                    explicit = None
                parent = chain[-1] if chain else None
                if explicit:
                    key = 'reader:' + explicit
                elif (depth < len(active) and self.nodes[active[depth]].path == path[:depth+1]
                      and self.nodes[active[depth]].parent == parent
                      and not (depth == len(path)-1 and element.text.strip() == title.strip()
                               and element.metadata.get('role') in {'heading','title'})):
                    key = active[depth]
                else:
                    key = f'inferred:{position}:{depth}'
                if key not in self.nodes:
                    self.nodes[key] = Section(key, path[:depth+1], parent)
                node = self.nodes[key]
                if node.path != path[:depth+1] or node.parent != parent:
                    raise ValueError('reader section identity has inconsistent ancestry: ' + key)
                node.positions.append(position)
                chain.append(key)
            active = chain
            self.element_section[element.element_id] = chain[-1]
            self.ancestry[element.element_id] = chain

    def overview(self):
        return [{'section_key': n.key, 'path': n.path, 'parent': n.parent,
                 'element_count': len(n.positions)} for n in self.nodes.values()]

    def matching(self, title, compact):
        return [node for node in self.nodes.values() if compact(title) in
                (compact(' / '.join(node.path)), compact(node.path[-1]))]

    def document_for(self, key):
        node = self.nodes[key]
        elements = []
        for position in node.positions:
            element = self.index.ordered_elements[position]
            ref = element.source_ref.model_copy(deep=True)
            # Keep reader-expanded text as the one canonical body during all
            # later splits. Never switch to pipe-rendered original table rows.
            ref.reader_ref.update(original_element_id=element.element_id,
                element_text_range={'start': 0, 'end': len(element.text)},
                projection_source_range={'start': 0, 'end': len(element.text)})
            metadata = dict(element.metadata)
            if element.kind == 'table':
                ref.reader_ref.update(text_slice_basis='expanded_table_text',
                    cell_ids_scope='whole_table_context; not precise text slice attribution')
                metadata.update(original_kind='table', table_rows=[])
                kind = 'expanded_table_text'
            else:
                kind = element.kind
            elements.append(element.model_copy(update={'kind':kind, 'source_ref':ref, 'metadata':metadata}))
        return self.index.document.model_copy(update={'elements':elements,
            'metadata':{**self.index.document.metadata, 'candidate_section_key':key,
                        'candidate_section_path':node.path, 'source_scope':'complete_section'}})

    def located(self, key, category, reasons):
        return LocatedRange(key + ':' + category, category, self.document_for(key), reasons)
