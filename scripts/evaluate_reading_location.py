"""Run real document-reading on 24 DOCX, then unchanged ABC location only."""
import argparse
import asyncio
from datetime import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import time
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from bid_outline.document.readers import read_document
from bid_outline.export.markdown import write_atomic

spec = importlib.util.spec_from_file_location('location_evaluation', ROOT / 'scripts/evaluate_location.py')
evaluation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluation)


def stamp():
    return datetime.now(ZoneInfo('Europe/Berlin')).isoformat(timespec='seconds')


def save(path, value):
    write_atomic(path, json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def report(output, baseline):
    batch = json.loads((output / 'batch.json').read_text())
    old = {x['sample']: x for x in json.loads((baseline / 'results.json').read_text())['results']}
    comparisons = []
    for item in batch['items']:
        if item['status'] != 'extracted':
            continue
        current = json.loads((output / item['sample'] / 'metrics.json').read_text())
        prior = old[item['sample']]
        probes = []
        for a, b in zip(prior['probes'], current['probes']):
            assert (a['category'], a['phrase'], a['expected']) == (b['category'], b['phrase'], b['expected'])
            state = ('retained' if b['selected'] else 'rule_miss') if b['source_found'] else 'reading_not_literal'
            probes.append({'category': a['category'], 'phrase': a['phrase'], 'expected': a['expected'],
                'baseline_source_found': a['source_found'], 'baseline_selected': a['selected'],
                'reading_source_found': b['source_found'], 'reading_selected': b['selected'], 'state': state})
        comparisons.append({'sample': item['sample'], 'input': item['input'], 'reading_status': item['reading_status'],
            'baseline_chars': prior['source_chars'], 'reading_chars': current['source_chars'],
            'baseline_stats': prior['stats'], 'reading_stats': current['stats'], 'probes': probes,
            'diagnostics': current['diagnostics']})
    save(output / 'comparison.json', comparisons)
    positives = [p for x in comparisons for p in x['probes'] if p['expected']=='candidate' and p['baseline_source_found']]
    rows = ['# document-reading解析后ABC规则定位结果', '',
        '本轮真实运行document-reading完整阅读：Docling＋OOXML、模型标题识别及表格展开；随后只执行当前locate()规则。未运行N01整理或N02—N05目录生成。', '',
        '## 执行结果', '',
        f"批次状态：{batch['status']}；开始：{batch['started_at']}；结束：{batch.get('finished_at', '尚未结束')}。时间为Europe/Berlin。", '',
        f"24份输入：{len(comparisons)}份完成适配和候选导出；{sum(x['status']=='failed' for x in batch['items'])}份失败。阅读completed {sum(x.get('reading_status')=='completed' for x in batch['items'])}份，incomplete {sum(x.get('reading_status')=='incomplete' for x in batch['items'])}份。incomplete保留原诊断，不当作语义完整验收。", '',
        f"阅读模块记录请求尝试合计{sum(x.get('reading_metrics',{}).get('request_attempts',0) for x in batch['items'])}次；另有一次OK连通生成探测。关键词提取阶段模型调用为0。", '',
        '## 与上一批代表性条目对照', '',
        f"上一批原文可确认的正向探针，在本轮成功样本中共有{len(positives)}条：{sum(p['state']=='retained' for p in positives)}条进入相应候选，{sum(p['state']=='rule_miss' for p in positives)}条仍在阅读文字中但被规则漏选，{sum(p['state']=='reading_not_literal' for p in positives)}条未在阅读适配文字中按原短语字面确认。最后一类可能是模型表格改写、遗漏或文字形态变化，需要回查原表，不能直接归为规则漏选或认定原文不存在。", '',
        '这些条目不是全文金标准，不能据此给出完整召回率或准确率。非零A/B/C候选也不代表确有对应方案义务。', '',
        '| 样本 | 阅读状态 | A占比（前→后） | B占比（前→后） | C占比（前→后） | 候选原文 |',
        '|---|---|---:|---:|---:|---|']
    for x in comparisons:
        sid = x['sample']
        cells = [f"{x['baseline_stats'][c]['ratio']:.1%} → {x['reading_stats'][c]['ratio']:.1%}" for c in 'ABC']
        rows.append(f"| {sid} | {x['reading_status']} | " + ' | '.join(cells) + f" | [A]({sid}/extraction/A.md) · [B]({sid}/extraction/B.md) · [C]({sid}/extraction/C.md) |")
    recovered = [(x['sample'], p) for x in comparisons for p in x['probes']
        if p['expected']=='candidate' and p['baseline_source_found'] and not p['baseline_selected'] and p['state']=='retained']
    rows += ['', '本轮新取回的代表性条目：']
    rows += ([f"- {sid} {p['category']}：{p['phrase']}。" for sid, p in recovered] or ['- 无。'])
    rows += ['', '占比是各自输入表示中候选字符量/全文字符量的负载指标。阅读会展开、重复或改写表格，前后分母不同，不能把占比降低直接解释为准确率提高。ABC允许重叠。', '',
        '## 仍然漏选或未字面确认的代表性内容', '', '| 样本 | 类别 | 内容 | 当前状态 | 上批是否进入候选 |', '|---|---|---|---|---|']
    for x in comparisons:
        for p in x['probes']:
            if p['expected']=='candidate' and p['baseline_source_found'] and p['state']!='retained':
                rows.append(f"| {x['sample']} | {p['category']} | {p['phrase']} | {p['state']} | {p['baseline_selected']} |")
    rows += ['', '## 边界反例', '', '| 样本 | 类别 | 内容 | 阅读文字可确认 | 仍进入候选 |', '|---|---|---|---|---|']
    for x in comparisons:
        for p in x['probes']:
            if p['expected']=='exclude':
                rows.append(f"| {x['sample']} | {p['category']} | {p['phrase']} | {p['reading_source_found']} | {p['reading_selected']} |")
    planning_path = output / 'planning_summary.json'
    if planning_path.exists():
        plans = json.loads(planning_path.read_text())
        old_plans = [x for x in json.loads((baseline / 'planning_summary.json').read_text()) if x['mode']=='ooxml']
        bodies = []
        for x in plans:
            plan = json.loads((output / x['sample'] / 'extraction/planning/plan.json').read_text())
            bodies.extend(p['body_tokens'] for p in plan['planned'])
        rows += ['', '## N01分块预演（没有调用整理模型）', '',
            f"相同配置下，计划整理输入数由{sum(x['planned_requests'] for x in old_plans)}降到{sum(x['planned_requests'] for x in plans)}；完整输入token由{sum(x['input_tokens'] for x in old_plans):,}变为{sum(x['input_tokens'] for x in plans):,}。分块数减少不等于材料负载减少，表格展开及候选范围变化也改变了输入文字。", '',
            f"本轮{sum(t<300 for t in bodies)}个输入的负责正文不足300 token，预算阻断{sum(x['blocked'] for x in plans)}个。输入片段仍偏碎；预算通过不证明模型能够完整整理。每份extraction/planning/inputs保存预演消息，plan.json记录原文切片及预算。"]
    rows += ['', '## 文件与复现', '',
        '- batch.json：源文件哈希、命令参数、状态、耗时、阅读指标；每份command.json记录实际调用命令。',
        '- Sxx/reading/：本轮全新阅读产物、模型调用审计、source_map和validation；reading.log为独立进程日志。',
        '- Sxx/extraction/A.md、B.md、C.md：当前规则选出的候选原文、章节、来源身份和命中原因。',
        '- comparison.json：与上一批相同条目的完整对照；本报告仅作有限证据核对。', '',
        '```bash', 'python scripts/evaluate_reading_location.py --output reviews/new_reading_location_batch', '```', '',
        '该复现命令会实际调用阅读模型服务，需要新输出目录。阅读模块和业务定位规则本轮未修改。完整阅读模块运行仍不表示已经展开外部附件、嵌入压缩包或识读全部图片；原有覆盖边界与诊断继续保留。']
    failures = [x for x in batch['items'] if x['status']=='failed']
    if failures:
        rows += ['', '## 失败文件', '']
        for x in failures:
            rows.append(f"- {x['sample']}：{x.get('error')}；日志：{x['sample']}/reading.log。")
    write_atomic(output / '测试报告.md', '\n'.join(rows) + '\n')


async def run(args):
    args.output.mkdir(parents=True, exist_ok=False)
    summary = (ROOT / 'docs/内容对应关键词分析/24份招标样本技术目录依据汇总.md').read_text()
    items = []
    for line in summary.splitlines():
        match = re.search(r'^\|S(\d+) .*?\]\(<([^>]+\.docx)>\)', line)
        if match:
            sid, filename = match[1], match[2].rsplit('/', 1)[-1]
            source = args.input / filename
            if not source.is_file():
                raise ValueError(f'missing S{sid}: {source}')
            items.append({'sample': 'S' + sid, 'input': str(source.resolve()),
                'input_sha256': hashlib.sha256(source.read_bytes()).hexdigest(), 'status': 'pending'})
    if len(items) != 24:
        raise ValueError('requires 24 samples')
    manifest = {'started_at': stamp(), 'status': 'running', 'scope': 'real reading then ABC location; no N01-N05',
        'reader_base_url': args.base_url, 'reader_model': args.model, 'thinking': 'off',
        'tokenizer': str(args.tokenizer), 'parallel_files': args.parallel, 'workers_per_file': args.workers,
        'logical_window': 20480, 'generation_tokens': 8192, 'input_budget': 11264,
        'locator_sha256': hashlib.sha256((ROOT/'src/bid_outline/document/locate.py').read_bytes()).hexdigest(),
        'items': items}
    def publish():
        save(args.output / 'batch.json', manifest)
    publish()
    slots = asyncio.Semaphore(args.parallel)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
               TOKENIZERS_PARALLELISM='false', OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1', MKL_NUM_THREADS='1')
    if not env.get('OUTLINE_GEN_API_KEY') and env.get('BID_OUTLINE_API_KEY'):
        env['OUTLINE_GEN_API_KEY'] = env['BID_OUTLINE_API_KEY']
    async def one(item):
        async with slots:
            folder = args.output / item['sample']
            folder.mkdir()
            item.update(status='reading', started_at=stamp())
            publish()
            reading = folder / 'reading'
            command = [sys.executable, '-B', '-u', '-m', 'outline_gen.cli.read_document',
                '--input', item['input'], '--output-dir', str(reading.resolve()),
                '--provider', 'vllm', '--runtime', 'api', '--base-url', args.base_url,
                '--model', args.model, '--tokenizer', str(args.tokenizer), '--thinking', 'off',
                '--max-tokens', '8192', '--heading-max-tokens', '4096',
                '--workers', str(args.workers), '--heading-workers', str(args.workers),
                '--request-max-tokens', '11264', '--table-chunk-tokens', '5632', '--toc-max-tokens', '10240']
            save(folder / 'command.json', command)
            start = time.monotonic()
            try:
                with (folder / 'reading.log').open('w') as log:
                    process = await asyncio.create_subprocess_exec(*command, cwd=ROOT/'document-reading',
                        env=env, stdout=log, stderr=asyncio.subprocess.STDOUT)
                    item['pid'] = process.pid
                    publish()
                    item['exit_code'] = await process.wait()
                if (reading / 'manifest.json').exists():
                    state = json.loads((reading / 'manifest.json').read_text())
                    item['reading_status'] = state['status']
                if item['exit_code'] not in (0, 2):
                    item.update(status='failed', error='reading CLI failed; see reading.log')
                else:
                    doc = read_document(reading)
                    result = evaluation.evaluate(doc, item['sample'][1:], folder / 'extraction')
                    save(folder / 'metrics.json', result)
                    item.update(status='extracted', source_chars=result['source_chars'], stats=result['stats'],
                        positive_found=sum(p['expected']=='candidate' and p['source_found'] for p in result['probes']),
                        positive_hit=sum(p['expected']=='candidate' and p['source_found'] and p['selected'] for p in result['probes']))
                if (reading / 'model_call_metrics.json').exists():
                    item['reading_metrics'] = json.loads((reading / 'model_call_metrics.json').read_text())
            except Exception as exc:
                item.update(status='failed', error=f'{type(exc).__name__}: {exc}')
            finally:
                item.update(finished_at=stamp(), elapsed_seconds=round(time.monotonic()-start, 2),
                    source_unchanged=hashlib.sha256(Path(item['input']).read_bytes()).hexdigest()==item['input_sha256'])
                publish()
                print(item['sample'], item['status'], item.get('reading_status'), item['elapsed_seconds'], flush=True)
    await asyncio.gather(*(one(item) for item in items))
    manifest.update(status='completed_with_failures' if any(x['status']=='failed' for x in items) else 'completed', finished_at=stamp())
    publish()
    report(args.output, ROOT / 'reviews/rule_location_24_20261109_baseline')
    print('OUTPUT', args.output.resolve(), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, default=ROOT/'test_inputs/input')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--base-url', default='http://10.21.150.137:50007/v1')
    parser.add_argument('--model', default='qwen35')
    parser.add_argument('--tokenizer', type=Path, default=Path('/groups/g900403/home/share/zxc/models/Qwen3.5-27B'))
    parser.add_argument('--parallel', type=int, default=2)
    parser.add_argument('--workers', type=int, default=2)
    args = parser.parse_args()
    if args.parallel < 1 or args.workers < 1 or not args.tokenizer.is_dir():
        parser.error('positive concurrency and an existing local tokenizer required')
    asyncio.run(run(args))


if __name__ == '__main__':
    main()
