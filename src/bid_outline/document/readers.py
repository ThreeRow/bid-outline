"""Adapters preserve reader evidence; neither pagination nor exact alignment is inferred."""
import hashlib
import json
from pathlib import Path
import re
from bid_outline.domain import Document, DocumentElement, SourceRef


def cell_text(cell):
    """Render original cell content, including nested tables in source order."""
    if cell.get('parts'):
        return '\n'.join(p.get('text', '') if p['kind'] == 'text' else '\n'.join(cell_text(c) for c in (p.get('table') or {}).get('cells', [])) for p in cell['parts'])
    return cell.get('text', '\n'.join(cell.get('paragraphs', [])))


def table_rows(table):
    """Build complete rows with source cell IDs and merged-parent context."""
    cells = table.get('cells', [])
    if not isinstance(cells, list):
        return []
    rows = []
    for row in sorted({c['row'] for c in cells}):
        owned = sorted([c for c in cells if c['row'] == row], key=lambda c: c.get('col', 0))
        inherited = [c for c in cells if c['row'] < row < c['row'] + c.get('row_span', 1)]
        rows.append({'row': row, 'text': ' | '.join(cell_text(c) for c in owned), 'cell_ids': [c['id'] for c in owned],
                     'header': bool(owned) and all(c.get('column_header', False) for c in owned),
                     'inherited_text': ' | '.join(cell_text(c) for c in inherited), 'inherited_cell_ids': [c['id'] for c in inherited]})
    return rows


