# 代码导引：模块、文件与运行流转

核对日期：2026-11-09；应用版本：0.12.0。当前默认入口为文本目录流程。旧P任务及旧runner保留供历史对照，不是新CLI的运行链路。环境及历史服务问题见[PROJECT_CONTEXT](../PROJECT_CONTEXT.md)，重构目标见[24号计划](24_文本目录生成流程重构计划.md)。

## 1. 最短阅读路径

1. `src/bid_outline/cli.py::execute`：输入、配置、run锁、client、runner及导出。
2. `document/readers.py::read_document`：阅读产物到Document。
3. `document/locate.py::locate`：章节、关键词定位ABC候选范围。
4. `workflow/text_runner.py::TextWorkflowRunner.run`：新的业务主流程。
5. `model/text_tasks.py`及`model/prompts/text/`：独立文本任务与占位提示。
6. `model/executor.py::_execute/_parse`：预算、通信、审计、恢复、正文采用。
7. `export/markdown.py::export_result`：从采用的结果导出文本目录。

以下源码路径以`src/bid_outline/`为基准。新流程提示词尚待用户参与定稿，未作真实模型质量验收。

## 2. 当前业务主流程

```text
cli.execute
  → read_document（DOCX先由独立document-reading进程阅读）
  → workflow.WorkflowRunner（别名指向TextWorkflowRunner）
  → locate：程序扫描章节路径及正文，得到ABC候选范围
  → organize：候选范围切块，N01-A/B/C整理为普通文本
  → N02-A：A生成目录；N02-B：B展开列项及评价维度
  → N03：逐批融合A/B目录草稿
  → N04：按C逐批补充；只有C时也可以依据C起草
  → N05：最后单独润色输出
  → adopted run_result → export_result
```

A/B无候选时跳过；无C时跳过补充；均未生成草稿则记录问题，不调用最终润色。新路径不运行P01—P19、不进行模型归属、目录语义检查、自动修正或扩展建议。

完整性、思路清晰度、先进性等评价维度需要显式列为B目录的下级，融合和润色继续保留。程序不解析这些业务内容来伪造语义验收。

## 3. 新业务模块导航

| 文件与方法 | 职责 |
|---|---|
| `workflow/__init__.py` | 默认WorkflowRunner导出指向新TextWorkflowRunner |
| `workflow/text_runner.py::run` | 校验恢复身份、定位、整理、生成、融合、补充、润色和结果状态 |
| `text_runner.py::organize` | 规划候选范围分块，保留章节及辅助条件，整理文本；已知预算/截断可拆原负责范围 |
| `text_runner.py::generation_batches` | 整理文本按完整段落/行及预算分批；不截掉输入 |
| `text_runner.py::call` | 根据任务、完整材料及依赖构造身份，调用执行器，保存采用文本和阶段投影 |
| `text_runner.py::attempt` | 阶段调用失败记录未处理范围，后续保留已有草稿 |
| `document/locate.py::locate` | 根据RULES及SUBMISSION定位候选，小范围取邻接单元，同类重叠范围合并 |
| `locate.py::submission_table` | C类需求表保留命中行、邻接行、表头及继承标签，记录候选行和cell来源 |
| `model/text_tasks.py` | N任务名称、阶段、模板版本及简短文本消息渲染 |
| `model/task_specs.py` | 统一任务注册；N任务转交text_tasks渲染，旧P任务继续旧渲染 |
| `model/executor.py::_parse` | text-v1只检查非空正文和stop；不调用旧字段/引用解析 |
| `model/call_view.py` | N任务显式阶段映射，逐调用导航及文本长度摘要 |
| `export/markdown.py` | 新结果按directory_text或draft_text导出；标注最终文本/未完成草稿 |

定位命中仅为候选，不是ABC真值。章名缺失时依正文线索及邻接范围；未实现模型补读或自动展开外部附件。超长单格在`document/text_chunks.py::expandable_tables`中转为展开文本切片，保留表头及原表身份；`text_slice_basis=expanded_table_text`明确偏移仅针对展开文字，cell集合仅作原表上下文，不冒充精确单元格切片。仍无法安全处理的单元明确阻断。

## 4. 文本任务与prompt入口

| 任务 | 模板位置（`model/prompts/text/`） | 输出 |
|---|---|---|
| N01-A | `N01-A.txt` | 格式编制内容整理 |
| N01-B | `N01-B.txt` | 评审项目、列项、评价维度及具体细节 |
| N01-C | `N01-C.txt` | 补充技术材料要求及疑点 |
| N02-A | `N02-A.txt` | A目录草稿 |
| N02-B | `N02-B.txt` | B目录草稿，显式列出内容及评价维度 |
| N03 | `N03.txt` | 完整融合草稿 |
| N04 | `N04.txt` | 按C补充后的完整草稿 |
| N05 | `N05.txt` | 润色后的目录文本 |

这些是待用户共同设计的占位提示。修改prompt前先读该目录README。中间结果允许列表、段落和分析，无JSON、记录标记、节点ID、父ID或REQ映射要求。末步只要求可读目录，没有逐节点协议解析；非空文本可以是解释性回答，运行completed不证明目录存在或质量合格。

文本流程不使用旧`S00`，其短system说明直接在`text_tasks.render`中。旧common文件修改不会影响新流程。新任务使用`text-v1`协议，不受strict/compact/markdown旧配置选择影响。

## 5. 复用的基础功能

