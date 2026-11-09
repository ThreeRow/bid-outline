# 独立 DOCX 阅读模块代码副本

> 后续集成修正（2026-11-03）：阅读CLI新增 `--thinking auto/on/off` 和 `--heading-max-tokens`；兼容运行时将 thinking 参数传到 vLLM，并使阅读缓存身份与计数包含相关配置。默认CLI仍为auto，GPU测试脚本显式选择off。下文及 SOURCE_MANIFEST.json 的“字节一致”描述是迁移时的历史基线，不表示这些后续修改文件仍与原快照一致；详见主项目 `docs/08_GPU首次运行诊断与脚本更新.md`。

整理日期：2026-11-03。此目录是当前项目源码的阅读模块快照，保留 `outline_gen` 包名和目录关系。核心入口为 `outline_gen/document_reading/pipeline.py::read_document`，命令入口为 `outline_gen/cli/read_document.py`。

## 对外调用与历史样例

- [阅读逻辑、核心代码与输出示例](READING_LOGIC_AND_CODE_GUIDE.md)：按处理流程介绍功能，并给出优先阅读的10个代码文件。
- [对外调用改造建议](EXTERNAL_API_PLAN.md)：同步Python封装、异步HTTP接口、任务状态、产物契约和实施顺序。目前是待实施方案。
- [四份历史阅读输出样例](examples/README.md)：深圳、福建、南京机场、江苏法院，提供全文、章节、结构化JSON与诊断；均为历史incomplete结果。

## 功能与边界

输入一份 DOCX，解析段落和表格，恢复 Word 编号及结构线索，识别招标文件自身的一二级标题，校正内容归属，并导出全文、逐章阅读材料、原始表格及来源索引。标题识别和表格展开需要外部模型服务。

本入口不执行 ABC 提取、技术标目录蓝图生成或投标正文生成。标题识别是在整理招标文件自身结构，不是在设计投标目录。

## 处理流程

1. 用一次性子进程执行 Docling，返回解析块与表格后退出，释放原生解析资源。
2. 主进程读取 DOCX 内 OOXML 的段落位置、样式、大纲、编号与单元格顺序。
3. 对齐两路结果，恢复可确定的编号和段落，记录差异。
4. 提取开头目录；按目标组选择一级正文位置，再在各章内选择二级标题。
5. 按字符边界生成 sections 和统一 CorrectedDocument，确定正文、标题与表格归属。
6. 保存表格处理前结构；对表格分片调用模型展开，失败片段保留原文及诊断。
7. 从同一校正结构导出全文、章节、标题、来源映射和校验结果。

## 代码位置

| 路径 | 作用 |
|---|---|
| `outline_gen/document_reading/` | 阅读核心，共20个 Python 文件 |
| `pipeline.py`、`config.py`、`models.py`（核心目录内） | 主编排、参数、数据模型 |
| `docling_reader.py`、`docling_worker.py`、`word_structure.py` | Docling子进程与OOXML原生结构 |
| `alignment.py` | 块对齐与编号恢复 |
| `heading_candidates.py`、`heading_recognition.py`、`layered_headings.py`、`heading_inputs.py` | 目录与一二级标题识别 |
| `sections.py`、`structure.py` | 字符范围、内容分区及归属 |
| `table_render.py`、`table_extraction.py` | 表格视图、分片和模型展开 |
| `render.py`、`export.py`、`validation.py` | 导出与机械一致性检查 |
| `model_calls.py` | 模型请求、契约校验、有限重试、缓存与证据 |
| `outline_gen/cli/read_document.py` | 独立命令入口 |
| `outline_gen/prompts/document_*_sys.md` | 4份默认提示词及3份非默认二级识别实验模板 |
| `outline_gen/runtime/`、`io/`、`domain/`等 | 保守收集的共享依赖 |
| `SOURCE_MANIFEST.json` | 原提交、逐文件来源与副本SHA256、差异说明 |
| `requirements.txt` | 阅读入口直接依赖，取自项目锁定版本 |

本副本共47个 Python 文件：20个阅读核心、1个阅读CLI、26个共享依赖及包初始化文件；另有7份阅读提示词。未包含旧生成流水线、HTTP服务、ABC、融合生成或预处理模块。保留 API、Train 和可选适配器的原实现。

为断开无关依赖，仅对副本做以下适配：`cli/__init__.py`取消旧生成／服务／查看入口；`runtime/__init__.py`取消目录生成结构化调用器的集中导出；`runtime/generation_recovery.py`仅保留原样的问题记录和可选缓存函数，取消目录生成阶段恢复。`config.py`清空内置API密钥默认值。20个阅读核心文件和7份提示词均与原项目字节一致。完整差异见清单。

