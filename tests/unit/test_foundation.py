import json
from pathlib import Path
import tempfile
import pytest
from bid_outline.domain import Document, DocumentElement, SourceRef, OutlineNode, validate_outline
from bid_outline.document import read_document, chunk_document, lookup_references
from bid_outline.storage import ArtifactStore, RunRepository, VersionConflict


def directory():
    # Retained intentionally: no automatic recursive cleanup.
    return Path(tempfile.mkdtemp(prefix='bid-outline-foundation-'))


def test_ranges_cover_unsectioned_and_long_text():
    text = '保留条件；' * 250
    e = DocumentElement(element_id='f', kind='paragraph', order=0, text=text, source_ref=SourceRef(document_id='d', element_ids=['f'], start=70, end=70+len(text), precision='parsed_text'))
    document = Document(document_id='d', source_hash='x', reader_name='test', elements=[e])
    chunks = chunk_document(document, 67)
    assert ''.join(c.text for c in chunks) == text
    ranges = [(c.owned_refs[0].start, c.owned_refs[0].end) for c in chunks]
    assert ranges[0][0] == 70 and ranges[-1][1] == 70 + len(text)
    assert all(a[1] == b[0] for a, b in zip(ranges, ranges[1:]))
    assert all(c.continuation_of == chunks[0].chunk_id for c in chunks[1:])
    assert all(not c.context_refs for c in chunks)


def test_reading_corrected_tables_and_diagnostics():
    path = directory()
    cells = [{'id': 'c1', 'row': 0, 'col': 0, 'paragraphs': ['评审表头'], 'column_header': True},
             {'id': 'c2', 'row': 1, 'col': 0, 'paragraphs': ['共同条件'], 'row_span': 3}]
    cells += [{'id': f'c{i+3}', 'row': i+1, 'col': 1, 'paragraphs': ['方案细节' * 10]} for i in range(3)]
    data = {'version': 2, 'corrected': {'sections': [], 'fragments': [
        {'id': 'f1', 'block_id': 'b1', 'kind': 'paragraph', 'text': '纠正正文', 'start_offset': 3, 'end_offset': 7},
        {'id': 'f2', 'block_id': 'b2', 'kind': 'table', 'table_id': 't', 'text': ''}], 'issues': [{'code': 'heading_review'}]},
        'blocks': [{'id': 'b1', 'text': 'xxx纠正正文', 'structure': [{'source': 'word', 'word_id': 'w1'}]}, {'id': 'b2', 'text': ''}],
        'source_tables': {'t': {'cells': cells}}, 'tables': {'t': {'text': '模型派生描述' * 40, 'issues': [{'code': 'table_review'}]}}}
    (path/'document.json').write_text(json.dumps(data), encoding='utf-8')
    doc = read_document(path)
    assert doc.elements[0].text == '纠正正文'
    assert doc.elements[0].source_ref.start == 3
    table = doc.elements[1]
    assert table.text.startswith('模型派生') and table.source_ref.start is None
    assert table.metadata['source_table']['cells'] == cells
    assert {d['code'] for d in doc.diagnostics if 'code' in d} == {'heading_review', 'table_review'}
    assert any(d.get('kind') == 'reader_fragment_coverage_gap_or_overlap' for d in doc.diagnostics)
    chunks = chunk_document(doc, 85)
    owned = [cid for c in chunks for r in c.owned_refs for cid in r.cell_ids]
    assert sorted(owned) == sorted(c['id'] for c in cells)
    last = chunks[-1]
    assert '共同条件' in last.text and '评审表头' in last.text
    assert last.context_refs and 'c2' in last.context_refs[0].cell_ids
    assert 'page' not in table.source_ref.model_dump()
    assert lookup_references(doc, '纠正正文')[0].element_id == 'f1'


def test_immutable_store_and_transactional_recovery():
    path = directory()
    store = ArtifactStore(path)
    aid = store.put('response', {'text': '保留完整响应'}, task_id='t')
    assert store.put('response', {'text': '保留完整响应'}, task_id='t') == aid
    repo = RunRepository(path)
    repo.save_run({'run_id': 'r'})
    repo.upsert_task('t', 'P01', 'fp', 'running')
    repo.record_attempt('call', 't', {'request': {'messages': ['request']}})
    repo.record_attempt('call', 't', {'response': 'saved'})
    repo.close()
    recovered = RunRepository(path)
    assert recovered.get_task('t')['status'] == 'running'
    assert recovered.list_attempts()[0]['request']['messages'] == ['request']
    recovered.commit_task('t', aid)
    assert recovered.get_task('t')['accepted_artifact_id'] == aid
    other = store.put('response', {'text': 'different'})
    with pytest.raises(ValueError):
        recovered.commit_task('t', other)
    recovered.close()
    (path/'artifacts'/f'{aid}.json').write_text('{}')
    with pytest.raises(ValueError, match='hash'):
        store.get(aid)


