# 代码导引：模块、文件与运行流转

核对日期：2026-11-09；当前版本0.13.0。默认工作流为规则与模型协同提取。设计见[25号计划](25_规则与模型协同提取实现计划.md)，环境、实验和维护约定见[PROJECT_CONTEXT](../PROJECT_CONTEXT.md)。新prompt仍是待用户参与定稿的占位版本；本轮不宣称真实模型质量验收。

## 1. 最短阅读路径

1. `src/bid_outline/cli.py::execute`：配置、输入、run锁、运行终点及导出。
2. `document/source_index.py::SourceIndex`：章节位置和原文局部单元。
3. `document/location_rules.py`、`document/locate.py::position_hits`：先位置后线索。
4. `workflow/extraction.py::Extraction`：目录辅助、局部判断、完整选区、多点锚定、类别汇总。
5. `workflow/collaborative_runner.py::CollaborativeRunner.run`：当前总编排。
6. `model/text_tasks.py`与`model/prompts/text/`：任务注册与占位提示。
7. `model/executor.py`、`model/train_client.py`、`model/train_batch.py`：逐任务采用与共享train批次。
8. `export/markdown.py::export_result`：导出ABC文本或目录。

下文源码路径以`src/bid_outline/`为基准。

## 2. 当前流程

```text
cli.execute → read_document → CollaborativeRunner.run
  → SourceIndex：章节/原文索引
  → 章节规则 + E01模型目录辅助
  → position_hits：只在所选位置匹配线索和★☆▲△■
  → E00：宽泛表达或符号的局部上下文判断
  → 完整选区 → 分块
      direct：N01-A/B/C判断并整理
      anchors：E02起止句+中间锚点 → 固定Python回取原文
               失败有限澄清，再显式回退N01
  → 按类别原文顺序拼接 → E03-A/B/C统一整理
  → N02-A/N02-B → N03融合 → N04按C补充 → N05润色
```

A为技术标格式/编制，B为技术或服务评审，C为技术约束、实质性响应、负偏离、必需指标、明确内容范围及相关材料。符号本身可定位，含义由本处上下文判断。当前不执行目录语义检查、自动修正或独立扩展建议。

## 3. 位置与范围模块

| 文件/接口 | 职责 |
|---|---|
| `document/readers.py::read_document` | 消费version=2 corrected阅读产物，回取表格、保留来源及诊断；不重写上游 |
| `source_index.py::SourceIndex` | 由reader章节路径建立位置索引；文字按段落/展开表格行文建立局部单元 |
| `SourceIndex.context` | 只取同章节相邻局部单元，不跨章带入整表 |
| `SourceIndex.full_range` | 回取完整reader段落；表格优先原始行匹配，不能唯一对应时按展开文字的编号关系扩展 |
| `SourceIndex.document_for` | 生成唯一切片身份，保存原element和projection_source_range；投影内偏移与原文偏移分开 |
| `location_rules.py::POSITIONS` | A格式/须知/附件，B评标/评审/评分，C技术/项目/采购/服务需求/须知/合同/附件等位置 |
| `location_rules.py::LOCAL/WEAK` | 范围内内容线索；符号及泛列举表达需局部判断；保留换行避免跨段拼接 |
| `locate.py::position_hits` | 在规则和目录模型合并的章节范围内检索；技术编制具体栏目保留完整内容，不要求每一项重复关键词 |
| `document/anchors.py::fields/match_anchors` | 确定性解析最少定位字段，按原文归一化映射找起止及有序中间锚点；不执行模型代码、不默认取第一处 |
| `document/token_chunks.py`、`chunks.py` | 复用完整预算切分、列表和表格保护及负责范围拆分 |
| `document/text_chunks.py` | 超长单格的展开文本投影，表/cell身份只作上下文，不能冒充精确单元格位置 |

`locate.py::locate`仍保留为0.12历史基线，`organization.py::route_organization`保留待定的0.12.1路由。当前默认流程不调用它们；不要把旧全文匹配或A/B压C逻辑加回生产链路。

目录辅助只接受真实章节标题/路径，重复或不存在时一次澄清，仍失败保留未处理范围。缺章节索引不退回全文盲匹配。当前未实现自动展开外部附件、对全文扫描图片或解析嵌入压缩包。

E00输出首行保留/排除/待核，其余可简短说明。空答、失败、非预期结论不作排除。同区其他命中不随一处排除消失。超长局部单元给模型显式有限节选；若节选被判排除，不能授权丢弃整份未发送内容，保留待核。后续可进一步完善局部单元边界。

## 4. 业务编排方法

| 方法 | 职责 |
|---|---|
| `CollaborativeRunner.run` | 恢复校验、协同提取、按终点停止、下游目录生成、采用run_result |
| `CollaborativeRunner.organize_call` | direct/anchors两条对照路径，锚定回退可追溯 |
| `text_runner.py::organize` | 当前复用的分块实现，独立块并发，按规划顺序返回；不按响应完成顺序拼接 |
| `Extraction.directory` | 分页目录E01，验证标题，合并候选位置 |
| `Extraction.locate` | position_hits、局部判断、合并完整选区，保留模型决策依赖 |
| `Extraction.anchors` | E02定位、验证、一次修正、原文回取或显式回退 |
| `Extraction.summarize` | 完整文本记录按预算分组汇总，有限层数；失败或不能继续归并保留分册及未完成标记 |
| `text_runner.py::call/attempt` | 统一文本任务身份、执行器、可读投影及失败记录；为新旧文本runner复用 |