| 模块 | 职责与边界 |
|---|---|
| `domain/__init__.py` | Document、DocumentElement、SourceRef、Chunk继续复用；旧RequirementGroup和OutlineVersion不作为新流程必需对象 |
| `document/readers.py` | 适配version=2 corrected阅读产物、原始表格及诊断，保留来源 |
| `document/token_chunks.py` | 候选范围内优先章节，超限再拆语义单元；完整请求复核 |
| `document/chunks.py` | 负责范围拆分、表格行组、来源偏移 |
| `document/text_chunks.py` | 新流程的超长表格文本切片投影，原阅读对象不变 |
| `document/paragraph_context.py` | 列表和上下文条件保护 |
| `workflow/common.py` | 稳定身份、时间、有限并发 |
| `model/executor.py` | task锁、指纹、预算、全局信号量、调用上限、原始证据、恢复与采用 |
| `model/budget.py` | 本地fast tokenizer、完整提示及thinking共享额度检查；不下载资源 |
| `model/client.py` | OpenAI客户端、FakeModelClient及provider工厂 |
| `model/train_client.py` | train提交、持久票据、轮询及响应；未知提交不盲重提 |
| `model/audit_io.py` | 异步审计I/O |
| `storage/artifact_store.py` | 内容寻址不可变产物，读取校验哈希 |
| `storage/repository.py` | SQLite任务采用、运行状态；旧目录版本接口保留 |
| `model/replay.py` | OpenAI显式精确请求复用；train仍禁止跨run复用 |

来源依赖链：document → located_ranges → located_chunk → model_result → 后续model_result → run_result。来源可追到范围及阶段，没有新造逐标题要求归属。

## 6. 配置、运行及恢复

- `config.py`增加`text_batch_tokens=3000`，限制单次整理和A/B生成的材料量；该值尚未真实调优。
- 上下文默认20480，max_tokens默认8192，安全余量1024。规划与执行使用同一prompt渲染和预算。
- thinking支持N01-A/B/C、N02-A/B、N03、N04、N05。示例`configs/train.text.example.json`统一关闭thinking，待实测。
- `scripts/run_train_batch.py --outline-thinking`同时覆盖新目录任务；旧directory-protocol参数只影响旧P任务。
- 配置、代码版本和`flow_version=text-directory-1`须匹配才可resume，旧run不能转为新流程。恢复会重新确定性规划并复用已采用文本，已保存有效响应及train票据仍由executor恢复。
- 融合、C补充、润色超预算时记录失败，保存完整上一版草稿；不部分截取后冒称完成。空响应失败，不当成无要求。

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
 /home/u2024010436/miniforge3/envs/bid-outline-qwen-runtime-cu128/bin/python \
 -m bid_outline run --reader reading --input /path/reading \
 --config configs/train.text.example.json --run /path/new-run
```

仅在用户授权实测时执行上述真实调用。导出和status只读采用产物，不生成内容。

## 7. 运行产物和排查

```text
run/
  manifest.json / runtime.json / run.sqlite3 / run.lock
  artifacts/a-*.json
  calls.md
  calls/01_organization|02_drafts|03_merge|04_supplement|05_final/...
    request.json / request.http.json / input.md
    response.raw.json / response.model.json / output.txt
    parsed.json / validation.json / meta.json / summary.md
    train.ticket.json / train.submit.* / train.poll-* / train.terminal.json
  stages/
    located_ranges.json
    text_<task_hash>.md          已采用文本的可再生成投影
    final.md                    N05已采用文本
  exports/
    backbone.md / unresolved.md / provenance.json
```

`stages`是投影，SQLite采用指针和artifacts是权威。新结果包含directory_text、draft_text、directory_state、located_ranges、stages及问题，不包含伪造的旧outline.nodes或assignments。失败调用可能没有全套文件。

| 问题 | 先查 |
|---|---|
| 候选不对或遗漏 | located_ranges的reason、来源element/cell和对应RULES/SUBMISSION |
| ABC内容不干净 | N01输入、output及对应原文范围；占位prompt |
| 目录过粗或评价维度丢失 | N01-B、N02-B、N03、N05之间的文本与依赖 |
| 无最终结果 | unprocessed_ranges、N05是否发出、完整预算及上一版draft_text |
| 服务成功却无内容 | train.terminal.json实际answer及finish_reason；缺finish_reason仍默认stop是已知provider风险 |
| 恢复未命中 | flow/code/config、fingerprint及任务采用指针 |

completed仅表示文本调用链执行成功，不表示已经完整识别ABC、生成有效目录或通过语义质量验收。语义质量始终not_evaluated。

## 8. 测试与历史模块

`tests/integration/test_text_workflow.py`覆盖新入口、定位、表格候选缩小、普通文本衔接、缺类、C独立生成、失败保留草稿、已知截断缩块、恢复及只读导出。其固定响应验证程序行为，不能作为真实模型能力证据。

共享执行、provider、token、存储测试继续适用。`test_contracts_text`区分新文本渲染与旧转义协议；旧记录边界检查只针对P模板。`test_workflow_boundaries`明确导入旧runner以保留旧边界回归。

旧业务实现仍在`workflow/runner.py`，与`outline.py`、`source_lists.py`、`context.py`、`history_view.py`、`repair_scope.py`及P01—P19模板组成历史严格流程。`parsers.py`、`references.py`、`compact_protocol.py`、`markdown_directory.py`仍供旧任务测试及对照使用。不要将这些模块的旧业务链路当成当前CLI流程；不要为维护新文本流程重新引入其模型字段约束。

修改文件职责或流程时同步本导引；修改外部接口、预算、thinking、恢复和产物时同步公共信息、样例配置和相关测试。历史报告保留原有版本边界。
