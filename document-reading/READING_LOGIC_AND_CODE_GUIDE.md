# 阅读模块：处理逻辑、核心代码与输出示例

本文面向接手代码和调用阅读能力的开发者。说明以本目录精简副本的当前源码为准；末尾例子来自历史阅读输出，二者分开理解。文中的代码链接均指向此副本。

## 1. 模块完成什么工作

输入是一份DOCX，输出是按章节组织的可读全文，以及便于程序消费的结构化文档、原始表格、来源映射和诊断。

核心工作是：把文档内容读出来，识别招标文件自身的一二级标题，把每段正文及表格放回正确章节，并把表格转换为连续文字。它不生成投标目录或投标正文，不提取ABC，也不判断所有内容是否正确。

图片当前作为跳过的占位标记保留，不属于图片内容识别模块。

## 2. 先读哪些代码

不必先读完副本内47个Python文件。建议第一轮按下面顺序阅读10个文件，建立主流程、数据结构和结果生成方式的完整认识。

| 顺序 | 代码 | 重点入口 | 读完应能回答的问题 |
|---|---|---|---|
| 1 | [pipeline.py](outline_gen/document_reading/pipeline.py) | `read_document()` | 输入是什么，各阶段如何衔接，何时写出哪些文件？ |
| 2 | [models.py](outline_gen/document_reading/models.py) | `Block`、`Table`、`Section`、`ContentFragment`、`CorrectedDocument` | 原块、表格、章节和内容片段如何表示？ |
| 3 | [layered_headings.py](outline_gen/document_reading/layered_headings.py) | `recognize_layered()`；再看`make_primary_groups()`、`secondary_candidates()` | 如何从候选中确定一级与二级，何时需要模型？ |
| 4 | [sections.py](outline_gen/document_reading/sections.py) | `build_sections()`、`heading_offset()` | 选中的标题如何变成有起止边界的章节？ |
| 5 | [structure.py](outline_gen/document_reading/structure.py) | `organize_document()`、`chapter_groups()` | 原文如何分区，每个片段归哪个章／节？ |
| 6 | [table_extraction.py](outline_gen/document_reading/table_extraction.py) | `extract_tables()`、`extract_one()`、`validate_table()` | 表格如何获得上下文、分片、展开和失败回退？ |
| 7 | [render.py](outline_gen/document_reading/render.py) | `render_fragments()` | 结构如何变成正文、标题和表格文字？ |
| 8 | [export.py](outline_gen/document_reading/export.py) | `export_structure()`、`export_reading()` | 全文、逐章文件和章节索引为什么能保持一致？ |
| 9 | [validation.py](outline_gen/document_reading/validation.py) | `validate_structure()`、`validate_document()` | 程序具体检查什么，completed／incomplete从哪里来？ |
| 10 | [model_calls.py](outline_gen/document_reading/model_calls.py) | `ModelCalls.ask()`、`_ask()` | 模型输入如何构造，如何校验、重试、缓存和记录回复？ |

如果只需要先看总体功能，先读1、2、5、8，再按问题补读其余文件。第一轮不需要深入共享runtime的网络适配、批量调用实现，也不需要读原项目其他模块。

### 第二轮：按问题补读

