# bid-outline

维护入口：[项目公共信息](PROJECT_CONTEXT.md) · [代码导引](docs/CODE_GUIDE.md) · [文本流程重构计划](docs/24_文本目录生成流程重构计划.md)。

当前版本 **0.13.1**：程序定位技术标编制与评审依据，模型在较小范围整理文本，再生成目录。新流程为：

```text
阅读产物 → 章节规则＋目录辅助 → 候选位置内匹配词和符号
        → 宽泛线索局部判断 → 完整候选分块
        → A直接出目录、B/C整理要求／多点锚定回取 → 按类别拼接与统一整理
        → A目录与B展开目录融合 → C补充 → 最终润色
```

当前只处理目录生成，暂不执行目录语义检查、自动修正或扩展建议。prompt已按用户审阅稿调整，尚未验证真实模型质量。中间文本允许列表、段落及分析，不要求模型提供固定字段、程序ID或JSON。

A为格式或集中编制要求，B为技术/服务评审内容，C为技术条款中的实质性响应、负偏离限制、必需指标和明确内容范围及相关材料要求。B中的具体列项与完整性、先进性等评价维度均显式展开。规则命中仅为候选，最终文本不等于全文无遗漏或语义验收通过。

## 环境与运行

开发环境：`bid-outline-qwen-runtime-cu128`，Python 3.11。无需重新安装CUDA或部署模型。

```bash
conda activate bid-outline-qwen-runtime-cu128
export PYTHONPATH="$PWD/src"
python -m bid_outline --help
python -m bid_outline doctor --config configs/train.text.example.json
python -m pytest tests -q
```

`doctor`默认不联网。`--check-service`才请求服务信息，也不证明生成质量或上下文能力。

模型接口优先使用支持批量的文本train模式；生成侧现支持query_list微批合并（示例2条），逐任务独立采用并沿共享票据恢复；尚未实测新批量路径吞吐。真实调用需要可用服务和本地CPU fast tokenizer，禁止隐式下载资源。`configs/train.text.example.json`是新流程示例，统一关闭thinking，仅为运行起点，尚未真实调优。模型身份由train服务端路由，配置model仅为审计标签。密钥通过环境变量读取。

```bash
python -m bid_outline run --reader reading --input /path/reading-output \
  --config configs/train.text.example.json --run /path/new-run
python -m bid_outline status --run /path/new-run
python -m bid_outline resume --run /path/new-run
python -m bid_outline export --run /path/new-run
```

`--reader reading`接收version=2的`document.json`或所在目录；`--reader sample`是历史样例适配器。`--reader docx`显式调用独立document-reading入口，目前仅支持OpenAI配置。train批处理先运行独立阅读进程，再生成目录，见[scripts说明](scripts/README.md)。

`text_batch_tokens`默认3000，约束单次整理及A/B生成的材料量；完整请求仍须满足上下文、生成额度和安全余量。不能安全拆分的单元、过大的当前目录或最终请求明确记为未完成，不截断原文或目录。

## 产物与恢复

```text
run/
  manifest.json / runtime.json / run.sqlite3 / run.lock
  artifacts/                     不可变来源和阶段产物
  calls.md / calls/              完整请求、响应、终态及采用审计
  stages/located_ranges.json     ABC候选范围、命中理由和来源
  stages/selected_ranges.json    位置与局部判断后的完整范围
  stages/local_screening.json    局部判断和待核/排除记录
  stages/organized/A.md B.md C.md 统一整理文本
  train_batches/                 共享批次、票据和成员审计
  stages/text_<task_hash>.md     已采用整理文本与草稿投影
  stages/final.md                已采用最终文本
  exports/backbone.md            最终文本或明确标注的未完成草稿
  exports/unresolved.md          执行失败、缺口及未处理范围
  exports/provenance.json        来源范围、阶段依赖和采用结果
```

文本来源可沿产物依赖追到输入范围，没有伪造逐标题的要求归属。SQLite及artifacts保存采用权威；stages是可再生成的阅读视图。

每个运行目录只允许一个进程。恢复核对代码、配置及`flow_version=collaborative-directory-2`，复用已采用文本和已保存的有效响应；train沿保存票据恢复轮询，提交结果不明时不盲目重提。修改配置、代码、协议或prompt使用新运行目录。旧P或0.12文本run不能恢复成新流程；已有旧run仍可只读status/export。

状态区分completed、completed_with_issues、incomplete、failed、cancelled。completed仅说明文本调用链成功，不证明回答是有效完整目录。空答案、已知截断、预算失败不算“没有要求”；存在未处理范围时标为incomplete。`semantic_quality`始终为not_evaluated。CLI无问题完成退出0，带问题/不完整退出2，故障退出1，中断退出130。

## 历史实现与质量边界

旧严格流程保留在`workflow/runner.py`及P01—P19模板，供历史对照与回归；CLI和工作流包默认指向`workflow/collaborative_runner.py`。strict/compact/markdown配置仍支持旧任务，对新N任务不生效。

旧实验结果按其日期和版本理解：[真实测试报告](docs/12_当前测试结果报告.md)、[提示词对照](docs/20_提示词真实模型对照测试.md)、[train与thinking](docs/21_文本train_provider与thinking实测.md)、[token分块](docs/22_token窗口与章节分块.md)、[Markdown实验](docs/23_分任务thinking与Markdown目录实验.md)。这些结果不证明新流程质量。

已知服务风险仍包括终态成功但answer为空、缺finish_reason时默认stop，以及不带明确截断标记的半句。新流程没有消除服务端问题；普通非空文本通过基础检查也可能漏项。定位规则、单次处理量、占位prompt及最终目录仍需真实样本验证。

2026-11-09本轮完整离线回归：**302项通过**。包含新文本流程及共享基础设施回归；未调用真实模型，不代表语义质量验收。

0.12.1：明确命中单池的材料只整理对应类别；通用提交词不将已明确的A/B材料再送C，多池证据或邻接歧义才保留对应多路。此项调整输入路由，尚未改进上游位置识别规则。


0.13.0运行终点：`--stop-after candidates`只取得完整候选，`--stop-after organized`整理出ABC文本，默认directory生成目录。`--extraction-mode direct/anchors`分别为直接整理和起止句＋中间锚点回取。可用`resume --stop-after directory`继续同一整理run，复用已采用提取；不能向前回退终点。见[协同提取计划](docs/25_规则与模型协同提取实现计划.md)与代码导引。

★☆▲△■用于定位，不预设其技术或强制性含义。“至少包括、包括但不限于”等先取局部上下文判断，需要后才回取完整内容。旧0.12.1 A/B优先压C的路由不再用于新流程。新E任务与N01-C均是待用户定稿的占位提示。

0.13.0离线回归：332项通过；规则回放及局部模型未执行的边界见`reviews/position_rules_24_20261109_v013_release/验证报告.md`。目录辅助、局部判断、锚定及汇总prompt尚待真实小模型验证。


0.13.1提示词适配：新文本任务是一条完整query，以`---`分隔说明与资料，train不再加system/user编号。A直接出目录并参与融合；N02-A只转换A锚点回取原文。精确整答“无”保存为无内容并跳过后继，空答仍失败。流程版本更新为collaborative-directory-2，需新run。阈值感知尚未实现，分块参数仍为当前3000 token配置。

本轮完整离线回归362项通过，未调用真实模型；新prompt的语义效果尚待实测。
