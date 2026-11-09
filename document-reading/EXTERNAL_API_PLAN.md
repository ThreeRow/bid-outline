# 阅读模块对外调用改造建议

本文为待实施方案。现有同步 Python 函数和 CLI 已存在，下面的稳定封装、HTTP 接口和任务管理尚未实现；本次不启动服务、不修改阅读算法。

## 推荐提供的两种方式

| 使用方 | 提供方式 | 返回内容 |
|---|---|---|
| 同一个 Python 环境内的其他程序 | 同步 Python SDK | 完成状态、诊断摘要、产物索引 |
| 其他语言、机器或系统 | 异步 HTTP API | 先返回任务编号，再查询状态和下载结果 |

两者共用同一个应用层封装，最终调用现有 `read_document()`。HTTP 服务不要调用原项目的技术标生成服务，也不要通过解析 CLI 控制台文字获取结果。

当前入口在 `outline_gen/document_reading/pipeline.py`：

```python
read_document(input_path, output_dir, *, runtime, config=None,
              docling_reader=read_docling, progress_callback=None)
```

它接受服务端本地 DOCX 路径、输出目录、模型运行时和 `ReadingConfig`，同步执行并返回 validation 字典，主要结果落盘。`progress_callback(stage)` 可报告 parsing、alignment、headings、structure、tables、finalize 阶段；当前没有可靠的百分比进度。`docling_reader` 是内部测试注入点，不对外开放。

## 第一层：可安装包与稳定 Python 接口

新增包元数据 `pyproject.toml`，把当前 `outline_gen` 包和 `prompts/*.md` 一起打入 wheel；确保安装后 Docling 子进程仍能导入 `outline_gen.document_reading.docling_worker`。当前副本保留旧包名，同一 Python 环境不宜同时安装原项目和副本；若需要共存，再统一更名并更新导入及子进程模块路径。

新增 `outline_gen/reading_api.py`，封装以下职责：

1. 初始化模型运行时和 tokenizer，参数由调用方或部署配置提供。
2. 校验输入、创建独立输出目录、调用原阅读函数。
3. 把 validation 和文件清单转换成稳定 `ReadingResult`，不把整个文档塞入每次状态响应。
4. 统一返回异常类别及原始诊断，保留历史内容不完整状态。

建议 Python 使用形式如下；这是拟议接口，不是当前可运行示例：

```python
from outline_gen.reading_api import ReadingClient, ModelSettings

client = ReadingClient(ModelSettings(
    provider="api", model="YOUR_MODEL", base_url="https://your-service/v1",
    api_key="...", tokenizer="Qwen/Qwen3.5-27B",
))
result = client.read(
    input_path="/data/input.docx", output_dir="/data/new-output",
    table_format="md", on_progress=print,
)
print(result.execution_status, result.reading_status, result.artifacts)
```

SDK先保持同步即可，需要异步调度的系统自行提交任务。原有 `read_document()` 接口保留，阅读核心不需要为了HTTP改成异步函数。

## 第二层：异步 HTTP 接口

阅读包含本地解析和多轮远端模型请求，提交接口应快速入队并返回 HTTP 202。文件由调用方上传，服务自己分配任务目录；外部调用方无需知道服务端路径。

| 方法与路径（拟议） | 作用 |
|---|---|
| `POST /v1/reading-jobs` | multipart上传 DOCX，附少量阅读选项，创建任务并返回202 |
| `GET /v1/reading-jobs/{job_id}` | 执行状态、阶段、阅读状态、诊断数、产物链接 |
| `GET /v1/reading-jobs/{job_id}/artifacts` | 可获取文件的逻辑名称、类型、大小和下载URL |
| `GET /v1/reading-jobs/{job_id}/artifacts/{artifact_id}` | 下载指定产物 |
| `GET /health` | 服务及worker可用状态；不触发模型推理 |

初版无需WebSocket、取消、批量上传、在线预览和回调通知。每个文件一个任务，调用方轮询即可。后续若要取消任务，需要新增显式取消机制，不能把删除任务记录当作停止推理。

提交请求示意：

```bash
curl -X POST https://reading.example/v1/reading-jobs \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -F 'file=@input.docx' \
  -F 'options={"table_format":"md"}'
```

拟议的受理响应：