def test_outline_cas_validates_versions():
    path = directory()
    repo = RunRepository(path)
    repo.commit_outline({'version_id': 'v1', 'nodes': [{'node_id': 'a', 'title': '技术方案'}]}, None)
    repo.commit_outline({'version_id': 'v2', 'parent_version_id': 'v1', 'nodes': [{'node_id': 'a', 'title': '技术方案'}]}, 'v1')
    with pytest.raises(VersionConflict):
        repo.commit_outline({'version_id': 'v3', 'parent_version_id': 'v1'}, 'v1')
    assert repo.get_outline()['version_id'] == 'v2'
    assert repo.get_outline_version('v1')['parent_version_id'] is None
    assert repo.get_outline_version('missing') is None
    with pytest.raises(ValueError):
        validate_outline([OutlineNode(node_id='a', title='a', parent_id='b'), OutlineNode(node_id='b', title='b', parent_id='a')])
    with pytest.raises(ValueError):
        validate_outline([OutlineNode(node_id='a', title='a'), OutlineNode(node_id='b', title='b')])
    repo.close()


def test_historical_reading_and_sample():
    base = Path(__file__).resolve().parents[2]
    doc = read_document(base/'document-reading/examples/shenzhen')
    assert len(doc.elements) >= 903 and doc.diagnostics
    owned = {eid for c in chunk_document(doc, 6000) for r in c.owned_refs for eid in r.element_ids}
    assert owned == {e.element_id for e in doc.elements}
    sample = read_document(base/'test_materials', reader='sample')
    assert sample.reader_name == 'sample_ooxml'
    assert any(e.kind == 'table' and e.metadata['table_rows'] for e in sample.elements)


def test_unavailable_image_and_unverified_alignment():
    path = directory()
    data = {'version': 2, 'corrected': {'sections': [], 'fragments': [
        {'id': 'f1', 'block_id': 'b1', 'kind': 'image', 'text': ''},
        {'id': 'f2', 'block_id': 'b2', 'kind': 'paragraph', 'text': '派生文字', 'start_offset': 0, 'end_offset': 4}]},
        'blocks': [{'id': 'b1', 'kind': 'image', 'text': ''}, {'id': 'b2', 'kind': 'paragraph', 'text': '原始文字'}]}
    (path/'document.json').write_text(json.dumps(data), encoding='utf-8')
    doc = read_document(path)
    assert {d['kind'] for d in doc.diagnostics} >= {'image_not_read', 'fragment_text_alignment_unverified'}
    assert doc.elements[1].source_ref.precision == 'block'
    assert doc.elements[1].source_ref.start is None
    assert {r.element_ids[0] for c in chunk_document(doc) for r in c.owned_refs} == {'f1', 'f2'}


def test_adaptive_split_preserves_sliced_paragraph_scope():
    from bid_outline.document import split_chunk
    text = '不应重复开头。' * 100 + '应保留尾部条件；' * 100
    element = DocumentElement(element_id='f', kind='paragraph', order=0, text=text,
        source_ref=SourceRef(document_id='d', element_ids=['f'], start=27, end=27+len(text), precision='parsed_text'))
    document = Document(document_id='d', source_hash='hash', reader_name='test', elements=[element])
    parent = chunk_document(document, 700)[1]
    children = split_chunk(document, parent)
    assert len(children) == 2 and ''.join(c.text for c in children) == parent.text
    assert children[0].owned_refs[0].start == parent.owned_refs[0].start
    assert children[0].owned_refs[0].end == children[1].owned_refs[0].start
    assert children[1].owned_refs[0].end == parent.owned_refs[0].end
    assert all(c.continuation_of == parent.chunk_id for c in children)
    assert [c.chunk_id for c in split_chunk(document, parent)] == [c.chunk_id for c in children]
    grandchildren = split_chunk(document, children[1])
    assert ''.join(c.text for c in grandchildren) == children[1].text
    assert grandchildren[0].owned_refs[0].start == children[1].owned_refs[0].start


def test_adaptive_split_packed_elements_does_not_expand_ranges():
    from bid_outline.document import split_chunk
    elements = [DocumentElement(element_id=f'f{i}', kind='paragraph', order=i, text=f'第{i}段条件' * 20,
        source_ref=SourceRef(document_id='d', element_ids=[f'f{i}'], start=0, end=100, precision='parsed_text')) for i in range(4)]
    document = Document(document_id='d', source_hash='hash', reader_name='test', elements=elements)
    parent = chunk_document(document, 1000)[0]
    children = split_chunk(document, parent)
    assert len(children) == 2
    assert [r.model_dump() for c in children for r in c.owned_refs] == [r.model_dump() for r in parent.owned_refs]
    assert all(c.text in parent.text for c in children)


