"""Position rules and local signals; marker meaning is deliberately undecided."""
import re

VERSION = 'position-rules-1'
POSITIONS = {
    'A': re.compile(r'(?:投标|响应)文件(?:的)?(?:内容及)?(?:格式|编制|组成|构成)|须知|前附表|模板|附件|技术标部分|商务技术文件'),
    'B': re.compile(r'评标|评审|评分|评定标|定标办法'),
    'C': re.compile(r'技术条款|技术(?:和服务)?要求|(?:项目|采购|服务)需求|招标内容|需求书|技术规范|须知|前附表|商务(?:要求|条款)|合同|附件'),
}
LOCAL = {
    'A': re.compile(r'技术(?:标|部分|文件)|方案|计划|预案|施工组织设计|服务承诺|其他(?:技术)?资料|编制说明|格式自拟|内容自拟|偏离表|(?:必须|应|至少)包括|包括但不限于|文件组成|文件构成'),
    'B': re.compile(r'技术|服务|方案|评分|得分|评审|优良中差|完整性|先进性'),
    'C': re.compile(r'[★☆▲△■]|负偏离|实质性|必须|至少包括|包括但不限于|应包括|需涵盖|投标人承诺|书面明确|(?:提交|提供).{0,18}(?:方案|计划|预案|说明|清单|证明|证书|报告|截图|承诺)|(?:投标文件|响应文件).{0,50}(?:提供|提交|列出|说明|承诺)'),
}
WEAK = re.compile(r'[★☆▲△■]|至少包括|包括但不限于|应包括|需涵盖')


def compact(text):
    # Do not concatenate paragraphs: matching runs on an individual unit/line.
    return re.sub(r'[^\S\n]+', '', text)


def position_categories(path):
    return [category for category, pattern in POSITIONS.items() if pattern.search(compact(' / '.join(path)))]