旧宽范围副本移至同级 `document-reading-reference/` 保留；其中的打包草稿也仅供参考。日常阅读和使用以本目录为准。未删除文件，原项目源码未修改。

## 使用方式

建议使用原项目已配置的 `bid-outline-qwen-runtime-cu128` 环境（Python 3.11），并先进入本目录，保证导入本副本。此处仅提供命令，没有在整理时执行。

```bash
conda activate bid-outline-qwen-runtime-cu128
cd /absolute/path/to/document-reading
python -m outline_gen.cli.read_document --help

python -m outline_gen.cli.read_document \
  --input /absolute/path/input.docx \
  --output-dir /absolute/path/new-reading-output \
  --provider api --base-url https://your-model-service.example/v1 \
  --model YOUR_MODEL --tokenizer Qwen/Qwen3.5-27B \
  --heading-workers 4 --workers 2 --table-format md
```

API密钥使用 `OUTLINE_GEN_API_KEY` 环境变量配置。也支持 `--provider train`，需使用自己的 `--base-url`、`--model` 及相应凭据。副本中的 `outline_gen/config.py` 已清空原有内置API密钥默认值；其余复制源码保持字节一致，详见清单。原项目文件未修改。

新环境可按 `requirements.txt` 安装直接依赖；此精简安装组合未在新环境验证。可选 LangChain 适配器有额外依赖，阅读无需部署原项目的本地模型推理服务或CUDA全套组件。首次运行 tokenizer 可能按HF机制下载分词资源，Docling也可能需要其解析资源；外部模型服务需另行提供。

### 当前参数

- `--heading-workers` 默认4，控制标题任务并发；`--workers` 默认2，控制表格任务并发，不是全局多文件并发。
- `--table-format` 可选 md/html/text；`--table-chunk-tokens` 默认6000。
- 标题上下文采用 `--heading-context-tokens`（256）与 `--secondary-context-tokens`（96）；目录预算 `--toc-max-tokens` 默认11000。
- 请求输入预算默认12000 token，默认输出上限8192 token，默认温度0；完整参数以本副本CLI为准。
- 默认CPU Fast Tokenizer为 `Qwen/Qwen3.5-27B`，可用 `--tokenizer` 或 `OUTLINE_GEN_TOKENIZER` 指定本地目录／HF仓库；不会因此下载模型权重。
- `--resume` 只用于同输入及匹配请求身份的恢复，缓存回复需重新校验；不要把旧版本输出无条件当作可恢复状态。
- 旧 `--secondary-context-chars` 和 `--attempts` 参数已移除。重试由公共 RetryPolicy 控制。

## 主要输出与下游读取

| 输出 | 内容 |
|---|---|
| `document.json` | version=2文档、统一corrected结构、原块、原表格和展开结果 |
| `structure/` | 表格模型调用前的结构、阅读视图及校验 |
| `reading.md`、`headings.md` | 最终全文与一二级标题导航 |
| `chapters/` | 首章前内容、各一级章及章节索引 |
| `source_map.json` | 片段与解析原块的对应 |
| `parsing/`、`headings/`、`tables/`、`model_calls/` | 解析证据、识别依据、表格视图与逐次模型请求回复 |
| `validation.json`、`manifest.json`、`model_call_metrics.json` | 诊断、执行状态、请求指标 |

下游应消费 `document.json.corrected`。表格片段的 `text` 不能代替整表，必须按 `table_id` 回取表格；需要原文单元格时使用 `source_tables`，不能把模型展开文本当作原文。字符范围基于解析／对齐后的文字，不是DOCX字节偏移。表格展开尚无逐字来源映射。

## 状态与已知限制

`completed` 表示当前程序诊断无待核项，`incomplete` 表示已有导出但仍有诊断，`failed` 表示异常终止。CLI分别对 completed 返回0、incomplete返回2；未捕获异常按失败退出。这些状态不构成语义保真证明。

当前仍存在目录与正文混淆、附件／表单层级误判、二级标题膨胀、表格条目重复或错号、展开关系失真等已知风险。覆盖、范围、数字和格式检查不能证明全部语义正确。文本协议仍使用各自有限重试；公共JSON恢复能力主要服务共享调用及ModelCalls的JSON模式，不能误称阅读四类文本协议都由JSON修复。

源码整理及隔离导入的验证范围见 `VERIFICATION.md`。后续按用户要求另行选取历史阅读输出，复制到 `examples/`，并编写对外调用方案；这一步读取了历史输出，但未读取输入DOCX、调用模型、下载资源、安装依赖、启动任务或运行阅读流程。历史结果不作为本副本独立运行通过的证据。