| 想深入的问题 | 补读代码 |
|---|---|
| DOCX到底怎样解析，为什么单独启动子进程？ | [docling_reader.py](outline_gen/document_reading/docling_reader.py)的`read_docling()`；[docling_worker.py](outline_gen/document_reading/docling_worker.py) |
| Word自动编号、样式、合并单元格怎样读取？ | [word_structure.py](outline_gen/document_reading/word_structure.py)的`read_word_structure()`和`Numbering` |
| Docling与Word原生结构怎样对齐？ | [alignment.py](outline_gen/document_reading/alignment.py)的`align()`、`sync_table()` |
| 目录区域、候选行从哪里来？ | [heading_candidates.py](outline_gen/document_reading/heading_candidates.py)的`find_toc()`、`split_lines()` |
| 目录条目协议和标题摘录如何组织？ | [heading_recognition.py](outline_gen/document_reading/heading_recognition.py)、[heading_inputs.py](outline_gen/document_reading/heading_inputs.py) |
| 表格如何按预算分片并保留共同父项？ | [table_render.py](outline_gen/document_reading/table_render.py)的`chunks()`、`render_table()`、`render_cells()` |
| 当前参数和命令入口是什么？ | [config.py](outline_gen/document_reading/config.py)、[read_document.py](outline_gen/cli/read_document.py)；注意CLI有自己的参数默认值 |
| 模型服务与token预算如何接入？ | [factory.py](outline_gen/runtime/factory.py)、[port.py](outline_gen/runtime/port.py)、[call_policy.py](outline_gen/runtime/call_policy.py)、[tokenization.py](outline_gen/runtime/tokenization.py) |

## 3. 按数据流理解处理逻辑

```text
DOCX
 ├─ Docling：正文块、表格、原生解析记录
 └─ OOXML：位置、样式、大纲、自动编号、单元格顺序
         ↓ 对齐与可确定内容恢复
 原块 blocks ＋ 原始表格 source_tables
         ↓ 目录发现、一级位置选择、章内二级选择
 带起止边界的 sections
         ↓ 按字符范围切片并确定归属
 CorrectedDocument（sections ＋ fragments）
         ↓ 表格分片展开，失败保留原文
 表格阅读结果 tables
         ↓ 同一结构驱动渲染与导出
 reading.md ＋ chapters/ ＋ document.json ＋ 诊断
```

### 第一步：两路读取DOCX

Docling负责提取主要内容，OOXML读取负责补充Word自身的编号、位置、样式和表格结构。两路结果都不是单独的最终答案，`alignment.py`把它们按顺序对应起来，恢复可确定的文字和编号，并保留差异证据。

Docling在一次性子进程中执行，结束后释放其原生线程和内存资源；主进程继续组织后续结构。改变进程方式是资源隔离措施，不表示解析保真问题已解决。

### 第二步：识别文档自身的一二级标题

先从开头区域寻找目录并提取目录条目，再结合默认标题形式和原生结构线索生成候选。

一级识别按“同一个目标标题对应的多个正文位置”分组，每组选择一个位置；程序随后确定一级范围。二级识别只在对应一级范围内选择直属标题，可以多选。

唯一明确的目录对应、顺序无冲突时可由程序直接匹配；其余交给模型判断。模型选择的是程序给出的候选，不负责自由编造标题。识别失败保留诊断，不以补造章名冒充成功。目前仍可能混淆目录与正文、附件层级或二级标题。

四种默认模型任务均使用文本协议：

| 任务 | 主要输入 | 要求输出 | 提示词 |
|---|---|---|---|
| 目录条目 | 截出的目录原文 | 每行“层级 原文标题”或“无” | [document_toc_sys.md](outline_gen/prompts/document_toc_sys.md) |
| 一级位置 | 同一目标的候选位置及摘录 | 一个候选ID或“无” | [document_primary_sys.md](outline_gen/prompts/document_primary_sys.md) |
| 二级位置 | 章内候选及摘录 | 选中的候选ID列表或“无” | [document_secondary_sys.md](outline_gen/prompts/document_secondary_sys.md) |
| 表格展开 | 章节背景、共同父项及待展开表格 | 连续文字 | [document_table_sys.md](outline_gen/prompts/document_table_sys.md) |

另外三份secondary实验提示词是可选替代版，不是再串联执行的三个阶段。共享JSON恢复存在于调用设施中，但不能因此把这四类文本任务描述成JSON任务。

### 第三步：确定范围、分区和归属

`sections.py`把选中标题转换为左闭右开的章／节范围。对符合条件的粘连标题，可以在块内计算字符起点；不能确定的位置不猜切。

`structure.py`按这些边界拆分原块，生成不重叠的`ContentFragment`。片段保存原块ID、字符范围、内容角色、所属一级章和最深节。一级章的范围包含其二级节，而片段在全文分区中只出现一次。

