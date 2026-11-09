"""Mechanical mutation, budget recovery and CLI boundaries."""

import asyncio
import json
from pathlib import Path
import tempfile

import pytest

from bid_outline.cli import main, read_state, run_lock
from bid_outline.config import AppConfig
from bid_outline.domain import Document, DocumentElement, OutlineConstraint, OutlineNode, SourceRef
from bid_outline.export import export_result
from bid_outline.model.client import FakeModelClient, ModelResponse
from bid_outline.storage import ArtifactStore
from bid_outline.workflow.runner import WorkflowRunner
from bid_outline.workflow.common import bounded_map
from bid_outline.workflow.outline import make_version, replace_branch, retain_skeleton_nodes


NEGATIVE = "[[结果开始]]\n判断：未发现\n相关主题：无\n疑点：无\n引用线索：无\n[[结果结束]]"


def folder():
    return Path(tempfile.mkdtemp(prefix="bid-boundaries-"))


def test_branch_replacement_preserves_other_chapters_and_source_constraints():
    nodes = [OutlineNode(node_id="A", title="实施", order=1),
             OutlineNode(node_id="B", title="运维", order=2),
             OutlineNode(node_id="C", title="部署", parent_id="A")]
    constraint = OutlineConstraint(constraint_id="fixed", affected_node_ids=["A", "B"],
                                   kind="preserve", origin="系统策略")
    base = make_version(nodes, None, "test", [constraint])
    replacement = [OutlineNode(node_id="D", title="迁移", parent_id="A")]
    result = replace_branch(base, "A", replacement)
    assert {n.node_id for n in result} == {"A", "B", "D"}
    assert next(n for n in result if n.node_id == "B") == nodes[1]
    with pytest.raises(ValueError, match="outside"):
        replace_branch(base, "A", [nodes[1]])
    with pytest.raises(ValueError, match="boundary"):
        replace_branch(base, "A", [OutlineNode(node_id="D", title="错误归属", parent_id="B")])


def test_generation_cannot_drop_previously_assigned_skeleton_nodes():
    nodes = [OutlineNode(node_id='root', title='响应方案'),
             OutlineNode(node_id='project', title='项目方案', parent_id='root'),
             OutlineNode(node_id='deployment', title='平台部署方案及实施计划', parent_id='project')]
    base = make_version(nodes, None, 'skeleton')
    combined = retain_skeleton_nodes(base, 'root', [nodes[1]])
    assert {node.node_id for node in combined} == {'project', 'deployment'}
    # A new sibling conflicting with the preserved node's order is not silently fixed.
    conflict = OutlineNode(node_id='new', title='另一个方案', parent_id='project')
    with pytest.raises(ValueError):
        retain_skeleton_nodes(base, 'root', [nodes[1], conflict])


def test_preserved_title_whitespace_keeps_original_name_with_audit():
    from bid_outline.domain import RequirementGroup
    root = folder()
    runner = WorkflowRunner(root, AppConfig(model='fake'), FakeModelClient([]))
    runner.requirements = [RequirementGroup(requirement_id='r', topic='AI', content='要求')]
    original = OutlineNode(node_id='n', title='AI合规方案', basis_requirement_ids=['r'])
    constraint = OutlineConstraint(constraint_id='c', affected_node_ids=['n'], kind='preserve', origin='系统策略')
    runner.commit([original], 'initial', [constraint])
    runner.commit([original.model_copy(update={'title': 'AI 合规方案'})], 'whitespace')
    assert runner.version.nodes[0].title == 'AI合规方案'
    assert any(json.loads(p.read_text()).get('kind') == 'preserved_title_whitespace'
               for p in (root / 'artifacts').glob('a-*.json'))
    with pytest.raises(ValueError, match='preserved node changed'):
        runner.commit([original.model_copy(update={'title': 'AI安全方案'})], 'semantic_change')
    asyncio.run(runner.aclose())


def test_truncated_screen_reprocesses_disjoint_children_and_cli_export(capsys):
    async def scenario():
        directory = folder()
        text = "普通说明。" * 50
        document = Document(document_id="doc", source_hash="hash", reader_name="test", elements=[
            DocumentElement(element_id="e", kind="paragraph", order=0, text=text,
                            source_ref=SourceRef(document_id="doc", element_ids=["e"],
                                                 start=0, end=len(text), precision="parsed_text"))])
        client = FakeModelClient({"P01": [ModelResponse("partial", finish_reason="length"), NEGATIVE, NEGATIVE]})
        runner = WorkflowRunner(directory, AppConfig(model="fake"), client)
        result = await runner.run(document)
        assert result["unprocessed_ranges"] == []
        assert len(client.requests) == 3
        assert len(runner._leaf_chunks) == 2
        refs = [c.owned_refs[0] for c in runner._leaf_chunks.values()]
        assert sorted((r.start, r.end) for r in refs)[0][0] == 0
        assert sorted((r.start, r.end) for r in refs)[-1][1] == len(text)
        await runner.aclose()
        manifest, tasks, _ = read_state(directory)
        assert manifest["status"] == "completed_with_issues"
        assert any(t["status"] == "failed" for t in tasks)  # rejected parent is historical
        assert main(["status", "--run", str(directory)]) == 2
        assert main(["export", "--run", str(directory)]) == 0
        assert (directory / "exports/backbone.md").is_file()
        return directory
    directory = asyncio.run(scenario())
    assert "completed_with_issues" in capsys.readouterr().out
    with run_lock(directory):
        with pytest.raises(ValueError, match="owns"):
            with run_lock(directory):
                pass


def test_backpressure_and_fatal_worker_cancel_producer():
    async def scenario():
        async def double(item):
            await asyncio.sleep(0)
            return item * 2
        assert await bounded_map(double, list(range(100)), workers=3, capacity=1) == list(range(0, 200, 2))
        async def fail(item):
            if item == 2:
                raise OSError("audit disk failed")
            await asyncio.sleep(0)
            return item
        with pytest.raises(ExceptionGroup):
            await asyncio.wait_for(bounded_map(fail, list(range(100)), workers=2, capacity=1), timeout=2)
    asyncio.run(scenario())


def test_reader_missing_scope_never_reports_complete():
    async def scenario():
        document = Document(document_id="d", source_hash="hash", reader_name="reading", diagnostics=[
            {"kind": "unrepresented_reader_block", "block_id": "missing", "has_text": True}])
        runner = WorkflowRunner(folder(), AppConfig(model="fake"), FakeModelClient([]))
        try:
            result = await runner.run(document)
            assert result["status"] == "incomplete"
            assert result["unprocessed_ranges"][0]["scope"] == "upstream_reader"
        finally:
            await runner.aclose()
    asyncio.run(scenario())


def test_corrupt_adopted_document_is_fatal_on_resume():
    async def scenario():
        directory = folder()
        runner = WorkflowRunner(directory, AppConfig(model="fake"), FakeModelClient([]))
        await runner.run(Document(document_id="d", source_hash="hash", reader_name="test"))
        aid = runner.repository.get_run()["document_artifact_id"]
        await runner.aclose()
        (directory / "artifacts" / (aid + ".json")).write_text("{}", encoding="utf-8")
        resumed = WorkflowRunner(directory, AppConfig(model="fake"), FakeModelClient([]))
        try:
            with pytest.raises(ValueError, match="hash"):
                await resumed.run(resume=True)
        finally:
            await resumed.aclose()
    asyncio.run(scenario())
