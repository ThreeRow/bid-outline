"""Conservative paragraph/list context; ownership is never extended by context."""
import re

LIST_ITEM = re.compile(r'^\s*(?:\d+[.、)）]|[（(]?[一二三四五六七八九十]+[)）、.])')
CONDITION = re.compile(r'^\s*(?:仅|如果|若|当|在[^\n。；：:]{0,40}时)[^\n。；：:]*[\n。；：:]')


def list_groups(document):
    elements = sorted(document.elements, key=lambda e:e.order)
    groups = []
    for index, leader in enumerate(elements):
        if leader.kind == 'table' or not leader.text.rstrip().endswith(('：', ':')):
            continue
        group = [leader]
        for element in elements[index+1:]:
            if element.section_path != leader.section_path or element.kind == 'table' or not LIST_ITEM.match(element.text):
                break
            group.append(element)
        if len(group) > 1:
            groups.append(group)
    return groups


def attach_paragraph_context(document, chunk):
    from .chunks import _slice_ref
    elements = {e.element_id:e for e in document.elements}
    notes = []
    # Only own-element leading conditions or structurally adjacent list groups.
    for ref in chunk.owned_refs:
        for eid in ref.element_ids:
            element = elements[eid]
            if element.kind == 'table':
                continue
            span = ref.reader_ref.get('element_text_range')
            end = span['end'] if span else (
                ref.end-element.source_ref.start if ref.end is not None and element.source_ref.start is not None else len(element.text))
            start = span['start'] if span else (
                ref.start-element.source_ref.start if ref.start is not None and element.source_ref.start is not None else 0)
            for left, right in protected_list_ranges(element.text):
                lead_end = element.text.find('\n', left, right) + 1
                if left < lead_end <= start < right:
                    notes.append({'text':element.text[left:lead_end].rstrip('\n'),
                                  'source_ref':_slice_ref(element,left,lead_end).model_dump(mode='json'),
                                  'candidate_scope':{'element_id':eid,'start':max(start,left),'end':min(end,right)},
                                  'status':'候选列表共同引导句：仅所列候选负责范围，适用关系待语义确认'})
            match = CONDITION.match(element.text)
            boundary = element.text.find('\n\n',match.end()) if match else -1
            condition_end = boundary if boundary >= 0 else len(element.text)
            if match and match.end() <= start < condition_end:
                notes.append({'text':match[0], 'source_ref':_slice_ref(element,0,match.end()).model_dump(mode='json'),
                              'candidate_scope':{'element_id':eid,'start':start,'end':min(end,condition_end)},
                              'status':'候选共同条件：同一正文元素首段的引导句，适用范围待语义确认，不自动强加义务'})
    owned = {eid for ref in chunk.owned_refs for eid in ref.element_ids}
    for group in list_groups(document):
        leader = group[0]
        if leader.element_id in owned or not owned & {e.element_id for e in group[1:]}:
            continue
        notes.append({'text':leader.text,'source_ref':leader.source_ref.model_dump(mode='json'),
                      'candidate_scope':{'element_ids':sorted(owned & {e.element_id for e in group[1:]})},
                      'status':'候选列表引导句：仅所列同章连续编号列表元素，适用范围待语义确认'})
    # Recompute for children rather than blindly inheriting another child's scope.
    chunk.context_notes = notes
    for note in notes:
        from bid_outline.domain import SourceRef
        ref = SourceRef.model_validate(note['source_ref'])
        if ref not in chunk.context_refs:
            chunk.context_refs.append(ref)
    return chunk


def protected_list_ranges(text):
    """Recognizable multiline lists are packing groups, never hard ownership merges."""
    lines = text.splitlines(keepends=True)
    offset = 0
    ranges = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.rstrip().endswith(('：', ':')) and index+1 < len(lines) and LIST_ITEM.match(lines[index+1]):
            end = offset + len(line)
            stop = index+1
            while stop < len(lines) and LIST_ITEM.match(lines[stop]):
                end += len(lines[stop]); stop += 1
            ranges.append((offset,end))
        offset += len(line); index += 1
    return ranges