这一步得到的`CorrectedDocument`是后续共同消费依据。它表达程序此次采用的归属，不保证标题判断在语义上正确。

### 第四步：把表格变成连续阅读文字

程序从校正结构取得表格所在章节及前后正文，按token预算拆分大表，必要时把共同表头和父项一并提供给模型。

展开结果需要通过非空、输出形式、原文数字及次数、背景复制等检查；仍失败的片段会标记并保留原文单元格视图，而不是静默丢失。数字保留等检查无法证明行列关系、否定和条目对应正确，历史上仍出现过重复条目和错号。

`source_tables`保留原始单元格，`tables`保存模型展开文字和状态，不能互相替代。

### 第五步：校验并统一导出

表格调用前先校验片段覆盖、顺序、字符范围与归属，失败会终止本次流程；通过后保存`structure/`中的中间阅读视图。表格完成后再汇总诊断，并导出最终全文、各章及结构化文档。

`render.py`和`export.py`消费同一个校正结构，不各自重新推断章节。原生图片输出为跳过占位，正文中原有类似Markdown标题的符号会作转义，避免与确认标题混淆。

## 4. 最需要理解的几个数据对象

| 对象 | 意义 | 常见误解 |
|---|---|---|
| `Block` | 解析／对齐后的原内容块，可能为段落、表格或图片 | 不是一个章节，也不等于DOCX原始字节区间 |
| `Table`／`Cell` | 原始表格及单元格，包含合并跨度和嵌套信息 | 不等于模型生成的表格展开文字 |
| `Candidate` | 等待选择的标题候选 | 候选不代表已经确认 |
| `Section` | 确认标题、层级、父级和起止范围 | 节段总数不等于一级章数量 |
| `ContentFragment` | 分区后的内容片段，记录角色与章／节归属 | 表格片段text可能为空，不能因此认定表格丢失 |
| `CorrectedDocument` | 统一的sections和fragments集合 | organized只表示当前结构组织状态，不表示语义已验收 |

读取`document.json`时优先使用`corrected`；遇到表格片段，通过`table_id`回取`tables[id].text`。需要核对原始表格时再读`source_tables[id]`，不能只拼接所有`fragment.text`。

## 5. 输出文件和状态怎样看

| 输出 | 适合谁使用 |
|---|---|
| `reading.md` | 人阅读完整文件 |
| `headings.md` | 快速浏览识别出的一二级结构 |
| `chapters/index.json`、`chapters/*.md` | 按章浏览、分章处理 |
| `document.json` | 程序读取统一结构、原块、原表格和展开结果；当前version=2 |
| `source_map.json` | 追溯片段、原块及Word元素关系 |
| `validation.json` | 查看当次诊断和数量统计 |
| `manifest.json` | 内部执行阶段、配置和终态；不建议原样对外公开 |
| `structure/` | 表格展开前的结构和原表阅读视图 |
| `parsing/`、`headings/`、`tables/`、`model_calls/` | 排查具体解析或模型调用问题 |

`read_document()`同步执行，返回validation字典，完整结果主要落盘；它不直接返回整个`document.json`。调用者可通过`progress_callback`获取阶段名，不能把这些离散阶段当作精确百分比。

- `completed`：当前诊断列表为空，语义质量仍是`not_evaluated`。
- `incomplete`：流程已导出结果，但有待核诊断。
- `failed`：异常终止；可能已有中间产物，不能把它们当作完整最终输出。

表格失败片段可保留原文继续，标题问题也可能作为诊断留下；原文覆盖结构检查失败则会中断。不能把这些失败处理概括为“所有异常都继续”或“所有异常都失败”。

## 6. 历史输出示例

以下四份来自2026-10-25四例端到端记录中的reading目录；这里展示的是阅读阶段，不是ABC或目录蓝图。已复制到本目录`examples/`，无需回到原项目才能查看。