def _json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def read_document(path: Path, reader: str = 'reading') -> Document:
    """Adapt a frozen reading export or explicitly identified OOXML sample."""
    path = Path(path)
    if reader == 'sample':
        return _sample(path)
    if reader != 'reading':
        raise ValueError('reader must be reading or sample')
    if path.suffix.lower() == '.docx':
        raise ValueError('DOCX must first be processed by document-reading/outline_gen/cli/read_document.py; supply its version=2 artifact directory')
    path = path / 'document.json' if path.is_dir() else path
    raw = path.read_bytes()
    data = json.loads(raw)
    if data.get('version') != 2 or 'corrected' not in data:
        raise ValueError('reading adapter requires version=2 with corrected fragments')
    corrected = data['corrected']
    digest_input = bytearray(raw)
    for name in ('validation.json', 'source_map.json', 'manifest.json'):
        if (path.parent / name).is_file():
            digest_input.extend(name.encode() + b'\0' + (path.parent / name).read_bytes())
    digest = hashlib.sha256(digest_input).hexdigest()
    docid = 'doc-' + digest[:20]
    diagnostics = list(corrected.get('issues', []))
    diagnostics.extend(data.get('issues', []))
    manifest_path = path.parent / 'manifest.json'
    manifest = _json(manifest_path) if manifest_path.is_file() else None
    if manifest is not None and manifest.get('status') not in ('completed', 'incomplete'):
        raise ValueError(f"reader manifest status {manifest.get('status')!r} cannot be adopted; document.json may be stale")
    validation = _json(path.parent / 'validation.json') if (path.parent / 'validation.json').is_file() else {}
    if not (path.parent / 'validation.json').is_file():
        diagnostics.append({'kind': 'missing_reader_validation', 'message': 'Reading validation is unavailable; completion has not been established.'})
    elif validation.get('status') in ('failed', 'running', 'cancelled'):
        raise ValueError(f"reader validation status {validation['status']!r} cannot be adopted")
    if manifest is not None and validation.get('status') and manifest['status'] != validation['status']:
        diagnostics.append({'kind': 'reader_status_mismatch', 'manifest_status': manifest['status'], 'validation_status': validation['status']})
    diagnostics.extend(validation.get('issues', []))
    mapping = _json(path.parent / 'source_map.json') if (path.parent / 'source_map.json').is_file() else {}
    fragment_sources = mapping.get('fragment_sources', {})
    if isinstance(fragment_sources, list):
        fragment_sources = {f['fragment_id']: f for f in fragment_sources}
    word_elements = {w['id']: w for w in mapping.get('word_elements', [])}
    aligned_words = {}
    for alignment in mapping.get('alignment', []):
        for bid in alignment.get('block_ids', []):
            aligned_words.setdefault(bid, []).extend(alignment.get('word_ids', []))
    blocks = {b['id']: b for b in data.get('blocks', [])}
    sections = {s['id']: s for s in corrected.get('sections', [])}
    def section_path(sid):
        result, seen = [], set()
        while sid:
            if sid in seen or sid not in sections:
                diagnostics.append({'kind': 'invalid_section_path', 'section_id': sid})
                break
            seen.add(sid)
            result.insert(0, sections[sid]['title'])
            sid = sections[sid].get('parent_id')
        return result
    elements = []
    for order, fragment in enumerate(corrected['fragments']):
        fid, bid = fragment['id'], fragment['block_id']
        block = blocks.get(bid, {})
        tid = fragment.get('table_id')
        reference = {'fragment_id': fid, 'block_id': bid, 'docling_ref': block.get('ref'),
                     'structure': fragment.get('structure') or block.get('structure', []),
                     'fragment': fragment}
        if fid in fragment_sources:
            reference['source_map'] = fragment_sources[fid]
        reference['word_elements'] = [word_elements[wid] for wid in aligned_words.get(bid, []) if wid in word_elements]
        metadata = {'role': fragment.get('role'), 'heading_status': fragment.get('heading_status')}
        text = fragment.get('text', '')
        precision = 'parsed_text'
        start, end = fragment.get('start_offset'), fragment.get('end_offset')
        if start is None or end is None:
            start = end = None
            precision = 'block'
        cells = []
        if tid:
            original = data.get('source_tables', {}).get(tid)
            derived = data.get('tables', {}).get(tid, {})
            rows = table_rows(original) if original else []
            metadata.update(source_table=original, table_rows=rows, derived_table=derived, text_origin='reader_table_derived')
            text = derived.get('text') or '\n'.join(r['text'] for r in rows)
            if not derived.get('text'):
                metadata['text_origin'] = 'source_cells_rendered'
            diagnostics.extend(derived.get('issues', []))
            if not original:
                diagnostics.append({'kind': 'missing_source_table', 'table_id': tid})
            if not text:
                diagnostics.append({'kind': 'missing_table_content', 'table_id': tid})
            start = end = None
            precision = 'table'
            cells = [c['id'] for c in original.get('cells', [])] if original else []
        elif fragment.get('kind') == 'image':
            diagnostics.append({'kind': 'image_not_read', 'fragment_id': fid})
            precision = 'block'
            start = end = None
        elif start is not None and (start < 0 or end < start or end > len(block.get('text', '')) or block.get('text', '')[start:end] != text):
            diagnostics.append({'kind': 'fragment_text_alignment_unverified', 'fragment_id': fid, 'block_id': bid})
            reference['reported_range'] = {'start': start, 'end': end}
            start = end = None
            precision = 'block'
        elements.append(DocumentElement(element_id=fid, kind=fragment['kind'], order=order, text=text,
            section_path=section_path(fragment.get('section_id') or fragment.get('chapter_id')),
            source_ref=SourceRef(document_id=docid, element_ids=[fid], table_id=tid, cell_ids=cells,
                                 start=start, end=end, precision=precision, reader_ref=reference), metadata=metadata))
    represented = {f['block_id'] for f in corrected['fragments']}
    block_ranges = {}
    for f in corrected['fragments']:
        block_ranges.setdefault(f['block_id'], []).append((f.get('start_offset', 0), f.get('end_offset', 0)))
    for bid, block in blocks.items():
        if bid not in represented:
            diagnostics.append({'kind': 'unrepresented_reader_block', 'block_id': bid, 'has_text': bool(block.get('text'))})
        elif block.get('text') and block.get('kind', 'paragraph') not in ('table', 'image'):
            ranges = sorted(block_ranges[bid])
            cursor = 0
            for start, end in ranges:
                if start != cursor:
                    diagnostics.append({'kind': 'reader_fragment_coverage_gap_or_overlap', 'block_id': bid, 'expected_start': cursor, 'actual_start': start})
                cursor = max(cursor, end)
            if cursor != len(block['text']):
                diagnostics.append({'kind': 'reader_fragment_coverage_gap_or_overlap', 'block_id': bid, 'covered_end': cursor, 'text_length': len(block['text'])})
    return Document(document_id=docid, source_hash=digest, reader_name='reading', reader_version='2', elements=elements,
                    diagnostics=diagnostics, metadata={'input_path': str(path.resolve()), 'reader_validation': validation,
                    'reader_source_map': mapping, 'semantic_quality': validation.get('semantic_quality', 'not_evaluated'),
                    'reader_manifest_present': manifest is not None,
                    'input_sha256': manifest.get('input_sha256') if manifest else None,
                    'reader_status': manifest['status'] if manifest else validation.get('status', 'unknown')})


