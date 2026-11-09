"""Semantic packing with exclusive ownership; repeated context never owns content."""
from bid_outline.domain import Chunk
from .paragraph_context import attach_paragraph_context, list_groups, protected_list_ranges


def _ranges(text, limit):
    start = 0
    groups = protected_list_ranges(text)
    while start < len(text):
        end = min(start + limit, len(text))
        if end < len(text):
            candidates = [text.rfind(mark, start + limit // 2, end) for mark in ('\n', '。', '；', '. ')]
            boundary = max(candidates)
            if boundary >= start:
                end = boundary + 1
        for left, right in groups:
            if left < end < right and right-left <= limit:
                end = left if left > start else right
                break
        yield start, end
        start = end
    if not text:
        yield 0, 0


def _slice_ref(element, start, end):
    ref = element.source_ref.model_copy(deep=True)
    # Derived text offsets describe the element rendering, never source cells.
    ref.reader_ref['element_text_range'] = {'start': start, 'end': end}
    if ref.precision == 'parsed_text' and ref.start is not None:
        ref.start, ref.end = ref.start + start, ref.start + end
    else:
        ref.start = ref.end = None
    return ref


def _table_parts(element, max_chars, selected_rows=None):
    rows = element.metadata['table_rows']
    headers = [r for r in rows if r.get('header')]
    # First row is context when the reader supplied no header designation; its
    # candidate status remains explicit rather than claiming a verified header.
    candidate = not headers
    if not headers and rows:
        headers = rows[:1]
    header_ids = [cid for r in headers for cid in r['cell_ids']]
    header_text = '\n'.join(r['text'] for r in headers)
    groups, current, count = [], [], 0
    for row in rows if selected_rows is None else selected_rows:
        size = len(row['text']) + len(row.get('inherited_text', '')) + len(header_text) + 32
        if current and count + size > max_chars:
            groups.append(current)
            current, count = [], 0
        current.append(row)
        count += size
    if current:
        groups.append(current)
    for group in groups:
        owned_ids = [cid for r in group for cid in r['cell_ids']]
        inherited_ids = list(dict.fromkeys(cid for r in group for cid in r.get('inherited_cell_ids', [])))
        context_ids = list(dict.fromkeys(cid for cid in header_ids + inherited_ids if cid not in owned_ids))
        text_parts = []
        if any(cid not in owned_ids for cid in header_ids):
            text_parts.append(('首行候选上下文：' if candidate else '表头上下文：') + header_text)
        inherited = list(dict.fromkeys(r.get('inherited_text', '') for r in group if r.get('inherited_text')))
        if inherited:
            text_parts.append('跨行共同父项：' + '\n'.join(inherited))
        text_parts.append('原始表格行：\n' + '\n'.join(r['text'] for r in group))
        ref = element.source_ref.model_copy(deep=True)
        ref.cell_ids = owned_ids
        ref.precision = 'cells'
        ref.start = ref.end = None
        ref.reader_ref['owned_rows'] = [r['row'] for r in group]
        ref.reader_ref['rendering'] = 'original_cells_not_model_derived_text'
        context = []
        if context_ids:
            ctx = element.source_ref.model_copy(deep=True)
            ctx.cell_ids = context_ids
            ctx.precision = 'cells'
            ctx.start = ctx.end = None
            context = [ctx]
        yield '\n'.join(text_parts), [ref], context


def chunk_document(document, max_chars=6000):
    """Pack semantic units while assigning each source range exactly once."""
    if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars < 1:
        raise ValueError('max_chars must be a positive integer')
    result, texts, owned = [], [], []
    section = []
    def append(text, refs, contexts=None, continuation=None):
        number = len(result)
        chunk = Chunk(chunk_id=f'{document.document_id}:chunk:{number + 1:06d}', text=text, owned_refs=refs,
                      context_refs=contexts or [], section_context=list(section), continuation_of=continuation, order=number)
        result.append(attach_paragraph_context(document, chunk))
        return chunk.chunk_id
    def flush():
        if owned:
            append('\n\n'.join(texts), list(owned))
            texts.clear()
            owned.clear()
    group_sizes = {group[0].element_id: sum(len(e.text)+2 for e in group) for group in list_groups(document)}
    for element in sorted(document.elements, key=lambda e: e.order):
        group_size = group_sizes.get(element.element_id, 0)
        if group_size and group_size <= max_chars and sum(map(len,texts))+2*len(texts)+group_size > max_chars:
            flush()
        if element.section_path != section:
            flush()
            section = list(element.section_path)
        rows = element.metadata.get('table_rows')
        if element.kind == 'table' and rows and (len(element.text) > max_chars or sum(len(r['text']) for r in rows) > max_chars):
            flush()
            first = None
            for text, refs, contexts in _table_parts(element, max_chars):
                cid = append(text, refs, contexts, first)
                first = first or cid
            continue
        if len(element.text) > max_chars:
            flush()
            first = None
            for start, end in _ranges(element.text, max_chars):
                cid = append(element.text[start:end], [_slice_ref(element, start, end)], continuation=first)
                first = first or cid
            continue
        if sum(map(len, texts)) + 2 * len(texts) + len(element.text) > max_chars:
            flush()
        texts.append(element.text)
        owned.append(element.source_ref.model_copy(deep=True))
    flush()
    return result


def split_chunk(document, chunk):
    """Bisect only this chunk's owned material, never rescan the whole element.

    Return [] when the remaining scope cannot be split without breaking a table
    row or when text is already small. Context carries no ownership.
    """
    import hashlib
    import json

    index = {e.element_id: e for e in document.elements}

    def element_for(ref):
        if len(ref.element_ids) != 1 or ref.element_ids[0] not in index:
            raise ValueError('adaptive split requires one known element per source reference')
        return index[ref.element_ids[0]]

    def text_range(element, ref):
        span = ref.reader_ref.get('element_text_range')
        if span:
            start, end = span['start'], span['end']
        elif ref.precision == 'parsed_text' and ref.start is not None and element.source_ref.start is not None:
            start, end = ref.start - element.source_ref.start, ref.end - element.source_ref.start
        else:
            start, end = 0, len(element.text)
        if not 0 <= start <= end <= len(element.text):
            raise ValueError('owned text range lies outside its element')
        return start, end

    def selected_table_rows(element, ref):
        if ref.reader_ref.get('element_text_range'):
            return []  # An expanded-text slice cannot be mapped back to cells.
        rows = element.metadata.get('table_rows', [])
        requested = set(ref.cell_ids)
        if not requested:
            return rows if ref.precision == 'table' else []
        selected = [r for r in rows if requested.intersection(r['cell_ids'])]
        if any(not set(r['cell_ids']) <= requested for r in selected):
            return []
        if {cid for r in selected for cid in r['cell_ids']} != requested:
            return []
        return selected

    def render(refs):
        texts, contexts = [], []
        for ref in refs:
            element = element_for(ref)
            rows = selected_table_rows(element, ref) if element.kind == 'table' else []
            if rows:
                text, _, ctx = next(_table_parts(element, 10**30, rows))
                texts.append(text)
                contexts.extend(ctx)
            else:
                start, end = text_range(element, ref)
                texts.append(element.text[start:end])
        return '\n\n'.join(texts), contexts

    def child(text, refs, contexts, ordinal):
        payload = json.dumps({'parent': chunk.chunk_id, 'owned_refs': [r.model_dump(mode='json') for r in refs]},
                             ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        digest = hashlib.sha256(payload.encode()).hexdigest()[:24]
        paragraph_refs = [note['source_ref'] for note in chunk.context_notes]
        inherited = [r.model_copy(deep=True) for r in chunk.context_refs if r.model_dump(mode='json') not in paragraph_refs]
        owned_ids = {eid for ref in refs for eid in ref.element_ids}
        spans = [{**s, 'element_ids': [eid for eid in s['element_ids'] if eid in owned_ids]}
                 for s in chunk.section_spans if owned_ids.intersection(s['element_ids'])]
        return attach_paragraph_context(document, Chunk(chunk_id=f'{chunk.chunk_id}:split:{digest}', text=text,
                     owned_refs=refs, context_refs=inherited + contexts,
                     section_context=list(chunk.section_context), section_spans=spans,
                     continuation_of=chunk.chunk_id, order=chunk.order * 2 + ordinal))

    refs = chunk.owned_refs
    if not refs:
        return []
    if len(refs) > 1:
        midpoint = len(refs) // 2
        children = []
        for ordinal, group in enumerate((refs[:midpoint], refs[midpoint:])):
            owned = [r.model_copy(deep=True) for r in group]
            text, contexts = render(owned)
            children.append(child(text, owned, contexts, ordinal))
        return children
    ref = refs[0]
    element = element_for(ref)
    if element.kind == 'table':
        rows = selected_table_rows(element, ref)
        if len(rows) <= 1:
            return []
        midpoint = len(rows) // 2
        result = []
        for ordinal, group in enumerate((rows[:midpoint], rows[midpoint:])):
            text, owned, contexts = next(_table_parts(element, 10**30, group))
            result.append(child(text, owned, contexts, ordinal))
        return result
    start, end = text_range(element, ref)
    if end - start <= 80:
        return []
    midpoint = start + (end - start) // 2
    candidates = [element.text.rfind(mark, start + (end-start)//4, midpoint) for mark in ('\n', '。', '；', '. ')]
    boundary = max(candidates)
    if boundary >= start:
        midpoint = boundary + 1
    result = []
    for ordinal, (left, right) in enumerate(((start, midpoint), (midpoint, end))):
        owned = _slice_ref(element, left, right)
        result.append(child(element.text[left:right], [owned], [], ordinal))
    return result