四份均为`incomplete`，语义质量为`not_evaluated`，不是精简副本刚运行得到的结果，也不作为正确答案。

| 样例 | 原块 | 一二级节段 | 原表 | 诊断 | 示例入口 |
|---|---:|---:|---:|---:|---|
| 深圳AI合规审查工具 | 903 | 21 | 22 | 5 | [预览](examples/shenzhen/PREVIEW.md) · [全文](examples/shenzhen/reading.md) · [结构](examples/shenzhen/document.json) |
| 福建考场智能巡查 | 1149 | 56 | 25 | 1 | [预览](examples/fujian/PREVIEW.md) · [全文](examples/fujian/reading.md) · [结构](examples/fujian/document.json) |
| 南京机场信创云 | 715 | 80 | 33 | 8 | [预览](examples/nanjing/PREVIEW.md) · [全文](examples/nanjing/reading.md) · [结构](examples/nanjing/document.json) |
| 江苏法院网络安全服务 | 601 | 53 | 13 | 4 | [预览](examples/jiangsu/PREVIEW.md) · [全文](examples/jiangsu/reading.md) · [结构](examples/jiangsu/document.json) |

### 示例一：标题导航

以下逐行摘自深圳历史`headings.md`中的连续片段：

```markdown
# 第四章 项目需求
## 第一节 商务条款
## 第二节 技术条款
# 第五章 投标文件格式
## 一、资格审查文件部分
## 二、商务标部分
## 三、技术标部分
## 四、价格标部分
```

这说明模块识别并展示招标文件自身的章节，保留商务等原内容，不在阅读阶段执行技术标材料筛选。

### 示例二：表格展开文字

以下为江苏法院历史`document.json.tables.T0005.text`的前两个条目，逐字摘录：

```text
序号：1
服务项：等级保护测评服务
服务需求：按省法院要求对不少于四个应用系统开展等级保护测评，并提供1份网络安全评估报告。

序号：2
服务项：密码测评服务
服务需求：按省法院要求对不少于四个应用系统进行商用密码应用安全性评估，并提供1份数据安全评估报告。
```

完整文字和其他服务项见[重点章节](examples/jiangsu/chapters/S00033.md)。这些是模型展开结果，原始单元格仍须回查source_tables。

### 示例三：表格片段与章节的关联

下面从南京机场历史`document.json.corrected.fragments`中的一个表格片段选取字段，字段值未改写；省略了其他字段：

```json
{
  "id": "F000411",
  "block_id": "B00436",
  "kind": "table",
  "role": "table",
  "text": "",
  "table_id": "T0011",
  "chapter_id": "S00052",
  "section_id": "S00053"
}
```

该片段的text为空，但表格未丢失：正文渲染时读`tables["T0011"]["text"]`，原单元格读`source_tables["T0011"]`。`chapter_id`指向“第五章 技术需求书”，`section_id`指向其中最深的当前节。

### 示例四：带诊断的结果摘要

下面是福建历史`validation.json`的字段节选，不含完整issues明细：

```json
{
  "status": "incomplete",
  "block_count": 1149,
  "section_count": 56,
  "table_count": 25,
  "image_count": 0,
  "semantic_quality": "not_evaluated"
}
```

该次记录有1条`secondary_mixed_numbering_review`诊断，可在[原诊断文件](examples/fujian/validation.json)查看。存在可读全文与结构化结果，并不意味着无待核项。

所有历史文件的来源、字节大小及SHA256见[样例来源清单](examples/SOURCE_MANIFEST.json)，样例总入口见[examples/README.md](examples/README.md)。

## 7. 后续对外调用改造应从哪里接入

在`read_document()`之外增加稳定SDK和任务接口即可，不需要改写上述核心处理流程。HTTP可使用现有阶段回调，完成后返回摘要及产物下载链接；把执行状态与阅读质量状态分开。

具体建议单独见[对外调用改造方案](EXTERNAL_API_PLAN.md)。该接口方案尚未实施，历史输出展示不代表HTTP接口已可用。