汇总不允许按固定字符截断原文。单条过大、合并不收敛或到层数上限时明确不完整，保留完整分册；不会将失败组静默丢弃。来源精度为范围和阶段级，没有造逐标题要求归属。

## 5. 任务与prompt

任务注册在`model/text_tasks.py`，文件在`model/prompts/text/`：

| 任务 | 用途 |
|---|---|
| E01 | 根据目录辅助选择候选位置 |
| E00 | 局部宽泛线索判断 |
| E02 | 起始句、结束句、中间锚点定位 |
| N01-A/B/C | 完整块的判断与对应内容整理 |
| E03-A/B/C | 同类块结果统一整理内容和格式 |
| N02-A/B | 根据统一A/B文本生成目录，B显式列项和评价维度 |
| N03/N04/N05 | 融合、C补充、最终润色 |

业务文本采用text-v1，不走旧字段/引用解析；E00及定位任务仅解析必要的短控制信息。模型不承担程序ID和来源偏移。`model/task_specs.py`按任务转交渲染。每次独立请求携带完整资料。新prompt尚待用户参与定稿，不能将机械测试当真实小模型能力证明。

## 6. 执行与train批量

`ModelExecutor`继续统一预算、全局逻辑任务信号量、调用额度、审计、指纹、恢复和采用。max_run_calls统计逻辑尝试，不能当HTTP批次数。train批次能力新增于`model/train_batch.py`：

- `train_batch_size>1`启用微批合并，实际条数不超过max_in_flight，短等待合并已就绪任务。
- 每条query仍占逻辑在途名额并独立预算，`train_max_batches`另限HTTP批次。
- `train_batches/batch-*/`保存批次成员、原始请求/响应、共享ticket及终态；members/按任务指纹索引共享批次。
- 各calls目录通过train.batch.json及train.ticket.json关联共享票据；返回结果按query_id关联，乱序不影响采用。
- 空答、缺ID或失败不采用为成功；其他成功条目可独立采用。提交不确定不盲目重提；共享收据可恢复本地ticket写入前的中断。
- 恢复同一票据时共享轮询；取消一个任务不主动取消服务作业，关闭时保留可恢复证据。
- 不自动重提失败成员或终态无正文。使用已有票据恢复结果；显式变更输入/新运行须另保留失败历史。旧P任务继续单任务train路径，文本E/N任务才启用合批，避免改变旧格式重试语义。

批量默认AppConfig值1；新示例train.text.example.json设2，与max_in_flight=2匹配。尚未进行新批量路径真实服务吞吐/质量调优。阅读模块独立train运行时不是本执行器，旧阅读诊断脚本默认OpenAI的行为属于历史实测；本轮直接复用reading产物。

## 7. 运行终点、恢复与产物

```bash
python -m bid_outline run --reader reading --input /path/reading \
 --config configs/train.text.example.json --run /path/new-run --stop-after organized
python -m bid_outline resume --run /path/new-run --stop-after directory
```

`stop_after=candidates/organized/directory`分别停在完整候选、ABC汇总或目录。anchors/direct可用配置或`--extraction-mode`选择。恢复允许只向后推进终点；终点不影响任务指纹，所以继续目录不重跑已采用提取。配置其他字段、代码、规则版本、prompt或flow变化要求新run。旧run不可恢复到新流程，只读status/export仍支持。

```text
run/
  manifest.json / runtime.json / run.sqlite3 / run.lock
  artifacts/                         权威不可变产物及parents
  calls.md / calls/                  逐任务请求、结果、采用审计
  train_batches/                     共享train批次审计与成员索引
  stages/
    section_index.json / rule_candidates.json
    local_screening.json / selected_ranges.json
    text_<task_hash>.md               逐任务可读投影
    anchor_matches_*.json             锚点原文匹配与偏移
    organized/A.md B.md C.md          按类整理文本，失败时保留完整分册
    final.md
  exports/
    A.md B.md C.md                    整理终点及目录结果可导出
    backbone.md                      只有目录终点输出
    unresolved.md / provenance.json
```

stages是可重建视图，采用权威在SQLite/artifacts。completed仅表示当前终点执行无已记录故障，不代表内容完整、判断正确或目录有效。semantic_quality始终not_evaluated。provider缺finish_reason时默认stop仍是已知风险。

## 8. 测试和诊断

- `tests/integration/test_collaborative_extraction.py`：当前入口、位置限定、符号、局部取舍、标题验证、锚点、类别汇总、终点推进和来源切片。
- `tests/unit/test_train_batch.py`：真实HTTP载荷模拟，批量乱序、失败隔离、票据恢复、未知提交、共享收据、取消恢复等。
- `tests/integration/test_text_workflow.py`与`test_organization_routing.py`明确导入旧TextWorkflowRunner作历史对照；其中CLI用例验证新默认入口。
- 旧P任务测试与预算、provider、审计、存储回归继续保留。
- `scripts/evaluate_positioned.py`：24份已有阅读产物的纯规则选区回放；不运行目录模型或局部门控，弱命中全部只计待判断，不能宣称端到端准确率。
- `scripts/evaluate_location.py`、`evaluate_reading_location.py`：保留0.12关键词基线与真实阅读实验，结果不覆盖。

当前已知边界：规则仍可能选过宽或漏选，阅读路径可能误归属；原表和模型展开文字不一定可唯一映射行，表内范围依赖展开文本边界；锚点不存在或重复时保留问题；没有全文金标准。真实模型prompt和局部判断质量需要后续小批文本train验证。

旧`workflow/runner.py`为P流程；`workflow/text_runner.py::run`为0.12文本流程。当前仅复用后者的调用、预算、分块方法，默认不运行其旧总编排。
