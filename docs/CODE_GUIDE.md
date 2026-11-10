# 代码导引：模块、文件与运行流转

核对日期：2026-11-10；当前版本0.15.2。默认工作流按完整章节块取正文。环境与约定见[PROJECT_CONTEXT](../PROJECT_CONTEXT.md)，设计与历史变更见[25号计划](25_规则与模型协同提取实现计划.md)。当前改变了选区职责，真实模型质量尚未验收。

## 1. 最短阅读路径

1. `src/bid_outline/cli.py::execute`：配置、阅读适配、run锁、运行终点及导出。
2. `document/readers.py::read_document`：读取version=2 corrected片段和章节树，表格采用reader展开正文。
3. `document/source_index.py::SourceIndex`：原文与局部线索索引，提供有来源的完整元素。
4. `document/section_blocks.py::SectionBlocks`：章节身份、父子关系、完整候选块回取。
5. `document/location_rules.py`与`locate.py::position_hits`：明确章节规则、宽泛位置规则、位置内关键词。
6. `workflow/extraction.py::Extraction`：目录辅助、局部判断、候选块去重、锚定及按需整合。
7. `workflow/collaborative_runner.py::CollaborativeRunner.run`：当前业务总编排。
8. `workflow/text_runner.py::organize`：复用材料包装、感知阈值、切分和并发。
9. `model/text_tasks.py`、`model/executor.py`、train provider：输入渲染、预算、采用及审计。
10. `export/markdown.py::export_result`：整理结果或目录导出。

下文源码路径以`src/bid_outline/`为基准。

## 2. 当前流程

```text
cli.execute → read_document → CollaborativeRunner.run
  → SourceIndex + SectionBlocks：正文、章节身份及父子关系
  → DIRECT明确章节规则 + E01模型目录定位
      明确位置：直接建立完整章节候选，包含后代章节
      须知/合同/附件等宽泛位置：位置内关键词定位
        宽泛表达/符号 → E00少量同章节上下文判断
        保留/待核 → 对应完整小节；没有更细结构时取完整所在章节
  → 按类别合并父子重叠候选，保留独立位置和多类用途
  → 每个完整候选检查9000感知阈值和真实请求预算
      可容纳：单次处理；否则以4000正文tokens为目标结构切分
      direct：N01-A生成目录，N01-B/C整理要求
      anchors：E02多点原文锚定 → 固定Python回取
               A再N02-A转目录；B/C由E03整理；失败有限澄清后显式回退
  → 长候选分块结果按源序E03整合；短块直接采用
  → 多个同类候选按需E03整合
  → A目录直接采用，N02-B独立评审分册有界并发生成目录，源序收集
  → N03融合 → N04根据C补充 → N05润色
```

关键词决定取哪个结构块，不决定块内哪行保留。完整候选以阅读模块已有章节/小节边界为准，不用编号猜测层级来缩小范围。不执行目录语义检查、自动修正或独立扩展建议。

## 3. 阅读与完整候选块

| 文件/接口 | 当前职责 |
|---|---|
| `readers.py::read_document` | 保留corrected章节树到metadata.reader_sections，片段保留section_id/chapter_id和完整来源；表格优先reader展开正文 |
| `source_index.py::SourceIndex` | 阅读元素与章节路径索引；局部行只用于找线索、局部判断，不用于筛除章节正文 |
| `section_blocks.py::SectionBlocks` | 优先按reader章节身份和parent_id建立节点；兼容旧适配产物的片段身份；缺身份时按连续路径和显式标题建立位置身份 |
| `SectionBlocks.matching` | E01返回真实路径/标题，多个同名位置视为歧义，不默认取第一处 |
| `SectionBlocks.document_for` | 取节点及后代的全部元素，保留原文顺序和完整text；设置完整范围来源，无shared_context碎片投影 |
| `SectionBlocks.located` | 一条LocatedRange对应一个完整章节块及类别、依据；record包含source_scope、section_key、section_path |
| `location_rules.py::DIRECT` | 明确格式编制、评审评分、技术/采购需求等章名直接建立候选，不再要求每行命中 |
| `location_rules.py::POSITIONS/LOCAL/WEAK` | 限定宽泛位置、提供局部线索及弱命中标记 |
| `locate.py::position_hits` | 仅产生定位证据；不再决定正文逐行取舍 |