def test_adaptive_split_table_only_owns_selected_complete_rows():
    from bid_outline.document import split_chunk
    from bid_outline.document.readers import table_rows
    cells = [{'id': 'h', 'row': 0, 'col': 0, 'paragraphs': ['表头'], 'column_header': True},
             {'id': 'p', 'row': 1, 'col': 0, 'paragraphs': ['共同适用条件'], 'row_span': 4}]
    cells += [{'id': f'c{i}', 'row': i, 'col': 1, 'paragraphs': ['具体方案要求' * 25]} for i in range(1, 5)]
    table = {'cells': cells}
    element = DocumentElement(element_id='f', kind='table', order=0, text='派生文字' * 300,
        source_ref=SourceRef(document_id='d', element_ids=['f'], table_id='t', cell_ids=[c['id'] for c in cells], precision='table'),
        metadata={'table_rows': table_rows(table), 'source_table': table})
    document = Document(document_id='d', source_hash='hash', reader_name='test', elements=[element])
    parent = chunk_document(document, 3000)[0]
    children = split_chunk(document, parent)
    assert len(children) == 2
    ids = [cid for c in children for r in c.owned_refs for cid in r.cell_ids]
    assert sorted(ids) == sorted(parent.owned_refs[0].cell_ids) and len(ids) == len(set(ids))
    assert all(r.start is None and r.end is None for c in children for r in c.owned_refs)
    assert '表头' in children[1].text and '共同适用条件' in children[1].text
    grandchildren = split_chunk(document, children[1])
    assert {cid for c in grandchildren for r in c.owned_refs for cid in r.cell_ids} == set(children[1].owned_refs[0].cell_ids)
    leaf = grandchildren[0]
    assert split_chunk(document, leaf) == []


def test_reader_manifest_gates_stale_artifacts_and_hashes_input_evidence():
    path = directory()
    data = {'version': 2, 'corrected': {'sections': [], 'fragments': []},
            'blocks': [{'id': 'omitted', 'kind': 'paragraph', 'text': '不可静默丢失的正文'}]}
    (path/'document.json').write_text(json.dumps(data), encoding='utf-8')
    without_manifest = read_document(path)
    assert without_manifest.metadata['reader_manifest_present'] is False
    assert {d['kind'] for d in without_manifest.diagnostics} >= {'missing_reader_validation', 'unrepresented_reader_block'}
    for status in ('running', 'failed', 'cancelled', 'unknown'):
        (path/'manifest.json').write_text(json.dumps({'status': status, 'input_sha256': 'source1'}))
        with pytest.raises(ValueError, match='manifest status'):
            read_document(path)
    (path/'validation.json').write_text(json.dumps({'status': 'incomplete', 'issues': []}))
    (path/'manifest.json').write_text(json.dumps({'status': 'incomplete', 'input_sha256': 'source1'}))
    adopted = read_document(path)
    assert adopted.metadata['input_sha256'] == 'source1'
    assert adopted.metadata['reader_status'] == 'incomplete'
    assert adopted.source_hash != without_manifest.source_hash
    (path/'manifest.json').write_text(json.dumps({'status': 'incomplete', 'input_sha256': 'source2'}))
    assert read_document(path).source_hash != adopted.source_hash


def test_outline_depth_boundary_is_enforced():
    nodes = [OutlineNode(node_id=f'n{i}', title=f'层级{i}', parent_id=f'n{i-1}' if i else None) for i in range(5)]
    validate_outline(nodes[:4])
    with pytest.raises(ValueError, match='depth'):
        validate_outline(nodes)


def test_failed_serialization_does_not_replace_committed_audit_state():
    path = directory()
    repo = RunRepository(path)
    repo.save_run({'run_id': 'r', 'status': 'running'})
    repo.record_attempt('call', 't', {'request': {'messages': ['完整请求']}})
    with pytest.raises(ValueError):
        repo.save_run({'run_id': 'r', 'value': float('nan')})
    with pytest.raises(ValueError):
        repo.record_attempt('call', 't', {'response': float('nan')})
    assert repo.get_run() == {'run_id': 'r', 'status': 'running'}
    attempt = repo.list_attempts()[0]
    assert attempt['request']['messages'] == ['完整请求']
    assert 'response' not in attempt
    repo.upsert_task('t', 'P01', 'fingerprint', 'running')
    with pytest.raises(FileNotFoundError):
        repo.commit_task('t', 'a-' + '0' * 64)
    assert repo.get_task('t')['status'] == 'running'
    assert 'accepted_artifact_id' not in repo.get_task('t')
    repo.close()
