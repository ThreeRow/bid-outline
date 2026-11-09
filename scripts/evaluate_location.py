"""Offline ABC locator evaluation; no model client, service or workflow execution.

DOCX coverage uses the existing OOXML reader with deterministic heading paths.
Historical version=2 inputs are evaluated separately, only after SHA256 matching.
"""
import argparse
import asyncio
from dataclasses import asdict
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'document-reading')]
from outline_gen.document_reading.word_structure import read_word_structure
from bid_outline.document.readers import table_rows, read_document
from bid_outline.document.locate import locate, normalized
from bid_outline.domain import Document, DocumentElement, SourceRef

# Representative positive/negative evidence from the three supplied case reports.
# Not a complete annotation set and not model-generated semantic ground truth.
PROBES = {
 '01': [('A','投标人提交的其他资料'),('B','总体技术服务方案'),('B','故障响应及处理流程'),('C','组网方案简要说明')],
 '02': [('A','项目实施方案'),('B','日常运维统计和报告表单模板'),('B','信息安全管理方案'),('C','供应商需提交项目质量计划')],
 '03': [('A','技术偏离表'),('B','供货保障方案'),('B','与昆山市交通局称重平台系统的对接方案')],
 '04': [('A','售后服务方案'),('C','在投标文件中提供针对性方案设计'),('C','培训课程计划须随投标文件一起提交'),('C','在投标文件中提供质保期后的服务方案')],
 '05': [('A','平台应用培训及搭建技术支撑'),('B','核心业务需求'),('B','技术架构、平台选型、性能保障、安全设计')],
 '06': [('A','技术需求偏离表'),('B','施工组织方案'),('B','主要分部分项工程施工方案和技术措施')],
 '07': [('A','服务方案'),('B','需求分析的全面性、准确性、充分性'),('B','项目管理与实施'),('C','提供承诺函并加盖公章')],
 '08': [('A','项目技术方案或服务说明'),('B','拟用方法、可行性分析'),('B','培训目标、培训形式、培训规模、培训批次')],
 '09': [('A','技术部分中必须包括的内容'),('B','硬件整体架构、安全管理、运维支持'),('C','设备尺寸图及机柜安装兼容性说明')],
 '10': [('A','总体实施方案'),('B','人员考核内容、考核方式、考核标准'),('B','培训对象、培训内容、培训场地、培训方式')],
 '11': [('A','提供培训计划'),('B','项目实施保密'),('B','平台兼容性、安全性方案')],
 '12': [('A','项目实施方案'),('B','服务保障体系、服务组织、服务制度'),('B','培训计划、培训师资力量、培训内容')],
 '13': [('A','第四章评标标准要求提供的方案或相关证明材料'),('B','设备安装调试、质量控制'),('C','配套材料和工作，供应商应在投标文件中列出')],
 '14': [('A','系统替换实施方案'),('B','操作系统安装、测试、优化'),('C','需提供软件著作权登记证书')],
 '15': [('A','提供培训计划'),('B','项目实施集成方案'),('C','投标文件中突发事件应急处理方案')],
 '16': [('A','投标人服务要求/实施方案偏离表'),('B','关键时间节点'),('C','投标人在编制投标文件时必须列出')],
 '17': [('A','施工组织设计'),('B','施工部署、施工准备、施工现场布置'),('C','低压电工证书和高压电工证书')],
 '18': [('A','投标文件的技术部分中必须包括的内容'),('A','保修期后服务计划'),('C','GPU算力资源扩容')],
 '19': [('A','系统总体要求理解'),('B','二三级公司需求'),('C','人员配置管理计划')],
 '20': [('A','项目实施组织方案'),('B','SRv6隧道规划'),('B','平滑的割接实施方案')],
 '21': [('A','技术方案、服务承诺、培训承诺'),('B','数据资源梳理、数据库建设、数据汇聚'),('B','市县数据对接方案'),('C','投标人承诺')],
 '22': [('A','投标人提交的其他资料'),('B','未标符号'),('C','免费质保期以后的售后服务方案承诺书')],
 '23': [('A','评分细则中技术部分需提供的相关证明材料'),('B','备品备件及消耗品储备方案'),('B','项目组织架构配置方案'),('C','提供承诺函')],
 '24': [('A','C8、其他'),('B','47项通用审查规则'),('B','思路清晰度'),('B','动态更新机制')],
}
NEGATIVES = {
 '05': [('C','项目交付技术文档资料包括')],
 '06': [('C','施工组织设计应包含以下内容')],
 '10': [('C','合同签订后5个工作日内提交服务方案')],
 '12': [('C','在驻场运维过程中需交付')],
 '21': [('C','项目启动阶段')],
 '24': [('B','分值设置不合理'),('C','提交完整数据库设计文档')],
}


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def adapt(path):
    words, issues = read_word_structure(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    docid, elements, stack = 'ooxml-' + digest[:20], [], []
    for order, word in enumerate(words):
        if word.is_toc:
            section = ['目录（OOXML样式）']
        else:
            chapter = re.match(r'^第[一二三四五六七八九十百\d]+[章部分节篇]', normalized(word.text))
            level = 0 if chapter and len(word.text) < 100 else word.outline_level
            if word.kind == 'paragraph' and level is not None and level < 5 and len(word.text) < 100:
                stack = [item for item in stack if item[0] < level] + [(level, word.text)]
            section = [title for _, title in stack]
        metadata = {'style': word.style, 'outline_level': word.outline_level, 'is_toc': word.is_toc}
        cells = []
        if word.table:
            raw = asdict(word.table)
            metadata.update(source_table=raw, table_rows=table_rows(raw))
            cells = [c.id for c in word.table.cells]
        elements.append(DocumentElement(element_id=word.id, order=order, kind=word.kind,
            text=word.text, section_path=section, metadata=metadata,
            source_ref=SourceRef(document_id=docid, element_ids=[word.id],
                table_id=word.table.id if word.table else None, cell_ids=cells,
                precision='table' if word.table else 'block', reader_ref={'word_path': word.path})))
    return Document(document_id=docid, source_hash=digest, reader_name='offline_ooxml_probe',
                    reader_version='existing_word_structure+deterministic_paths_v1', elements=elements, diagnostics=issues)


def evaluate(doc, sid, folder):
    ranges = locate(doc)
    stats, texts = {}, {}
    for category in 'ABC':
        chosen = [r for r in ranges if r.category == category]
        texts[category] = '\n\n'.join(e.text for r in chosen for e in r.document.elements)
        ids = {e.element_id for r in chosen for e in r.document.elements}
        stats[category] = {'ranges': len(chosen), 'elements': len(ids), 'chars': len(texts[category]),
                           'ratio': round(len(texts[category]) / max(1, sum(len(e.text) for e in doc.elements)), 4)}
        lines = [f'# S{sid} {category}候选原文', '', '未经模型整理；关键词命中不等于要求性判断。', '']
        for r in chosen:
            lines += ['## ' + r.range_id, '', '命中：' + '；'.join(r.reasons), '']
            for e in r.document.elements:
                lines += [f'### {e.element_id} / ' + ' / '.join(e.section_path), '', e.text, '']
        write(folder / f'{category}.md', '\n'.join(lines))
    probes = []
    full = normalized('\n'.join(e.text for e in doc.elements))
    for expected, items in ((True, PROBES[sid]), (False, NEGATIVES.get(sid, []))):
        for category, phrase in items:
            needle = normalized(phrase)
            source = [e for e in doc.elements if needle in normalized(e.text)]
            probes.append({'category': category, 'phrase': phrase, 'expected': 'candidate' if expected else 'exclude',
                'source_found': needle in full, 'selected': needle in normalized(texts[category]),
                'source_elements': [{'id': e.element_id, 'path': e.section_path} for e in source]})
    record = {'sample': 'S' + sid, 'reader': doc.reader_name, 'source_chars': sum(len(e.text) for e in doc.elements),
              'source_elements': len(doc.elements), 'heading_paths': len({tuple(e.section_path) for e in doc.elements}),
              'stats': stats, 'probes': probes, 'diagnostics': doc.diagnostics}
    write(folder / 'document.json', doc.model_dump(mode='json'))
    write(folder / 'ranges.json', [r.record() for r in ranges])
    write(folder / 'metrics.json', record)
    return record


async def plan_document(doc, folder, config, budget):
    """Run the actual organizer's planning path, replacing only its model boundary.

    Saves proposed messages and source slices, never executes/adopts model results.
    """
    from types import SimpleNamespace
    from bid_outline.workflow.text_runner import TextWorkflowRunner
    from bid_outline.document.organization import route_organization
    from bid_outline.storage import ArtifactStore
    class PlanningRunner(TextWorkflowRunner):
        def __init__(self):
            self.config = config
            self.executor = SimpleNamespace(budget=budget)
            self.artifacts = ArtifactStore(folder)
            self.unprocessed = []
            self.planned = []

        async def call(self, task, material, parents, name):
            report = budget.check(self.messages(task, material), config.max_tokens, task_type=task)
            chunk = self.artifacts.get(parents[0])
            index = len(self.planned) + 1
            relative = f'inputs/{index:04d}_{task}.md'
            write(folder / relative, '\n\n'.join(m['role'] + '\n' + m['content'] for m in self.messages(task, material)))
            self.planned.append({'task': task, 'name': name, 'input': relative, 'budget': report,
                'body_tokens': budget.text_tokens(chunk['text']), 'source_refs': chunk['owned_refs'], 'parents': parents})
            return {'planning_only': True}  # No text result is adopted or passed to N02.

    folder.mkdir(parents=True, exist_ok=False)
    runner = PlanningRunner()
    source = runner.artifacts.put('document', doc.model_dump(mode='json'))
    ranges = locate(doc)
    parent = runner.artifacts.put('located_ranges', {'ranges': [r.record() for r in ranges]}, parents=[source])
    routed, routing = route_organization(doc, ranges)
    parent = runner.artifacts.put('organization_plan', routing, parents=[parent])
    write(folder / 'organization_plan.json', routing)
    for r in routed:
        await runner.organize(r, parent)
    result = {'model_calls': 0, 'scope': 'planned N01 inputs only; no organization results or directory generation',
              'planned': runner.planned, 'blocked': runner.unprocessed}
    write(folder / 'plan.json', result)
    return {'planned_requests': len(runner.planned), 'blocked': len(runner.unprocessed),
            'by_category': {c: sum(x['task'] == 'N01-' + c for x in runner.planned) for c in 'ABC'},
            'input_tokens': sum(x['budget']['input_tokens'] for x in runner.planned)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, default=ROOT / 'test_inputs/input')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--plan', action='store_true', help='Also save actual N01 planning inputs without model calls')
    parser.add_argument('--config', type=Path, default=ROOT / 'configs/train.text.example.json')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    summary = (ROOT / 'docs/内容对应关键词分析/24份招标样本技术目录依据汇总.md').read_text()
    samples = []
    for line in summary.splitlines():
        match = re.search(r'^\|S(\d+) .*?\]\(<([^>]+\.docx)>\)', line)
        if match:
            sid, name = match[1], match[2].rsplit('/', 1)[-1]
            path = args.input / name
            if not path.is_file():
                raise ValueError(f'missing sample S{sid}: {name}')
            samples.append((sid, path))
    if len(samples) != 24:
        raise ValueError('expected exactly 24 named samples')
    hashes = {hashlib.sha256(path.read_bytes()).hexdigest(): sid for sid, path in samples}
    historical = {}
    for root, dirs, files in os.walk(ROOT / 'runs'):
        dirs[:] = [d for d in dirs if d not in ('calls', 'artifacts', 'structure', '.git')]
        if 'document.json' in files and 'manifest.json' in files:
            manifest = json.loads((Path(root) / 'manifest.json').read_text())
            sid = hashes.get(manifest.get('input_sha256'))
            if sid and (sid not in historical or (Path(root) / 'document.json').stat().st_mtime > historical[sid].stat().st_mtime):
                historical[sid] = Path(root) / 'document.json'
    results = []
    for sid, path in samples:
        doc = adapt(path)
        item = evaluate(doc, sid, args.output / f'S{sid}' / 'ooxml')
        item.update(input=str(path.resolve()), input_sha256=doc.source_hash)
        if sid in historical:
            try:
                production = read_document(historical[sid])
                item['historical_reading'] = evaluate(production, sid, args.output / f'S{sid}' / 'reading')
                item['historical_reading']['input'] = str(historical[sid])
            except Exception as exc:
                item['historical_reading_error'] = f'{type(exc).__name__}: {exc}'
        results.append(item)
        print(f"S{sid} chars={item['source_chars']} " + ' '.join(f"{c}={item['stats'][c]['ratio']:.1%}" for c in 'ABC'), flush=True)
    write(args.output / 'results.json', {'timestamp': datetime.now(ZoneInfo('Europe/Berlin')).isoformat(),
        'model_calls': 0, 'scope': 'unchanged locate() only; no N01 organization or directory generation',
        'results': results})
    if args.plan:
        from bid_outline.config import load_config
        from bid_outline.model.budget import TokenBudget
        config = load_config(args.config)
        budget = TokenBudget(config)
        plans = []
        for sid, _ in samples:
            for mode in ('ooxml', 'reading'):
                path = args.output / f'S{sid}' / mode / 'document.json'
                if path.exists():
                    doc = Document.model_validate(json.loads(path.read_text()))
                    result = asyncio.run(plan_document(doc, path.parent / 'planning', config, budget))
                    plans.append({'sample': 'S' + sid, 'mode': mode, **result})
        write(args.output / 'planning_summary.json', plans)
    print('OUTPUT', args.output.resolve())


if __name__ == '__main__':
    main()