```json
{
  "job_id": "rd_123",
  "execution_status": "queued",
  "status_url": "/v1/reading-jobs/rd_123"
}
```

拟议的结束查询响应，以下数量仅作协议说明，不对应历史样例：

```json
{
  "job_id": "rd_123",
  "execution_status": "finished",
  "stage": "finished",
  "reading_status": "incomplete",
  "semantic_quality": "not_evaluated",
  "issue_count": 2,
  "document_schema_version": 2,
  "artifacts_url": "/v1/reading-jobs/rd_123/artifacts",
  "error": null
}
```

执行状态使用 queued / running / finished / failed；结束后阅读状态保持原 completed / incomplete / failed。`incomplete` 是有结果且有诊断，不能转换成“任务失败”，也不能隐藏成“全部成功”。`semantic_quality` 单独保留，`completed` 不表示语义已验收。致命错误返回可定位阶段与错误类别，详细堆栈留在任务内部证据。

## 任务运行与数据契约

新增 `reading_service/`，建议仅含 app.py、schemas.py、jobs.py、worker.py 四个文件，启动入口另设。API处理上传和查询，worker调用SDK，阅读核心仍使用现有20个文件。

- 单机首版可以使用一个服务进程、线程池worker和持久化任务登记，例如SQLite。提交受理前持久化任务及输入位置；并发任务数由服务级队列控制，单文件标题／表格并发由ReadingConfig控制。不要在async请求处理中直接执行同步阅读函数。
- Docling继续在一次性子进程运行。进程崩溃或服务重启后，持久化任务应标记中断并允许显式重试，不能永久显示running。多服务进程或多机器部署时，再替换为共享任务队列和统一任务所有权；不能让多个worker同时写同一个目录。
- 每个任务使用独立输入和输出目录。提交时按文件大小、DOCX容器及扩展名检查；接入现有鉴权体系，以任务归属控制查询与下载。下载只接受服务登记的artifact_id，不接受任意文件路径。
- 原manifest和请求日志属于内部证据，公共状态和下载清单由服务层生成，不默认暴露绝对路径、模型凭据或原始请求日志。
- 模型地址、凭据、tokenizer和调用额度由服务配置统一控制。请求初版只开放table_format等必要选项；并发和重试参数留给部署者，避免每个调用方自行放大负载。
- 重试和resume不能混为一谈。初版失败重试可生成新任务目录；需要复用时再明确输入哈希、tokenizer、代码、配置和提示词的匹配规则。当前阅读函数对输出目录的初始校验只检查输入和tokenizer身份，具体模型回复缓存另按请求指纹复验，不宜对外承诺完整的任务级同版本恢复。

## 对外提供哪些输出

默认产物清单建议开放：`reading.md`、`headings.md`、`chapters/index.json`及逐章Markdown、`document.json`、`validation.json`。`source_map.json`作为需要追溯时的可选产物。

保持现有 `document.json` version=2作为结构化下载格式，API版本 `/v1` 与文档结构版本2相互独立。状态响应只提供摘要和链接；全文与大JSON走文件下载。原始模型调用、解析日志和运行配置不进入默认公共文件清单。

必须在接口说明中保留以下含义：

- 正式归属使用 `document.json.corrected`。
- 表格片段需按table_id回取表格，不能只拼fragment.text。
- 模型展开结果在tables，原始单元格在source_tables；两者不可互换。
- source范围针对解析／对齐后的文字，不是DOCX字节位置。
- 标题、展开和机械校验仍存在质量限制，诊断与语义验收分别表达。

## 实施顺序与验收

1. 增加打包元数据和同步SDK，保留原CLI；验证安装到临时目录后能导入CLI、加载提示词、定位Docling worker，不依赖原项目源码路径。
2. 增加HTTP上传、持久化任务、worker、状态查询和产物下载；用注入的阅读函数验证受理、进度、incomplete、失败、文件归属、任务隔离和重启后的中断识别，不需要真实模型。
3. 选一份文件做真实阅读，核对API结果与原核心输出一致；再评估服务并发。只有此步完成后才能称端到端接口已验证。

本次仅产出方案和历史输出样例。第一轮建议做到单机SDK＋异步HTTP服务，不扩展ABC或目录蓝图功能。历史样例入口见 [examples/README.md](examples/README.md)。
