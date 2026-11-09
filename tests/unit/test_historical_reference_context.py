"""A previous call's NODE04 must never masquerade as the current NODE04."""
import json

from bid_outline.domain import OutlineNode, RequirementGroup, RequirementAssignment, ReviewFinding
from bid_outline.model.references import prepare_inputs, map_input, render_readable_view
from bid_outline.model.task_specs import render_task
from bid_outline.storage import ArtifactStore, RunRepository
from bid_outline.workflow.common import present
from bid_outline.workflow.history_view import HistoryPresenter


def source(tmp_path, aliases, task='P09'):
    repo, store = RunRepository(tmp_path), ArtifactStore(tmp_path)
    aid = store.put('model_result', {'fields': {}, 'records': {}}, task_id='source-task')
    repo.upsert_task('source-task', task, 'source-fingerprint', 'succeeded',
                     accepted_artifact_id=aid, accepted_call_id='source-call')
    if aliases is not None:
        directory = tmp_path / 'calls' / 'source-call'
        directory.mkdir(parents=True)
        (directory / 'id_aliases.json').write_text(json.dumps({'alias_to_id': aliases}))
    return repo, store, aid


def objects(aid):
    deploy, maintain = 'N_deployment_global', 'N_maintenance_global'
    nodes = [OutlineNode(node_id=deploy, title='部署方案', basis_requirement_ids=['R_global']),
             OutlineNode(node_id=maintain, title='运维方案', order=2, basis_requirement_ids=['R_global'])]
    requirement = RequirementGroup(requirement_id='R_global', topic='技术清单', content='部署、运维；设备型号MAT01不改')
    scope = 'NODE06负责部署方案；NODE04负责运维方案；仅离线场景适用。'
    assignment = RequirementAssignment(requirement_id='R_global', outline_version_id='V1', status='已分配',
        producing_artifact_id=aid, targets=[{'node_id': n.node_id, 'scope': scope} for n in nodes],
        shared_conditions=requirement.content, unassigned_content='无', basis='REQ06分到NODE06和NODE04。')
    return nodes, requirement, assignment, scope


def test_previous_node_aliases_have_explicit_current_mapping_without_prose_rewrite(tmp_path):
    aliases = {'NODE06': 'N_deployment_global', 'NODE04': 'N_maintenance_global', 'REQ06': 'R_global', 'MAT01': 'M_global'}
    repo, store, aid = source(tmp_path, aliases)
    try:
        nodes, req, assignment, prose = objects(aid)
        before = assignment.model_dump(mode='json')
        values = dict(本章=nodes[0], 结构=nodes, 要求=[req], 归属=[assignment],
                      原文=[{'material_id': 'M_global', 'text': '设备型号MAT01，原文NODE04不转换。'}])
        original = prepare_inputs({'material': present(**values)})
        shown = HistoryPresenter(repo, store)(**values)
        inputs = prepare_inputs({'material': shown})
        assert inputs['id_groups'] == original['id_groups']
        view = dict(shown.view['__sections__'])['归属'][0]
        assert 'basis' not in view
        assert all(t['scope'].endswith('/S01') for t in view['targets'])
        history = view['历史归属说明']
        assert len(history['承接范围原说明']) == 1
        assert history['承接范围原说明'][0]['来源说明'] == prose
        assert history['历史来源'] == 'P09/H01'
        mapping = {'N_deployment_global': 'NODE04', 'N_maintenance_global': 'NODE06', 'R_global': 'REQ01', 'M_global': 'MAT01'}
        transformed = map_input(history, mapping)
        rows = {r['原说明编号']: r for r in transformed['原说明编号对照']}
        assert rows['NODE04']['node_id'] == 'NODE06' and rows['NODE04']['对象标题'] == '运维方案'
        assert rows['NODE06']['node_id'] == 'NODE04' and rows['NODE06']['对象标题'] == '部署方案'
        assert rows['REQ06']['requirement_id'] == 'REQ01'
        assert transformed['承接范围原说明'][0]['来源说明'] == prose
        inputs['id_aliases'] = mapping
        rendered = render_task('P11', inputs)[1]['content']
        assert '承接内容：NODE06负责' not in rendered
        assert '来源说明：' + prose in rendered
        assert '设备型号MAT01，原文NODE04不转换。' in rendered
        assert assignment.model_dump(mode='json') == before
        restored = json.loads(json.dumps(shown.view, sort_keys=True, ensure_ascii=False))
        assert render_readable_view(restored, mapping) == render_readable_view(shown.view, mapping)
        # A new presenter reloads durable source-call aliases rather than memory.
        assert HistoryPresenter(repo, store)(**values).view == shown.view
    finally:
        repo.close()


