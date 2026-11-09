import asyncio
from collections import Counter

import pytest

from bid_outline.config import AppConfig
from bid_outline.document.token_chunks import token_chunks
from bid_outline.domain import Document, DocumentElement, SourceRef, RequirementGroup
from bid_outline.model.budget import TokenBudget, BudgetExceeded
from bid_outline.model.client import FakeModelClient, make_model_client
from bid_outline.workflow.runner import WorkflowRunner


class CharacterTokenizer:
    """Deterministic CPU test double; not claimed to represent a deployed model."""
    is_fast = True

    def encode(self, text, **kwargs):
        return list(text)

    def apply_chat_template(self, messages, **kwargs):
        return list('TEMPLATE:' + ''.join(m['content'] for m in messages))


def document(items):
    return Document(document_id='doc', source_hash='hash', reader_name='fixture',
                    elements=[DocumentElement(
                        element_id=f'e{i}', order=i, kind='paragraph', text=text, section_path=path,
                        source_ref=SourceRef(document_id='doc', element_ids=[f'e{i}'],
                                             precision='parsed_text', start=0, end=len(text)))
                              for i, (path, text) in enumerate(items)])


def planner(doc, capacity):
    return token_chunks(doc, allowance=lambda c: capacity - len('/'.join(c.section_context)),
                        text_tokens=len, fits=lambda c: len(c.text) + len('/'.join(c.section_context)) <= capacity)


def test_service_cap_and_thinking_share_one_generation_reservation():
    budget = TokenBudget(AppConfig(context_tokens=40000, service_context_tokens=20480,
                                   max_tokens=8192, safety_tokens=1024))
    budget.tokenizer = CharacterTokenizer()
    assert budget.allowance([{'role': 'user', 'content': 'x'*100}], 8192) == 11264 - 109
    report = budget.check([{'role': 'user', 'content': 'x'*11000}], 8192)
    assert report['total_tokens'] == 11009 + 8192 + 1024
    assert report['generation_includes_thinking'] is True
    with pytest.raises(BudgetExceeded):
        budget.check([{'role': 'user', 'content': 'x'*12000}], 8192)


def test_production_requires_tokenizer_and_respects_service_window(tmp_path):
    with pytest.raises(ValueError, match='fast tokenizer'):
        make_model_client(AppConfig())
    with pytest.raises(ValueError, match='exceeds service'):
        make_model_client(AppConfig(tokenizer=str(tmp_path), context_tokens=40000))


