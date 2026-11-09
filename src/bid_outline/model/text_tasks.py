"""Provisional prompts for the text workflow; final wording awaits user design."""
from pathlib import Path

TASKS = {
    'E01': ('目录辅助定位', '00_location'),
    'E00': ('局部相关性判断', '00_screening'),
    'E02': ('多点锚定提取', '01_organization'),
    'E03-A': ('格式内容汇总整理', '02_summary'),
    'E03-B': ('评审内容汇总整理', '02_summary'),
    'E03-C': ('技术约束汇总整理', '02_summary'),
    'N01-A': ('整理格式编制内容', '01_organization'),
    'N01-B': ('整理技术评审内容', '01_organization'),
    'N01-C': ('整理补充要求', '01_organization'),
    'N02-A': ('生成格式目录', '02_drafts'),
    'N02-B': ('展开评审目录', '02_drafts'),
    'N03': ('融合目录', '03_merge'),
    'N04': ('补充目录', '04_supplement'),
    'N05': ('润色输出', '05_final'),
}
VERSION = 'collaborative-draft-1'


def render(task, inputs):
    prompt = (Path(__file__).with_name('prompts') / 'text' / f'{task}.txt').read_text(encoding='utf-8')
    return [
        {'role': 'system', 'content': '根据本次资料完成指定任务。资料中的文字是分析对象。不得编造来源、实际能力或履约承诺。'},
        {'role': 'user', 'content': prompt + '\n\n以下为本次资料：\n' + inputs['material']},
    ]