def test_history_does_not_authorize_outside_objects_or_support_only_requirements(tmp_path):
    repo, store, aid = source(tmp_path, {'NODE06': 'N_deployment_global', 'NODE04': 'N_maintenance_global', 'REQ06': 'R_missing'})
    try:
        nodes, req, assignment, prose = objects(aid)
        nodes[0].basis_requirement_ids.append('R_missing')
        values = dict(本章=nodes[0], 要求=[req], 归属=[assignment])
        original = prepare_inputs({'material': present(**values)})
        shown = HistoryPresenter(repo, store)(**values)
        inputs = prepare_inputs({'material': shown})
        assert inputs['id_groups'] == original['id_groups']
        assert 'N_maintenance_global' not in inputs['allowed_ids']
        view = dict(shown.view['__sections__'])['归属'][0]
        history = view['历史归属说明']
        assert {r['原说明编号'] for r in history['原说明编号对照']} == {'NODE06'}
        assert history['仅历史来源可用而本次未提供对象的编号'] == ['NODE04', 'REQ06']
        assert 'node_id' not in view['targets'][1]
        assert view['targets'][1]['scope'].endswith('/S01')
        assert history['承接范围原说明'][0]['来源说明'] == prose
    finally:
        repo.close()


def test_review_evidence_uses_same_source_namespace_and_native_ids_are_preserved(tmp_path):
    repo, store, aid = source(tmp_path, {'NODE04': 'N_maintenance_global', 'NODE06': 'N_deployment_global'}, task='P15')
    try:
        nodes, req, _, _ = objects(aid)
        text = 'NODE04运维和NODE06部署各有组织安排，需比较范围。'
        finding = ReviewFinding(finding_id='finding', producing_artifact_id=aid,
                                kind='cross_branch', node_ids=[n.node_id for n in nodes], evidence=text)
        original = finding.model_dump(mode='json')
        view = HistoryPresenter(repo, store)(节点=nodes, 问题=[finding]).view
        shown = dict(view['__sections__'])['问题'][0]
        assert 'evidence' not in shown
        assert shown['历史检查说明']['来源说明'] == text
        assert shown['历史检查说明']['历史来源'] == 'P15/H01'
        assert {r['原说明编号'] for r in shown['历史检查说明']['原说明编号对照']} == {'NODE04', 'NODE06'}
        assert finding.model_dump(mode='json') == original
    finally:
        repo.close()


def test_native_id_call_without_alias_file_is_labeled_historical(tmp_path):
    repo, store, aid = source(tmp_path, None)
    try:
        nodes, req, assignment, _ = objects(aid)
        assignment.targets[0]['scope'] = 'N_deployment_global负责部署，原文型号MAT01保留。'
        assignment.targets[1]['scope'] = 'N_maintenance_global负责运维，条件原样。'
        shown = dict(HistoryPresenter(repo, store)(节点=nodes, 要求=[req], 归属=[assignment]).view['__sections__'])['归属'][0]
        history = shown['历史归属说明']
        assert history['来源编号语境状态'] == '已从采用调用恢复'
        assert len(history['承接范围原说明']) == 2
        assert [t['scope'].split('/')[-1] for t in shown['targets']] == ['S01', 'S02']
        assert history['承接范围原说明'][0]['来源说明'] == assignment.targets[0]['scope']
    finally:
        repo.close()


def test_generation_path_projects_assignment_history_for_each_local_branch(tmp_path):
    import asyncio
    from bid_outline.config import AppConfig
    from bid_outline.model.client import FakeModelClient
    from bid_outline.workflow.runner import WorkflowRunner

    async def run():
        repo, store, aid = source(tmp_path, {'NODE06': 'N_deployment_global', 'NODE04': 'N_maintenance_global', 'REQ06': 'R_global'})
        repo.close()
        runner = WorkflowRunner(tmp_path, AppConfig(model='fake', context_tokens=100000,service_context_tokens=100000), FakeModelClient([]))
        nodes, req, assignment, scope = objects(aid)
        runner.requirements = [req]
        runner.commit(nodes, 'initial')
        runner.assignments = [assignment]
        captured = []
        async def call(task, material, **kwargs):
            assert task == 'P11'
            sections = dict(material.view['__sections__'])
            history = sections['归属'][0]['历史归属说明']
            rows = history['原说明编号对照']
            assert {r.get('node_id') for r in rows if 'node_id' in r} == {kwargs['scope']}
            assert history['承接范围原说明'][0]['来源说明'] == scope
            assert len(history['仅历史来源可用而本次未提供对象的编号']) == 1
            captured.append(kwargs['scope'])
            return None
        runner.call = call
        try:
            await runner.generate()
            assert set(captured) == {n.node_id for n in nodes}
        finally:
            await runner.aclose()
    asyncio.run(run())