同类父候选已包含子候选时只保留父块并合并依据，正文不重复；跨类别允许同一原文分别整理。宽泛位置有一个有效线索即可取该完整小节；排除一个弱线索不删除已由其他依据选中的块。E00节选排除不能授权丢弃未发送内容，保持待核。局部判断按章节身份隔离，同名不同章节不混合。

完整表格沿用reader展开text并标为expanded_table_text；原始单元格身份保留为来源上下文。为保持同一份正文，当前块不携带可触发原表行重渲染的table_rows，后续切分按展开文本偏移，不能声称精确单元格切片归属。

`SourceIndex.full_range/complete_units/document_for`保留历史局部片段逻辑，当前主选区不调用；`locate.py::locate`、`organization.py::route_organization`也不进入默认主流程。不要把原来的行筛选、表格逐字映射或A/B压C优先级接回主链路。

## 4. 分块、整理及整合

| 方法 | 职责 |
|---|---|
| `Extraction.locate` | 明确规则/E01直接选章；宽泛位置匹配与E00；完整回取和同类父子去重 |
| `CollaborativeRunner.run`的N02-B分册展开 | 复用Extraction.parallel与executor请求限额；独立分册并发、源序收集，失败/无内容沿既有处理，N03依赖融合仍串行 |
| `CollaborativeRunner.organize` | 直接处理locate给出的一个完整候选，不再按顶层章节重新合并独立小节 |
| `TextWorkflowRunner.organize(adaptive=True)` | 完整候选先计量，超9000或硬预算不足才切分，独立块并发并按规划源序返回 |
| `organization_material/material_tokens/perception_fits` | 资料分区、正文与必要条件计量、硬预算统一准入 |
| `token_chunks/split_chunk/expandable_tables` | 复用结构与负责范围切分；单元不可安全处理则明确失败 |
| `Extraction.anchors` | E02原文起止/中间锚点验证，最多一次修正，不默认取第一匹配，不执行模型生成代码 |
| `Extraction.summarize` | 单个已整理结果直接采用；分块/锚定原文/多位置按需整合；完整记录打包、有限层数、失败保留分册 |

4000只指导触发拆分后的正文大小，正文加必要条件仍受9000和实际请求预算约束。没有“每章最多5—6块”的硬限制，依阅读模块实际边界和token量决定。原文、补充和程序失败不静默裁剪。输出nonempty和调用成功均不代表语义完整。

## 5. Prompt与provider

模板在`model/prompts/text/`，注册在`model/text_tasks.py`。模板版本separated-material-provider-2，本轮不改正文。A直接目录、B/C原句要求；B目录显式保留列项和评价维度。

`TextMaterial`分body、context（位置）、supplement（来源补充）、feedback，所有资料位于任务说明之后的`---`后。OpenAI保留system角色与user任务/资料；train是一份完整query，不加裸system/user文字。runner规划和executor使用同一provider渲染；train input.md等于实际query，OpenAI审计用角色标题展示。旧P多角色train序列化兼容保留。

精确整答“无”标记no_content，空答案/失败不等于无内容；原始回复保留。E00仍为保留/排除/待核。已有目录后继突然返回无则保留草稿及问题。

## 6. 执行、恢复和产物

ModelExecutor复用逻辑并发、总调用额度、预算、指纹、逐任务采用及审计。train_batch.py复用query_list微批和共享票据；任务独立采用、乱序按ID、失败隔离，未知提交不盲目重提。text train优先，示例温度0、thinking关闭；不据此承诺准确性。

应用0.15.2，flow collaborative-directory-5，规则complete-section-rules-2，模板separated-material-provider-2。需新run；旧版本仅只读status/export，不能跨新版本恢复。

