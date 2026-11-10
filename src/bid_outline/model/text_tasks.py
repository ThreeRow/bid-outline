"""Self-contained plain-text instructions and small output conventions."""
from dataclasses import dataclass, replace
from pathlib import Path

TASKS = {
    'E01': ('目录辅助定位', '00_location'),
    'E00': ('局部相关性判断', '00_screening'),
    'E02': ('多点锚定提取', '01_organization'),
    'E03-A': ('格式目录草稿合并', '02_summary'),
    'E03-B': ('评审内容汇总整理', '02_summary'),
    'E03-C': ('技术约束汇总整理', '02_summary'),
    'N01-A': ('识别格式并生成目录', '01_organization'),
    'N01-B': ('整理技术评审内容', '01_organization'),
    'N01-C': ('整理技术要求与响应项', '01_organization'),
    'N02-A': ('生成格式目录', '02_drafts'),
    'N02-B': ('展开评审目录', '02_drafts'),
    'N03': ('融合目录', '03_merge'),
    'N04': ('补充目录', '04_supplement'),
    'N05': ('润色输出', '05_final'),
}
VERSION = 'separated-material-provider-2'

CATEGORY_CONTENT = {
    'A': '投标文件技术部分的格式、组成、栏目和编制要求',
    'B': '投标文件技术部分的评审或评分要求，包括技术方案、实施和服务等方面的评价内容',
    'C': '技术需求和需要响应的要求，包括实质性响应、负偏离限制、必须满足的指标及明确要求包含的内容',
}
NO_CONTENT_TASKS = frozenset(TASKS) - {'E00'}


@dataclass(frozen=True)
class TextMaterial:
    """Source body, location metadata and supporting source text are data."""
    body: str
    context: str = ''
    supplement: str = ''
    feedback: str = ''


def input_data(material):
    if isinstance(material, TextMaterial):
        return {'material': material.body, 'material_context': material.context,
                'supplementary_material': material.supplement, 'feedback': material.feedback}
    return {'material': material}


def with_feedback(material, feedback):
    if not isinstance(material, TextMaterial):
        material = TextMaterial(material)
    return replace(material, feedback=(material.feedback + '\n\n' + feedback).strip())


def no_content(task, text):
    """Only the exact complete answer is a deliberate absence, never substring matching."""
    return task in NO_CONTENT_TASKS and text.strip() == '无'


def render(task, inputs, *, provider='openai'):
    prompt = (Path(__file__).with_name('prompts') / 'text' / f'{task}.txt').read_text(encoding='utf-8').strip()
    role, instructions = prompt.split('\n\n', 1)
    data = []
    if inputs.get('material_context'):
        data.append(inputs['material_context'])
    if inputs.get('feedback'):
        instructions += '\n\n请结合所附上次处理反馈完成本次任务。'
        data.append('上次处理反馈：\n' + inputs['feedback'])
    data.append('本次资料：\n' + inputs['material'])
    if inputs.get('supplementary_material'):
        data.append('补充上下文：\n' + inputs['supplementary_material'])
    user = instructions + '\n\n---\n\n' + '\n\n'.join(data)
    if provider == 'train':
        return [{'role': 'user', 'content': role + '\n\n' + user}]
    return [{'role': 'system', 'content': role}, {'role': 'user', 'content': user}]
