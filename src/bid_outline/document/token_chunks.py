"""Chapter-first packing, with token allowances and complete-request verification."""
import hashlib
import json
from itertools import groupby

from bid_outline.domain import Chunk
from .chunks import split_chunk
from .paragraph_context import attach_paragraph_context, list_groups


def token_chunks(document, *, allowance, text_tokens, fits):
    """No clipping: an indivisible oversize leaf survives for an explicit failure.

    allowance subtracts each task's fixed prompt and the candidate's dynamic
    context BEFORE packing. fits verifies the actual complete rendered request.
    """
    elements = sorted(document.elements, key=lambda e: e.order)
    lookup = {e.element_id: e for e in elements}

    def finish(chunk):
        paths = []
        for ref in chunk.owned_refs:
            for eid in ref.element_ids:
                path = lookup[eid].section_path
                if not paths or paths[-1]['section_path'] != path:
                    paths.append({'section_path': path, 'element_ids': []})
                if eid not in paths[-1]['element_ids']:
                    paths[-1]['element_ids'].append(eid)
        chunk.section_spans = paths if len(paths) > 1 else []
        common = list(paths[0]['section_path']) if paths else []
        for item in paths[1:]:
            path = item['section_path']
            common = common[:next((i for i, (a, b) in enumerate(zip(common, path)) if a != b),
                                   min(len(common), len(path)))]
        chunk.section_context = common
        payload = [r.model_dump(mode='json') for r in chunk.owned_refs]
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:24]
        chunk.chunk_id = f'{document.document_id}:tokens:{digest}'
        return chunk

    def make(group):
        chunk = Chunk(chunk_id='planning', text='\n\n'.join(e.text for e in group),
                      owned_refs=[e.source_ref.model_copy(deep=True) for e in group])
        return finish(attach_paragraph_context(document, chunk))

    def merge(chunks):
        if len(chunks) == 1:
            return chunks[0]
        merged = Chunk(chunk_id='planning', text='\n\n'.join(c.text for c in chunks),
                       owned_refs=[r for c in chunks for r in c.owned_refs],
                       context_refs=[r for c in chunks for r in c.context_refs],
                       context_notes=[n for c in chunks for n in c.context_notes])
        return finish(merged)

    def accepts(chunk):
        capacity = allowance(chunk)
        # Unit count guides packing, but template token boundaries are not
        # additive. The complete request remains the only admission authority.
        estimate = text_tokens(chunk.text)
        if estimate > capacity + 32:
            return False
        return fits(chunk)

    def leaf(chunk):
        if accepts(chunk):
            return [chunk]
        children = split_chunk(document, chunk)
        if not children:
            return [chunk]
        return [part for child in children for part in leaf(finish(child))]

    def pack(chunks):
        output, pending = [], []
        for chunk in chunks:
            candidate = merge(pending + [chunk])
            if pending and not accepts(candidate):
                output.append(merge(pending))
                pending = [chunk]
            else:
                pending.append(chunk)
        if pending:
            output.append(merge(pending))
        return output

    list_by_first = {g[0].element_id: g for g in list_groups(document)}

    def descend(group, level):
        whole = make(group)
        if accepts(whole):
            return [whole]
        if any(len(e.section_path) > level for e in group):
            pieces = []
            for _, children in groupby(group, lambda e: tuple(e.section_path[:level+1])):
                pieces.extend(descend(list(children), level+1))
            return pack(pieces)
        # No subchapters left: retain whole list groups before splitting their
        # paragraphs, and retain whole tables before splitting owned rows.
        pieces, index = [], 0
        while index < len(group):
            unit = list_by_first.get(group[index].element_id, [group[index]])
            if [e.element_id for e in group[index:index+len(unit)]] != [e.element_id for e in unit]:
                unit = [group[index]]
            pieces.extend(leaf(make(unit)))
            index += len(unit)
        return pack(pieces)

    output = []
    # Do not fill spare capacity with an unrelated top-level chapter.
    for _, group in groupby(elements, lambda e: tuple(e.section_path[:1])):
        output.extend(descend(list(group), 1))
    for i, chunk in enumerate(output):
        chunk.order = i
    return output