def _sample(path):
    path = path / 'source_blocks.jsonl' if path.is_dir() else path
    raw = path.read_bytes()
    tables_path = path.parent / 'source_tables.json'
    digest = hashlib.sha256(raw + (tables_path.read_bytes() if tables_path.exists() else b'')).hexdigest()
    docid = 'sample-' + digest[:20]
    records = [json.loads(line) for line in raw.decode('utf-8-sig').splitlines() if line.strip()]
    tables = _json(tables_path) if tables_path.exists() else []
    table_map = {t['xml_path']: t for t in tables}
    groups = {}
    for rec in records:
        match = re.match(r'(.*/tbl\[\d+\])/tr\[(\d+)\]/tc\[(\d+)\]', rec.get('xml_path', ''))
        if match:
            groups.setdefault(match[1], []).append((rec, int(match[2]), int(match[3])))
    elements, emitted = [], set()
    for rec in records:
        match = re.match(r'(.*/tbl\[\d+\])/tr\[(\d+)\]/tc\[(\d+)\]', rec.get('xml_path', ''))
        if match:
            key = match[1]
            if key in emitted:
                continue
            emitted.add(key)
            table = table_map.get(key, {'id': 'table-' + rec['id']})
            cells = {}
            for item, row, col in groups[key]:
                cell = cells.setdefault((row, col), {'id': f'{table["id"]}:r{row}c{col}', 'row': row, 'col': col, 'paragraphs': [], 'row_span': 1})
                cell['paragraphs'].append(item.get('text', ''))
            merge_heads = {}
            for meta in table.get('cell_metadata', []):
                cell = cells.get((meta['row'], meta['cell']))
                if not cell:
                    continue
                cell['col_span'] = int(meta.get('gridSpan') or 1)
                col = meta['cell']
                if meta.get('vMerge') == 'continue' and col in merge_heads:
                    merge_heads[col]['row_span'] += 1
                elif meta.get('vMerge') == 'restart':
                    merge_heads[col] = cell
                else:
                    merge_heads.pop(col, None)
            original = {'id': table['id'], 'cells': list(cells.values())}
            rows = table_rows(original)
            text = '\n'.join(r['text'] for r in rows)
            ref = SourceRef(document_id=docid, element_ids=[rec['id']], table_id=table['id'], cell_ids=[c['id'] for c in cells.values()], precision='table', reader_ref={'xml_path': key, 'sample_block_ids': [r[0]['id'] for r in groups[key]]})
            metadata = {'source_table': original, 'table_rows': rows, 'text_origin': 'source_cells_rendered', 'sample_table_metadata': table}
            kind = 'table'
        else:
            text, kind = rec.get('text', ''), rec.get('kind', 'paragraph')
            ref = SourceRef(document_id=docid, element_ids=[rec['id']], start=0, end=len(text), precision='parsed_text', reader_ref={'xml_path': rec.get('xml_path'), 'sample_record': rec})
            metadata = {'text_origin': 'sample_ooxml'}
        elements.append(DocumentElement(element_id=rec['id'], kind=kind, order=len(elements), text=text, source_ref=ref, metadata=metadata))
    return Document(document_id=docid, source_hash=digest, reader_name='sample_ooxml', reader_version='1', elements=elements,
                    diagnostics=[{'kind': 'sample_adapter', 'message': 'Sample OOXML extraction; not production Docling; semantic structure unverified.'}], metadata={'input_path': str(path.resolve())})