```text
run/stages/
  section_index.json             章节身份/父子关系/元素数
  rule_candidates.json           章节范围内的定位线索，不是正文筛选结果
  local_screening.json           宽泛位置的局部判断和待核
  selected_ranges.json           完整候选章节及元素来源
  organization_plan_*.json       每个完整块的长度/触发原因/分块预算
  organized/A.md B.md C.md       模型整理产物
```

原始来源和采用权威仍为artifacts/SQLite，stages仅可读投影。stop_after=candidates/organized/directory依次控制终点；candidates在真实运行中仍会调用E01和必要E00。

## 7. 验证与诊断

- `tests/integration/test_complete_section_blocks.py`：完整评分项、无关键词子项、E01无需二次关键词、宽泛局部定位、同类父子去重、多类用途、同名章节身份、局部判断隔离、独立小节不重拼、S24真实表格及切分覆盖。
- `test_collaborative_extraction.py`：主流程、锚定、产物衔接、空内容、恢复和导出；弱线索测试在合同等宽泛位置，明确采购需求不再强制E00。
- `test_long_input.py`：感知阈值、长表、源序、失败分册和有限整合。
- `test_text_prompt_revision.py`：两种provider边界、实际HTTP模拟query/预算/input.md一致与原文保真。
- `scripts/evaluate_section_blocks.py`：复用24份阅读产物离线输出完整候选和N01输入，逐元素验证源文偏移覆盖和实际body一致，不运行模型。
- `scripts/evaluate_positioned.py/evaluate_location.py/evaluate_reading_location.py/evaluate_long_inputs.py`保留历史用途；不能把旧碎片规划结果当成本版本候选正文完整性的证据。

当前离线回放的E01关闭、E00待核保留；只验证已选块和输入覆盖，不证明定位召回、相关性或模型整理质量。既有reading状态与诊断继续保留；章节识别不完整会限制候选完整性。外部附件、图片和嵌入文件未自动补读。

0.15.1仅将独立N02-B分册改为有界并发；E01分页仍串行，未变更文件间调度、超时或模型接口配置。新增test_parallel_evaluation_drafts.py验证乱序完成仍源序融合、请求限额、失败及无内容隔离。当前代码版本变化需新run。

0.15.1验证：完整离线回归410项通过（19.01秒），新增3个N02-B并发集成用例，本轮真实模型调用0次。

批处理入口：scripts/run_reading_batch.py消费已有reading，独立文件进程和run，冻结src/prompt，每文件executor维持请求上限；默认5×4=20，HTTP600/train任务3600/轮询7200秒。progress.md和batch.json区分原阅读状态、生成状态及最终目录存在性。tests/integration/test_reading_batch.py覆盖容量、冻结代码CLI模拟、文件调度及失败隔离。真实批次入口记录在PROJECT_CONTEXT，避免重复启动。


## train窗口参数修正（0.15.2）

用户确认train的max_tokens表示总模型窗口。`model/train_client.py::wire_request`发送`config.service_context_tokens`，单条/微批均经过这里；不能发送逻辑请求中的生成预留`request.max_tokens`。`config.max_tokens`继续用于TokenBudget的本地生成预留，以及OpenAI请求的最大输出额度。train未单独传输出上限，不将本地预留视为服务强制输出限制。

`cli.doctor`输出train_wire_max_tokens、含义total_model_window及local_generation_reserve_tokens。request.json保留逻辑参数，实际提交以request.http.json和train_batches下train.submit.request.json为准。executor指纹1.9.1及代码0.15.2要求新run，旧批次快照与审计原样保留。20K/40K窗口回归分别验证20480/40000，逻辑窗口16000和预留4096故意设不同值，以防混用；OpenAI仍传8192输出上限。本次不改变示例服务窗口20480，也不调用真实模型。

0.15.2验证：完整离线415项通过（29.61秒），单条/微批20480与40000服务窗口均经模拟HTTP验证；doctor本地检查通过，真实模型请求0次。

服务接口字段、max_tokens语义、三层超时、并发、提交与轮询示例见[26号服务调用说明](26_模型服务调用说明.md)。

提示设计维护入口：docs/27_Prompt设计原则与示例.md；这里只描述当前代码职责，不从旧v0.1字段模板反推当前E/N业务契约。