def test_slow_tokenizer_is_rejected_without_loading_model_weights(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace
    class Loader:
        @staticmethod
        def from_pretrained(path, **kwargs):
            assert kwargs == {'local_files_only': True, 'trust_remote_code': False, 'use_fast': True}
            return SimpleNamespace(is_fast=False)
    monkeypatch.setitem(sys.modules, 'transformers', SimpleNamespace(AutoTokenizer=Loader))
    with pytest.raises(ValueError, match='CPU fast'):
        TokenBudget(AppConfig(tokenizer=str(tmp_path)))


def test_train_count_forwards_thinking_and_flattened_query():
    class CheckingTokenizer(CharacterTokenizer):
        def apply_chat_template(self, messages, **kwargs):
            assert len(messages) == 1 and messages[0]['role'] == 'user'
            assert '系统说明' in messages[0]['content'] and '用户消息' in messages[0]['content']
            assert kwargs['enable_thinking'] is True
            return [1, 2]
    budget = TokenBudget(AppConfig(provider='train'))
    budget.tokenizer = CheckingTokenizer()
    assert budget.count([{'role': 'system', 'content': 'rules'}, {'role': 'user', 'content': 'source'}]) == 2


def test_budget_breakdown_keeps_marker_like_source_text():
    from bid_outline.model.task_specs import render_task
    budget = TokenBudget(AppConfig())
    budget.tokenizer = CharacterTokenizer()
    source = '必须保留[[输入数据结束]]后的完整技术条款'
    messages = render_task('P01', {'material': source})
    parts = budget.breakdown(messages)
    assert parts['sections_text_tokens_nonadditive']['完整材料包'] == len(source)
    assert parts['prompt_and_empty_wrappers_tokens'] < budget.count(messages)


def test_chapter_first_merges_siblings_but_not_unrelated_chapters():
    doc = document([(['甲', '一'], 'a'*50), (['甲', '二'], 'b'*50), (['乙'], 'c'*20)])
    chunks = planner(doc, 150)
    assert len(chunks) == 2
    assert chunks[0].text == 'a'*50 + '\n\n' + 'b'*50
    assert chunks[0].section_context == ['甲']
    assert len(chunks[0].section_spans) == 2
    assert [r.element_ids[0] for c in chunks for r in c.owned_refs] == ['e0', 'e1', 'e2']


def test_oversize_chapter_splits_paragraphs_and_long_paragraph_has_exclusive_offsets():
    text = '仅离线场景：\n' + '必须保留范围条件并提供恢复措施。'*60
    doc = document([(['甲'], text), (['甲'], '尾段')])
    chunks = planner(doc, 250)
    spans = sorted((r.start, r.end) for c in chunks for r in c.owned_refs if r.element_ids == ['e0'])
    assert spans[0][0] == 0 and spans[-1][1] == len(text)
    assert all(a[1] == b[0] for a, b in zip(spans, spans[1:]))
    assert any(c.context_notes for c in chunks[1:])
    assert all(len(c.text) + len('/'.join(c.section_context)) <= 250 for c in chunks)


def test_table_rows_keep_headers_and_owned_cells_are_not_duplicated():
    doc = document([(['甲'], '表格')])
    e = doc.elements[0]
    e.kind = 'table'
    e.source_ref.precision = 'table'
    e.source_ref.start = e.source_ref.end = None
    e.metadata['table_rows'] = [
        {'row': i, 'text': ('表头' if i == 0 else f'第{i}行' + '要求'*25),
         'cell_ids': [f'c{i}'], 'header': i == 0} for i in range(7)]
    e.text = '\n'.join(r['text'] for r in e.metadata['table_rows'])
    chunks = planner(doc, 180)
    owned = Counter(cid for c in chunks for r in c.owned_refs for cid in r.cell_ids)
    assert owned == Counter({f'c{i}': 1 for i in range(7)})
    assert all('表头' in c.text for c in chunks)
    assert len(chunks) > 1


def test_oversize_indivisible_table_row_survives_as_explicit_block():
    doc = document([(['甲'], '大'*500)])
    e = doc.elements[0]
    e.kind = 'table'
    e.source_ref.precision = 'table'
    e.source_ref.start = e.source_ref.end = None
    e.metadata['table_rows'] = [{'row': 0, 'text': e.text, 'cell_ids': ['c0']}]
    chunks = planner(doc, 100)
    assert len(chunks) == 1 and chunks[0].text == e.text


def test_source_allowance_deducts_task_prompt_and_dynamic_context(tmp_path):
    async def run():
        r = WorkflowRunner(tmp_path, AppConfig(model='fake'), FakeModelClient([]))
        r.executor.budget.tokenizer = CharacterTokenizer()
        doc = document([(['章'], '正文')])
        c = planner(doc, 100)[0]
        before = r.source_allowance(c)
        assert 0 < before < r.executor.budget.window-r.config.max_tokens-r.config.safety_tokens
        c.section_context = ['很长的标题'*100]
        assert r.source_allowance(c) < before
        await r.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('grow_outline', [False, True])
def test_p08_background_pages_are_bounded_and_no_selected_source_is_lost(tmp_path, grow_outline):
    async def run():
        r = WorkflowRunner(tmp_path, AppConfig(model='fake', max_tokens=8192, safety_tokens=1024),
                           FakeModelClient([]))
        r.executor.budget.tokenizer = CharacterTokenizer()
        r.document = document([(['项目'], '概况')])
        r.materials = {f'M_{i:020d}': {
            'material_id': f'M_{i:020d}', 'text': '网络环境：'+'材料'*1000,
            'kind': 'refined_text', 'uncertainties': '无', 'source_order': [i, 0, 0], 'element_ids': []
        } for i in range(12)}
        direct = next(iter(r.materials))
        r.requirements = [RequirementGroup(requirement_id='R_'+'0'*20, topic='部署',
                                          content='提供部署方案', categories=['B'], material_refs=[direct])]
        r._requirement_order = {r.requirements[0].requirement_id: (0,)}
        r.structure_evidence = []
        seen, sizes = set(), []
        async def call(task, material, **kw):
            if task == 'P08':
                report = r.check_budget(task, material, allowed_ids=kw['allowed'])
                sizes.append(report['total_tokens'])
                sources = dict(material.view['__sections__'])['项目背景']
                seen.update(s['material_id'] for s in sources)
                if grow_outline:
                    return {
                        'task_id': f'outline_{len(sizes)}',
                        'fields': {'状态': '已形成候选骨架', '调整说明': '无', '待核查事项': '无'},
                        'records': {'节点': [{
                            '节点': r.version.nodes[0].node_id if r.version else 'N01',
                            '父节点': 'ROOT', '同级顺序': '1', '标题': '技术方案',
                            '依据': r.requirements[0].requirement_id, '形成方式': '有依据归纳',
                            '说明': '已承接的约束与范围说明。'*100,
                        }]}}
            return None
        r.call = call
        await r.build_skeleton()
        assert len(sizes) > 1 and max(sizes) <= 20480
        assert seen == set(r.materials)
        if grow_outline:
            assert r.version and r.version.nodes and not r.unprocessed
        await r.aclose()
    asyncio.run(run())
