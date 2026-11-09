"""Lexical candidates only: a hit is never a semantic relevance verdict."""
import re

KEYWORDS = ('技术', '方案', '实施', '运维', '交付', '验收', '评分', '评审', '投标文件格式', '应提供', '须提供', '编制', '附件')


def keyword_candidates(document):
    """Return full elements with lexical signals for targeted review."""
    return [e for e in document.elements if any(k in e.text for k in KEYWORDS)]


def owned_keyword_hit(document, chunk):
    """Match actual owned text/rows, excluding repeated headers and parent cells."""
    index = {e.element_id: e for e in document.elements}
    for ref in chunk.owned_refs:
        for eid in ref.element_ids:
            element = index[eid]
            span = ref.reader_ref.get('element_text_range')
            rows = element.metadata.get('table_rows', [])
            if rows and not span:
                cells = set(ref.cell_ids)
                text = '\n'.join(row['text'] for row in rows
                                 if (cells.intersection(row['cell_ids']) if cells else ref.precision == 'table'))
            elif span:
                text = element.text[span['start']:span['end']]
            elif ref.precision == 'parsed_text' and ref.start is not None and element.source_ref.start is not None:
                offset = element.source_ref.start
                text = element.text[ref.start - offset:ref.end - offset]
            else:
                text = element.text
            if any(keyword in text for keyword in KEYWORDS):
                return True
    return False


def lookup_references(document, clues: str, limit=5):
    """Rank source candidates without claiming semantic reference resolution."""
    if limit < 0:
        raise ValueError('limit must be nonnegative')
    terms = set(re.findall(r'[\w\u4e00-\u9fff]+', clues))
    # Chinese clues lack spaces: explicit chapter/attachment names and bigrams
    # provide candidates, with exact phrases ranking ahead of partial matches.
    bigrams = {clues[i:i+2] for i in range(len(clues)-1) if all('\u4e00' <= c <= '\u9fff' for c in clues[i:i+2])}
    ranked = []
    for element in document.elements:
        haystack = '\n'.join(element.section_path + [element.text])
        score = sum(10 * len(t) for t in terms if t in haystack) + sum(1 for t in bigrams if t in haystack)
        if score:
            ranked.append((-score, element.order, element))
    ranked.sort(key=lambda v: (v[0], v[1]))
    return [item[2] for item in ranked[:limit]]


def reference_candidates(document, clues, location=None):
    """Return complete candidate scopes and diagnostics; never silently take top N."""
    location = location or {}
    elements = sorted(document.elements, key=lambda e:e.order)
    sections = []
    for element in elements:
        path = tuple(element.section_path)
        if path and (not sections or sections[-1]['path'] != path):
            sections.append({'path':path, 'elements':[]})
        if path:
            sections[-1]['elements'].append(element)
    explicit_ids = [e for e in elements if re.search(r'(?<![A-Za-z0-9_.:/-])'+re.escape(e.element_id)+r'(?![A-Za-z0-9_.:/-])', clues)]
    groups = []
    strategy = 'lexical_fallback'
    if explicit_ids:
        strategy = 'explicit_element'
        groups = [{'path':tuple(e.section_path), 'elements':[e]} for e in explicit_ids]
    else:
        prefixes = {tuple(e.section_path[:i+1]) for e in elements for i in range(len(e.section_path))}
        matches = [path for path in prefixes if path[-1] and path[-1] in clues]
        if matches:
            # Prefer the most specific explicit heading. Mentioned ancestors
            # constrain same-name attachments rather than expanding to siblings.
            deepest = max(len(path) for path in matches)
            matches = [path for path in matches if len(path)==deepest]
            best = max(sum(name in clues for name in path[:-1]) for path in matches)
            matches = [path for path in matches if sum(name in clues for name in path[:-1])==best]
            strategy = 'explicit_section_or_attachment'
            for path in sorted(set(matches)):
                active = None
                for element in elements:
                    if tuple(element.section_path[:len(path)]) == path:
                        if active is None:
                            active = {'path':path, 'elements':[]}
                            groups.append(active)
                        active['elements'].append(element)
                    else:
                        active = None
        else:
            # Reader headings without a section path still bound an attachment
            # by the next heading, never by an arbitrary fixed character count.
            headings = [i for i,e in enumerate(elements) if e.kind == 'heading' and e.text.strip() and e.text.strip() in clues]
            for i in headings:
                stop = next((j for j in range(i+1,len(elements)) if elements[j].kind == 'heading'),len(elements))
                groups.append({'path':tuple(elements[i].section_path),'elements':elements[i:stop]})
            if groups:
                strategy = 'explicit_heading_range'
            elif any(word in clues for word in ('本节','本章','上述','前述','下述','本附件')) and location.get('section_path'):
                strategy = 'local_reference_candidate'
                groups = [s for s in sections if tuple(location['section_path']) == s['path']]
            else:
                # Keep all lexical hits; complete bounded scopes are budget-checked
                # upstream. No global top-five omission is disguised as absence.
                ranked = lookup_references(document, clues, limit=len(elements))
                groups = [{'path':tuple(e.section_path), 'elements':[e]} for e in ranked]
    origin_path = tuple(location.get('section_path', []))
    origin_order = location.get('order')
    groups.sort(key=lambda g:(0 if origin_path and g['path'][:len(origin_path)]==origin_path else 1,
                              min(abs(e.order-origin_order) for e in g['elements']) if origin_order is not None else 0))
    selected = list({e.element_id:e for group in groups for e in group['elements']}.values())
    return {'elements':selected, 'groups':[{'section_path':list(g['path']), 'element_ids':[e.element_id for e in g['elements']]} for g in groups],
            'strategy':strategy, 'ambiguous':len(groups)>1, 'truncated':False,
            'location':location, 'candidate_count':len(selected)}
