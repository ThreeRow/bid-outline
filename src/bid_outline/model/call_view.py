"""Human-readable audit navigation derived from immutable protocol evidence."""
import json
import os
import re
from pathlib import Path

from bid_outline.export.markdown import write_atomic
from .task_specs import TASK_NAMES


def stage(task: str, scope: str = '') -> str:
    from .text_tasks import TASKS
    if task in TASKS:
        return TASKS[task][1]
    number = int(task[1:3])
    if number <= 2:
        return '01_screening'
    if number == 3:
        return '02_refinement'
    if number <= 5:
        return '03_extraction'
    if number <= 13:
        return '04_outline'
    if number <= 16:
        return '05_review'
    return '06_extension'


def new_call_path(calls: Path, task: str, inputs: dict, attempt: int, number: int | None = None) -> tuple[str, str]:
    parent = stage(task)
    # Auxiliary calls carry the triggering stage when supplied by the workflow.
    parent = inputs.get('trigger_stage', parent) if task in ('P06', 'P19') else parent
    folder = calls / parent
    folder.mkdir(parents=True, exist_ok=True)
    label = inputs.get('display_name') or TASK_NAMES[task]
    label = re.sub(r'[^\w\u4e00-\u9fff.-]+', '_', label).strip('._')[:48]
    number = number or len(list(folder.iterdir())) + 1
    while True:
        relative = f'{parent}/{number:04d}_{task}_{label}_attempt{attempt:02d}'
        try:
            (calls / relative).mkdir()
            return relative, label
        except FileExistsError:
            number += 1


def publish(run_dir: Path, repository, call_id: str) -> None:
    """Render current adopted status and links without modifying original evidence."""
    calls = run_dir / 'calls'
    attempts = repository.list_attempts()
    tasks = {task['task_id']: task for task in repository.list_tasks()}
    current_task = next((a['task_id'] for a in attempts if a['call_id'] == call_id), None)
    rows = ['# Model calls', '', '| 调用 | 对象 | 结果 | 耗时(s) | 采用 | 来源 |',
            '|---|---|---|---:|---|---|']
    for attempt in attempts:
        attempt = {**attempt}
        task = tasks.get(attempt['task_id'], {})
        attempt.setdefault('task_type', task.get('task_type', 'unknown'))
        attempt.setdefault('attempt_no', '?')
        path = calls / attempt['call_id']
        adopted = task.get('accepted_call_id') == attempt['call_id']
        state = 'failed' if attempt.get('error') else attempt.get('validation_status', 'pending')
        origin = 'reused' if attempt.get('reused_from') else 'model'
        label = attempt.get('display_name', attempt['task_type']).replace('|', '\\|').replace('\n', ' ')
        rows.append(f"| [{attempt['task_type']} #{attempt['attempt_no']}](calls/{attempt['call_id']}/summary.md) | {label} | {state} | {attempt.get('request_seconds', 0):.2f} | {'yes' if adopted else 'no'} | {origin} |")
        if call_id is not None and attempt['task_id'] != current_task and (path / 'summary.md').exists():
            continue
        links = '\n'.join(f'- [{name}]({name})' for name in (
            'input.md', 'output.txt', 'request.http.json', 'response.raw.json', 'parsed.json',
            'validation.json', 'id_aliases.json', 'format_repairs.json', 'meta.json') if (path / name).exists())
        related = '\n'.join(f"- [attempt {other['attempt_no']}]({os.path.relpath(calls / other['call_id'] / 'summary.md', path)})"
                            for other in attempts if other['task_id'] == attempt['task_id'] and other['call_id'] != attempt['call_id'])
        body = f"# {attempt['task_type']} {TASK_NAMES.get(attempt['task_type'], '')}\n\n对象：{label}\n\n状态：{state}；采用：{adopted}；来源：{origin}\n\n"
        body += f"任务：`{attempt['task_id']}`\n\n原因：{attempt.get('reason')}\n\n"
        body += f"耗时：{attempt.get('request_seconds', 0):.2f}s；结束原因：{attempt.get('finish_reason')}\n\n"
        if (path / 'parsed.json').exists():
            parsed = json.loads((path / 'parsed.json').read_text(encoding='utf-8'))
            fields = parsed.get('fields', {})
            if 'text' in parsed:
                body += f"文本结果：{len(parsed['text'])}字符；语义质量未评估。\n\n"
            decisions = {key: value for key, value in fields.items()
                         if key in ('判断', '处理结果', '提取结果', '状态', '结论', '检查结论', '处理结论', '结果')}
            counts = {key: len(value) for key, value in parsed.get('records', {}).items()}
            body += '结果摘要：' + json.dumps({'decisions': decisions, 'records': counts,
                                              'supplements': len(parsed.get('supplements', []))}, ensure_ascii=False) + '\n\n'
        if task.get('accepted_artifact_id'):
            artifact = run_dir / 'artifacts' / (task['accepted_artifact_id'] + '.json')
            body += f"采用产物：[artifact]({os.path.relpath(artifact, path)})\n\n"
        if task.get('dependencies'):
            body += '输入产物：\n' + '\n'.join(
                f"- [{key}]({os.path.relpath(run_dir / 'artifacts' / (key + '.json'), path)})"
                for key in task['dependencies']) + '\n\n'
        if attempt.get('error'):
            body += f"校验/执行错误：{attempt['error']}\n\n"
        if attempt.get('reused_from'):
            body += '复用证据：\n```json\n' + json.dumps(attempt['reused_from'], ensure_ascii=False, indent=2) + '\n```\n\n'
        body += links + '\n\n同任务其他尝试：\n' + (related or '无') + '\n'
        write_atomic(path / 'summary.md', body)
    write_atomic(run_dir / 'calls.md', '\n'.join(rows) + '\n')
