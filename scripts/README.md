# GPU 测试脚本

在已启动 vLLM 的 GPU 节点执行。脚本使用当前 Python，不重启或修改模型服务。

```bash
conda activate bid-outline-qwen-runtime-cu128
cd /groups/g900403/home/share/zxc/ztest/mem/bid-outline

# 使用常用默认值：普通安防文档、4并发、关闭thinking、8192输出token
python scripts/run_gpu_test.py start --name 安防测试

# 指定文件及参数
python scripts/run_gpu_test.py start \
  --file 'test_inputs/historical/nanjing/【招标文件】南京机场信创云项目.docx' \
  --name 南京机场 --concurrency 4 --thinking off --max-tokens 8192
```

`start` 自动生成请求配置并后台执行，当前终端默认每5秒显示状态，到任务结束自动退出。Ctrl+C仅停止查看，后台任务继续运行。使用 `--detach` 可启动后立即返回，不需 nohup、重定向或手动设置 PYTHONPATH。

## 按目录批量测试

将本轮要测的DOCX直接放在同一目录，例如 `/path/to/tenders/`。只扫描该目录直接包含的 `.docx`/`.DOCX` 普通文件，忽略 `~$` Word锁文件和其他类型；不递归扫描子目录。文件列表在启动时固化，运行中新增文件不加入本批次。现有 `test_inputs/historical/` 下的文件在子目录中，因此不能直接把它当作平铺批量输入。

```bash
python scripts/run_gpu_test.py start \
  --input-dir /path/to/tenders \
  --name 招标文件批测 \
  --batch-size 2 \
  --concurrency 2 \
  --context-budget 32768
```

- `--batch-size`：同时处理的文件数，默认2；一份文件从阅读到生成完成/失败才释放名额。
- `--concurrency`（同 `--max-in-flight`）：每份文件模型请求的客户端在途上限，默认4；现有阅读模块及生成执行器均接收此参数。
- 上例本批次最多约4个在途模型请求（2文件×每份2请求）。这是本脚本客户端限制，不是vLLM服务端全局并发；其他程序的请求不在该范围内。现有脚本没有设置或动态修改vLLM服务端 `max_num_seqs` 等部署参数，本次也未增加该能力。
- 单文件失败不会取消其他文件；每个文件独立保存调用审计、日志、状态及结果。
- 批次完成时：任一文件故障则退出1；没有故障但存在不完整/待核查则退出2；全部完成则退出0。

默认自动轮询批次进度；`--detach` 仅后台提交。查看最近批次或指定批次：

```bash
python scripts/run_gpu_test.py status
python scripts/run_gpu_test.py status --run runs/招标文件批测_实际时间 --once
```

输出结构：

```text
招标文件批测_时间/
  overview.md                  每份文件状态及结果导航
  manifest.json                文件清单、两层并发设置、调度状态
  events.jsonl                 启动/结束/错误事件
  logs/                       批次控制器日志
  files/
    001_文件甲/                完整独立运行目录
    002_文件乙/
```

排查某份文件可使用 `logs --run 批次目录/files/001_文件甲 --stage generation --follow`。

批次调度中断后使用 `resume --run 批次目录`：接管仍在运行的子任务并继续尚未启动的文件，不重复已结束文件，也不超过文件数上限。若某个已结束子任务需要单独恢复，使用 `resume --run 批次目录/files/001_文件甲`，或新建批次重新测。批次status会读取各子任务当前状态；单独恢复子任务后原批次终态代表上次调度结果，重新resume批次可更新汇总。

## 目录命名

默认目录为 `runs/<运行名或输入文件名>_<北京时间到微秒>/`，例如 `安防测试_20261103_103000_123456`。不含随机哈希；极少见的同微秒重名使用 `_2`、`_3` 顺序后缀。可通过 `--runs-dir` 改父目录，或用 `--output` 指定一个尚不存在的完整目录。

## 常用参数

