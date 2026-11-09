"""Allow large table cells to be processed as qualified expanded-text slices."""


def expandable_tables(document, text_tokens, limit):
    elements = []
    for element in document.elements:
        rows = element.metadata.get('table_rows', [])
        if (element.kind == 'table' and text_tokens(element.text) > limit
                and (not rows or any(text_tokens(row['text']) > limit for row in rows))):
            source = element.source_ref.model_copy(deep=True)
            source.reader_ref.update(text_slice_basis='expanded_table_text',
                                     cell_ids_scope='whole_candidate_table_context')
            element = element.model_copy(update={
                'kind': 'expanded_table_text', 'source_ref': source,
                'metadata': {**element.metadata, 'original_kind': 'table'}})
        elements.append(element)
    return document.model_copy(update={'elements': elements})
