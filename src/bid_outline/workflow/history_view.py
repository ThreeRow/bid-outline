"""Project model-authored historical prose with its original reference context.

Persisted domain objects and free text are never rewritten. Mapping rows carry
partial identities only and cannot grant visibility to an unavailable object.
"""
import json
import re

from bid_outline.model.references import Presented, visible_groups
from .common import dump, present

IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:/-]*')


class HistoryPresenter:
    def __init__(self, repository, artifacts):
        self.repository, self.artifacts = repository, artifacts
        self._sources = {}

    def source(self, artifact_id):
        if artifact_id in self._sources:
            return self._sources[artifact_id]
        result = {'task_type': '前序任务', 'aliases': {}, 'available': False}
        if re.fullmatch(r'a-[0-9a-f]{64}', artifact_id):
            envelope = self.artifacts.envelope(artifact_id)
            task = self.repository.get_task(envelope.get('task_id'))
            # Never use a newer adoption's alias namespace for an older artifact.
            if task and task.get('accepted_artifact_id') == artifact_id:
                result['task_type'] = task['task_type']
                call_id = task.get('accepted_call_id')
                if call_id:
                    path = self.repository.run_dir / 'calls' / call_id / 'id_aliases.json'
                    result['aliases'] = json.loads(path.read_text(encoding='utf-8'))['alias_to_id'] if path.exists() else {}
                    result['available'] = True
        self._sources[artifact_id] = result
        return result

    def __call__(self, **sections):
        def raw(value):
            if isinstance(value, Presented):
                return value.view
            value = dump(value)
            if isinstance(value, dict):
                return {key: raw(item) for key, item in value.items()}
            if isinstance(value, list):
                return [raw(item) for item in value]
            return value

        original = raw({'__sections__': [[name, value] for name, value in sections.items()]})
        groups = visible_groups(original)
        visible = set().union(*(set(values) for group, values in groups.items() if group != 'support_requirements'))
        identities, producers = {}, set()

        def inspect(value):
            if isinstance(value, list):
                for item in value:
                    inspect(item)
            elif isinstance(value, dict):
                for field, group, title_field in [('node_id', 'nodes', 'title'), ('requirement_id', 'requirements', 'topic'),
                                                  ('material_id', 'materials', None), ('element_id', 'elements', None),
                                                  ('suggestion_id', 'suggestions', 'topic')]:
                    ident = value.get(field)
                    if isinstance(ident, str) and ident in groups[group]:
                        entry = identities.setdefault(ident, {field: ident})
                        if title_field and title_field in value:
                            entry['对象标题'] = value[title_field]
                if ('targets' in value and 'shared_conditions' in value) or ('finding_id' in value and 'evidence' in value):
                    if value.get('producing_artifact_id'):
                        producers.add(value['producing_artifact_id'])
                for item in value.values():
                    inspect(item)
        inspect(original)
        source_labels = {aid: f'H{i:02d}' for i, aid in enumerate(sorted(producers), 1)}

        def history(artifact_id, prose):
            source = self.source(artifact_id)
            # Chinese-adjacent IDs are tokens; no token replacement ever occurs.
            tokens = set(IDENTIFIER.findall('\n'.join(prose)))
            aliases = source['aliases']
            rows = []
            for old in sorted(tokens & (set(aliases) | visible)):
                target = aliases.get(old, old)
                if target in visible:
                    rows.append({'原说明编号': old, **identities[target]})
            unavailable = sorted(old for old in tokens & set(aliases) if aliases[old] not in visible)
            return {
                '历史来源': source['task_type'] + '/' + source_labels[artifact_id],
                '编号语境': '以下原说明来自独立的历史调用，其中编号按原说明编号对照解释；历史编号不等于本次同名编号。'
                           '对照仅解释引用用法，不改变原文中的同名型号或文字。',
                '原说明编号对照': rows,
                '仅历史来源可用而本次未提供对象的编号': unavailable,
                '来源编号语境状态': '已从采用调用恢复' if source['available'] else '来源映射不可用，不能把其中局部编号认作本次编号',
            }

        def project(value):
            if isinstance(value, list):
                return [project(item) for item in value]
            if not isinstance(value, dict):
                return value
            aid = value.get('producing_artifact_id')
            if aid and 'targets' in value and 'shared_conditions' in value:
                scopes = list(dict.fromkeys(target.get('scope', '') for target in value['targets']))
                history_fields = [*scopes, value.get('basis', ''), value.get('unassigned_content', '')]
                context = history(aid, history_fields)
                context['承接范围原说明'] = [{'说明编号': f'S{i:02d}', '来源说明': text} for i, text in enumerate(scopes, 1)]
                context['归属依据原说明'] = value.get('basis', '')
                context['未承接内容原说明'] = value.get('unassigned_content', '')
                targets = []
                for target in value['targets']:
                    projected_target = dict(target)
                    if target['node_id'] not in visible:
                        projected_target.pop('node_id')
                        projected_target['本次范围'] = '该承接目标未在本次提供，仅保留历史分工'
                    projected_target['scope'] = f'见历史归属说明/{source_labels[aid]}/S{scopes.index(target.get("scope", ""))+1:02d}'
                    targets.append(projected_target)
                return {**{key: project(item) for key, item in value.items() if key not in {'targets', 'basis', 'unassigned_content'}},
                        'targets': targets, '历史归属说明': context}
            if aid and 'finding_id' in value and 'evidence' in value:
                context = history(aid, [value['evidence']])
                context['来源说明'] = value['evidence']
                return {**{key: project(item) for key, item in value.items() if key != 'evidence'}, '历史检查说明': context}
            return {key: project(item) for key, item in value.items()}

        projected = project(original)
        return present(**dict(projected['__sections__']))
