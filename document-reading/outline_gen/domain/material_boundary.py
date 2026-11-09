"""Shared, evidence-producing rules for existing bidder materials."""
from __future__ import annotations

import re
from typing import Any

_PERSON = r"(?:项目经理|项目负责人|技术负责人|项目主管|项目总监|项目组成员|项目团队|团队人员|团队成员|正式员工|在职员工|拟派(?:人员|团队|项目经理)?|实施人员|服务人员|运维人员|驻场人员|技术人员|核心人员|人员配备|人员配置|人员资格|人员资质|人员证书)"
# Matching windows never cross independent comma-separated duties. Generic
# qualifications/guarantees are not evidence of existing credentials.
_PATTERNS = (
    ("personnel_credentials", re.compile(
        _PERSON + r".{0,28}(?:证书|职称|学历|学位|社保|社会保险|从业年限)|"
        + _PERSON + r".{0,24}(?:资质|资格).{0,12}(?:证明|材料|核验|复印|扫描|原件)|"
        + _PERSON + r".{0,16}(?:具备|持有|具有|须有|应有).{0,12}(?:注册|执业).{0,8}资格|"
        r"(?:人员|团队成员|项目成员)(?:既有)?(?:资质证明|资格证明|证书|社保|简历)|"
        r"拟派.{0,12}(?:简历|资质证书|资格证书)", re.I)),
    ("past_performance", re.compile(
        r"(?:类似业绩|同类业绩|过往业绩|企业业绩|历史业绩|成功案例|合同案例)|"
        r"(?:投标人|供应商|企业|我方).{0,30}(?:已完成|曾承担|具有).{0,30}"
        r"(?:项目业绩|类似项目|同类项目|项目案例|合同|业绩).{0,30}(?:证明|复印|验收|得分)", re.I)),
    ("enterprise_credentials", re.compile(
        r"(?:投标人|供应商|企业|本公司|厂商|制造商).{0,24}"
        r"(?:资质|认证证书|体系认证|荣誉|奖项).{0,24}(?:证明|证书|复印|扫描|提供|具有|持有)|"
        r"(?:企业资质|投标人资质|供应商资质|企业认证证书)(?:证明|材料|核验|汇编)?", re.I)),
    ("existing_resource_evidence", re.compile(
        r"(?:投标人|供应商|我方|本公司).{0,20}(?:自有|现有|已配备|既有).{0,24}"
        r"(?:设备|仪器|车辆|场地|实验室|服务网点|办公环境).{0,24}(?:证明|发票|产权|照片|实景)|"
        r"自有.{0,20}(?:设备|场地|实验室|车辆).{0,24}(?:证明|产权|发票|照片)", re.I)),
    ("third_party_document", re.compile(
        r"(?:第三方(?:厂商)?|厂商|制造商|原厂|厂家).{0,16}(?:承诺函|授权函|授权书)", re.I)),
)

# A negation applies to its local list only. A new affirmative instruction or
# contrast resets it; do not exempt a whole sentence containing '不涉及'.
_SEGMENT_BOUNDARY = re.compile(
    r"[\n。；;，,！？!?]+|但是|但|然而|不过|而(?:应|须|需)|"
    r"并且|而且|且|同时|并(?=须|应|需|提供|提交|出具|取得|列出|展示|编制)|"
    r"(?:仍|另)(?=须|应|需|提供|提交|出具|取得|列出|展示|编制)"
)
_NEGATION = re.compile(
    r"不涉及|不包含|不包括|不提供|不提交|不列出|不罗列|不列举|不收集|不展示|不编制|不要求|不需要|不保留|不依赖|不以|不基于|"
    r"不得(?!不|缺少|遗漏)|禁止|严禁|剔除|排除|删除|无需|无须|避免(?!遗漏|缺少)|"
    r"不能(?!不|缺少|遗漏)|不应(?!缺少|遗漏)|不要(?!遗漏|缺少)|而非|而不是|并非(?!不)"
)
_LICENSE_FLOW = re.compile(r"(?:软件|License|许可证).{0,30}(?:激活|绑定|授权交付|许可交付|有效期)", re.I)
_PRESENTATION = re.compile(r"投标|扫描|盖章|模板|证明材料|材料索引|文件清单")


def _is_negative(text: str) -> bool:
    # Double negatives demand the material; do not mistake their inner
    # 不提交/不提供 for an exclusion.
    normalized = re.sub(r"(?:不能不|不得不|不可不|并非不|不是不)", "必须", text)
    return bool(_NEGATION.search(normalized) or re.search(
        r"非(?=人员资质|个人资质|资质证书|证书清单|社保证明)|"
        r"(?:不以|不依赖|不基于)(?:个人|人员)?(?:资质|证书|社保|业绩)", normalized))


