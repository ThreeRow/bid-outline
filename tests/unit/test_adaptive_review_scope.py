import asyncio
from pathlib import Path
import tempfile

from bid_outline.config import AppConfig
from bid_outline.domain import OutlineNode, OutlineVersion, RequirementGroup, RequirementAssignment, ReviewFinding
from bid_outline.model.client import FakeModelClient
from bid_outline.model.executor import BudgetExceeded
from bid_outline.workflow.runner import WorkflowRunner


def test_oversize_repair_descends_into_disjoint_subbranches_with_all_requirements():
    async def run():
        runner = WorkflowRunner(Path(tempfile.mkdtemp(prefix='bid-repair-scope-')), AppConfig(model='fake'), FakeModelClient([]))
        runner.version = OutlineVersion(version_id='v', nodes=[
            OutlineNode(node_id='root', title='root'),
            OutlineNode(node_id='a', title='服务方案', parent_id='root'),
            OutlineNode(node_id='b', title='应急方案', parent_id='root', order=2)])
        runner.requirements = [RequirementGroup(requirement_id='ra', topic='服务', content='服务完整要求及共同条件'),
                               RequirementGroup(requirement_id='rb', topic='应急', content='应急完整要求及共同条件')]
        runner.assignments = [RequirementAssignment(requirement_id='r'+key, outline_version_id='v', status='已分配',
                                                     targets=[{'node_id': key, 'scope': '全部'}]) for key in ('a', 'b')]
        async def review(*args):
            return [ReviewFinding(finding_id='finding', kind='coverage', node_ids=['a','b'], requirement_ids=['ra','rb'])]
        runner.review = review
        def budget(task, material, **kwargs):
            if '"title": "root"' in material:
                raise BudgetExceeded('whole chapter too large')
        runner.check_budget = budget
        calls = []
        async def call(task, material, **kwargs):
            calls.append((kwargs['scope'], material))
            return {'fields': {'处理结论': '无法在范围内修正'}, 'records': {}}
        runner.call = call
        try:
            await runner.review_and_repair()
            assert [scope for scope, _ in calls] == ['a', 'b']
            assert '服务完整要求及共同条件' in calls[0][1]
            assert '应急完整要求及共同条件' in calls[1][1]
            assert runner.unprocessed == []
            assert runner.findings[0].status == 'open'  # no invented success
        finally:
            await runner.aclose()
    asyncio.run(run())


def test_generation_splits_using_complete_request_budget_not_only_character_count():
    async def run():
        runner = WorkflowRunner(Path(tempfile.mkdtemp(prefix='bid-generation-budget-')), AppConfig(model='fake'), FakeModelClient([]))
        runner.version = OutlineVersion(version_id='v', nodes=[OutlineNode(node_id='root', title='实施')])
        runner.requirements = [RequirementGroup(requirement_id='a', topic='A', content='甲要求及全部条件'),
                               RequirementGroup(requirement_id='b', topic='B', content='乙要求及全部条件')]
        runner.assignments = [RequirementAssignment(requirement_id=key, outline_version_id='v', status='已分配',
                                                     targets=[{'node_id': 'root', 'scope': '全部'}]) for key in ('a', 'b')]
        def budget(task, material, **kwargs):
            if '甲要求及全部条件' in material and '乙要求及全部条件' in material:
                raise BudgetExceeded('combined sources/assignments exceed budget')
        runner.check_budget = budget
        calls = []
        async def call(task, material, **kwargs):
            calls.append(material)
            return {'fields': {'章节': 'root', '处理结果': '无须新增子目录', '未承接事项': '无', '结构调整建议': '无'},
                    'records': {}, 'task_id': str(len(calls))}
        runner.call = call
        try:
            await runner.generate()
            assert len(calls) == 2
            assert '甲要求及全部条件' in calls[0] and '乙要求及全部条件' not in calls[0]
            assert '乙要求及全部条件' in calls[1] and '甲要求及全部条件' not in calls[1]
            assert not runner.unprocessed
        finally:
            await runner.aclose()
    asyncio.run(run())