| 参数 | 默认值 / 含义 |
|---|---|
| `--input` / `--file` | 普通安防DOCX，可指定其他DOCX或已有阅读产物 |
| `--input-dir` | 批量输入目录，与--input/--file互斥；不递归，仅DOCX |
| `--batch-size` | 2，批量模式同时处理的文件数 |
| `--reader auto/docx/reading` | auto，按输入类型选择 |
| `--name` | 输入文件名 |
| `--base-url` | http://127.0.0.1:8000/v1 |
| `--model` | 从 `/models` 自动取；多个模型时需指定 |
| `--tokenizer` | 从服务报告的本地模型目录获取；也可设置 BID_OUTLINE_TOKENIZER |
| `--context-tokens` | 服务实际max_model_len；优先从 `/models` 获取 |
| `--context-budget` | 20000，取它与服务实际上限的较小值 |
| `--max-tokens` | 8192，阅读表格和生成阶段输出上限 |
| `--heading-max-tokens` | 2048，阅读标题任务输出上限 |
| `--thinking off/on/auto` | off；两阶段同时传递 enable_thinking，auto不覆盖部署默认 |
| `--max-in-flight` / `--concurrency` | 4 |
| `--timeout` | 600秒/请求 |
| `--chunk-chars` | 5000，生成阶段初步分块字符目标，另检查token预算 |
| `--reading-input-tokens` | 12000，实际会为输出和安全余量让出空间 |
| `--table-chunk-tokens` | 4000，另受阅读输入预算限制 |
| `--poll-interval` | 5秒，后台保存进度及前台查看间隔 |
| `--detach` | 仅启动，不在当前终端轮询 |
| `--reuse-calls-from` | 显式指定旧generation目录，仅复用完整HTTP请求及服务地址完全一致的成功响应；可重复指定 |

当前Qwen3.5服务报告的模型目录能被脚本自动识别，因此通常无需手动指定tokenizer。其他服务器若目录不在本机，请明确提供本地tokenizer目录。开启thinking时需要为思考及最终正文共同预留输出预算；不支持thinking扩展参数的服务使用 `--thinking auto`。密钥通过 `BID_OUTLINE_API_KEY` 传入，不写入命令或状态文件。

真实测试发现长文件及补读材料可能需要超过20K的完整上下文。在本机已确认服务max_model_len=32768时，可用 `--context-budget 32768`；脚本仍检查不得超过服务实际限制，不截断材料。局部修复复验可使用 `--reuse-calls-from runs/旧运行/generation`，新运行仍独立保存全部请求、响应及解析，meta记录旧响应路径和SHA256；runtime区分真实calls与reused_responses。失败旧响应、不一致的模型/服务/请求/生成参数不复用，也不会绕过当前预算和解析校验。

## 自动查看、日志和恢复

不指定 `--run` 时默认查看最近启动的测试，也可以显式指定完整运行目录：

```bash
python scripts/run_gpu_test.py status                      # 自动轮询至结束
python scripts/run_gpu_test.py status --once               # 一次性完整JSON
python scripts/run_gpu_test.py status --run runs/实际目录 --poll-interval 10
python scripts/run_gpu_test.py logs --stage reading --follow
python scripts/run_gpu_test.py logs --stage generation --lines 100
python scripts/run_gpu_test.py resume                      # 续跑并自动查看
```

终端会显示阶段、阅读请求数、生成调用/在途数、失败尝试和诊断数。新失败尝试提醒为WARN，因为可能仍在重试；阶段失败提醒为ERROR，带诊断/不完整单独提醒。终端可交互时错误提醒附提示音，不发送外部通知。

恢复保留历史日志，复用已完成阶段。失败阅读阶段调用阅读模块resume；生成阶段调用生成resume。不完整输出不会被当作完整成功。配置和代码变化需要新建测试，尤其旧的4096-token/默认thinking失败运行应使用新的start，而不是原配置resume。恢复仍受原有重试/总调用上限约束，不会无限重试。

## 输出布局（新运行）

```text
运行名_时间/
  overview.md                 状态、错误摘要、结果和日志导航
  manifest.json               启动参数、配置、模型/环境、当前状态与阶段
  events.jsonl                历史状态、阶段变化、失败、重试和告警
  results/                    backbone.md、extensions.md、unresolved.md、provenance.json
  logs/                       进程stdout/stderr及运行锁
  reading/                    原阅读模块产物，保持原接口
  generation/
    calls.md                  可读调用索引
    calls/
      01_screening/
        0001_P01_第四章采购需求_attempt01/
          summary.md          对象、结果摘要、采用/复用、错误、其他尝试和证据链接
          input.md            实际请求的完整提示与材料
          output.txt          模型最终正文
          request.http.json   实际HTTP请求体（不含密钥）
          response.raw.json   原始响应
          parsed.json        类型化解析结果
          validation.json    校验结果
          meta.json          完整调用元信息
      02_refinement/
      03_extraction/
      04_outline/
      05_review/
      06_extension/
    artifacts/                不可变中间产物
    run.sqlite3               生成任务及版本恢复数据库
    manifest.json             生成子流程状态快照
    runtime.json              实时调用计数
```

阶段内编号为调用顺序；相同任务的重试通过summary.md互相链接。P06补读确认归入触发阶段；无法取得章节名时使用任务功能名或明确范围编号，不编造主题。UUID保留在meta内部，不作为新调用目录名。