def material_findings(text: str, *, field: str = "") -> list[dict[str, Any]]:
    """Return positive matches with offsets into the original field text."""
    findings: list[dict[str, Any]] = []
    start = 0
    negative_list = False
    boundaries = [*list(_SEGMENT_BOUNDARY.finditer(text)), None]
    for boundary in boundaries:
        end = boundary.start() if boundary else len(text)
        original = text[start:end]
        offsets = [i for i, char in enumerate(original) if not char.isspace()]
        clause = "".join(original[i] for i in offsets)
        inherited_negative = negative_list and bool(re.match(
            r"(?:以及|及|和|或)?(?:社保|社会保险|历史业绩|类似业绩|企业资质|证书|职称|学历|学位|第三方|原厂|人员)", clause
        )) and not re.search(r"须|应|需|提供|提交|出具|取得|列出|展示|编制|具备|持有|具有", clause)
        for code, pattern in _PATTERNS:
            for match in pattern.finditer(clause):
                matched = match.group()
                # Credentials can be the data processed by the purchased
                # system. Bind this exception to an explicit processing duty,
                # rather than exempting an arbitrary sentence containing 系统.
                if (code in {"enterprise_credentials", "personnel_credentials", "past_performance"}
                        and re.search(r"(?:系统|工具|平台|模块|功能|规则).{0,30}(?:审查|核验|校验|识别|抽取|解析|比对|审计|查询)", clause)
                        and not re.search(r"(?:投标人|供应商|我方)(?:应|须|需|必须)?(?:提供|提交|持有|具有)", clause)):
                    continue
                # Bind the software exception to this authorization object,
                # not any unrelated License operation elsewhere in the clause.
                if (code == "third_party_document" and "授权书" in matched
                        and re.search(r"软件|License|许可证", matched, re.I)
                        and _LICENSE_FLOW.search(clause)
                        and not _PRESENTATION.search(clause)):
                    continue
                if (code == "personnel_credentials"
                        and re.search(r"(?:数字|身份认证|产品|培训|结业).{0,4}证书$", matched)):
                    continue
                if (code == "enterprise_credentials"
                        and re.search(r"(?:产品|设备|系统).{0,8}(?:认证|证书)", matched)
                        and not re.search(r"企业资质|投标人资质|供应商资质|企业认证证书", matched)):
                    continue
                # Include negations inside a broad personnel match as well as
                # those preceding it (e.g. 核心人员...不涉及人员资质证明).
                if inherited_negative or _is_negative(clause[:match.end()]):
                    continue
                left, right = start + offsets[match.start()], start + offsets[match.end() - 1] + 1
                findings.append({"code": code, "field": field,
                                 "match": text[left:right], "start": left, "end": right,
                                 "context": original.strip()})
        negative_list = bool(boundary and boundary.group() in {"，", ","}
                             and (inherited_negative or _is_negative(clause)))
        start = boundary.end() if boundary else end
    return findings


def real_material_reasons(text: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(finding["code"] for finding in material_findings(text)))


def item_material_findings(item: dict[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for key in ("title", "reason", "summary", "split_plan", "writing_outline"):
        value = item.get(key)
        values = enumerate(value) if isinstance(value, list) else [(None, value)]
        for index, text in values:
            if isinstance(text, str):
                field = key if index is None else f"{key}[{index}]"
                findings.extend(material_findings(text, field=field))
    for key in ("children", "l1_nodes", "nodes"):
        children = item.get(key)
        if isinstance(children, list):
            for index, child in enumerate(children):
                if isinstance(child, dict):
                    findings.extend({**finding, "field": f"{key}[{index}].{finding['field']}"}
                                    for finding in item_material_findings(child))
    return findings


def item_material_reasons(item: dict[str, Any]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(finding["code"] for finding in item_material_findings(item)))


class MaterialBoundaryError(ValueError):
    """A parseable result violates the boundary; findings retain exact evidence."""

    def __init__(self, message: str, *, findings: list[dict[str, Any]] | None = None):
        super().__init__(message)
        self.findings = findings or []


def require_material_boundary(item: dict[str, Any], *, label: str) -> None:
    findings = item_material_findings(item)
    if findings:
        reasons = list(dict.fromkeys(finding["code"] for finding in findings))
        evidence = "；".join(f"{f['field']} 命中「{f['match'][:120]}」" for f in findings[:6])
        raise MaterialBoundaryError(
            f"{label} 含真实材料禁用内容: {reasons}。具体证据：{evidence}。"
            "请逐项纠正上述字段，并返回完整结果：将材料呈现改为技术职责、实施步骤或服务执行机制；"
            "删除既有证书、社保、历史业绩及第三方函件的清单、索引、模板与扫描盖章提纲。"
            "保留岗位能力要求、组织分工、技术认证、License激活交付、测试验收、培训和服务措施。"
            "不要仅改标题，也不要在工作卡中复述禁用材料清单或声明已剔除。",
            findings=findings,
        )


def require_tree_material_boundary(root: Any) -> None:
    from .tree import walk
    findings: list[dict[str, Any]] = []
    for node in walk(root):
        if node.node_id != "ROOT":
            findings.extend({**finding, "node_id": node.node_id}
                            for finding in item_material_findings({k: v for k, v in node.to_dict().items() if k != "children"}))
    if findings:
        # Validate all nodes first so retries can correct multiple offending
        # children at once, rather than discovering one per model call.
        items = {f"node[{finding['node_id']}].{finding['field']}": finding["context"]
                 for finding in findings}
        evidence = "；".join(f"{field}：{value[:120]}" for field, value in list(items.items())[:6])
        raise MaterialBoundaryError(
            f"目录含真实材料禁用内容: {list(dict.fromkeys(f['code'] for f in findings))}。"
            f"具体证据：{evidence}。删除材料清单、索引与模板，保留技术职责及服务执行机制，重新生成完整结果。",
            findings=findings,
        )