顶层manifest.json含request/config/service/environment/execution/stages/progress字段，统一原子更新；events.jsonl追加保留历史。overview.md和calls.md均由程序记录生成，不调用模型，不是独立状态源。overview区分最终未处理和历史失败尝试；calls显示是否采用、是否复用及实际耗时。新运行的结果统一放results/。

旧运行不迁移、不删除；status/resume继续读取旧散列状态文件，模型恢复和显式复用同时支持旧UUID平铺calls及新的阶段目录。直接使用bid_outline CLI而非本脚本时仍默认导出到运行目录exports，可用--output指定results位置。

退出码0为完成，2为带问题或不完整，1为故障。`--detach`模式返回0只表示后台启动成功，实际结果看status。SIGKILL等异常退出可能来不及写终态，status通过执行锁识别interrupted，保留日志供排查。脚本进程仍受GPU作业资源生命周期限制。

旧 `tools/run_gpu_test.py` 仅为兼容入口，推荐统一使用本目录脚本。

## 四份DOCX：OpenAI阅读＋train生成并记录时间

`run_train_batch.py` 独立于上面的GPU测试脚本。默认读取 `test_inputs/input1` 的直接子文件，不递归；忽略Word锁文件。每份先通过同一服务OpenAI接口阅读，再用train provider生成，两阶段都按`--thinking on`显式开启。阅读和生成均使用本地fast tokenizer；默认Qwen3.5目录，未确认其与当前服务模型/模板完全匹配。生成tokenizer可用`--generation-tokenizer`单独指定。默认逻辑/服务窗口均20480，`--max-tokens 8192`包含思考和答案；阅读的完整输入上限也按同一窗口预扣，默认11264。

```bash
python scripts/run_train_batch.py \
  --input-dir test_inputs/input1 \
  --output runs/input1_train_tokens_01 \
  --parallel-files 4 --concurrency 8 --thinking on
```

输出目录必须不存在。`--parallel-files`控制文件并发，`--concurrency`控制每文件的生成全局并发，并传给阅读的标题/表格worker；不代表服务端实际生成吞吐。四份进入生成时最多32个在途train作业。train配置从`configs/train.thinking.example.json`复制到本批目录，再覆盖并发、thinking、tokenizer、窗口和输出上限。`--context-tokens`设置逻辑窗口；`--service-context-tokens`记录已核实的服务上限，默认20480，前者不得超过后者。无需手改共享配置。

- `timing.json`/`timing.md`：各文件开始/结束时间（Europe/Berlin）、阅读秒数、生成秒数、单文件总耗时、批次墙钟时间，运行中按阶段更新。
- `01_文件名/reading.log`、`generation.log`：各阶段日志。
- `01_文件名/reading/`、`generation/exports/`：阅读与生成产物。

单文件总耗时从实际取得文件并发名额开始，批次耗时包含排队。耗时为单调时钟测得的墙钟时间，不是模型纯推理时间；并行文件耗时之和不是整批时长。失败文件保留退出码及耗时，其他文件继续。Ctrl+C会终止本脚本子进程并记录取消；服务端train作业可能继续。

本入口不支持整批resume；中断后已生成的单文件目录可用`bid_outline resume --run .../generation`恢复，或者新建批次重新运行。模拟验证已覆盖四文件并行、故障隔离、两个thinking开关和计时文件；本次未代用户启动真实批量调用。

## 分阶段thinking与Markdown目录实验（0.11.0）

新配置`configs/train.staged_markdown.example.json`：默认关闭thinking，P07–P18开启，
P08/P11/P12/P16使用`markdown-v1`。示例：

```bash
python scripts/run_train_batch.py \
  --input-dir test_inputs/input1 \
  --output runs/input1_staged_md_01 \
  --config configs/train.staged_markdown.example.json \
  --thinking config --reader-thinking off \
  --directory-protocol markdown-v1 \
  --parallel-files 4 --concurrency 8
```

`--thinking config`是新默认，保留逐任务策略；显式`--thinking on/off`覆盖所有生成任务。
`--outline-thinking on/off`可在全局覆盖后单独设置P07–P18；阅读使用独立`--reader-thinking`。
输出必须用新目录。保持阶段开关比较旧目录协议时，将`--directory-protocol`改为`strict`。
Markdown目录仍为新标题保留一行要求依据，由程序生成节点身份、父子关系和顺序；
原有结构保护与覆盖检查保留。详见[23号说明](../docs/23_分任务thinking与Markdown目录实验.md)。
