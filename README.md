# bid-outline

维护入口：[项目公共信息与维护约定](PROJECT_CONTEXT.md) · [代码导引：模块、文件与运行流转](docs/CODE_GUIDE.md)。新接手先读这两份；历史测试和版本报告保留各自时间边界。

程序主导的技术标骨干目录生成工作流。输入招标文件阅读产物，输出骨干目录、独立扩展建议、未解决事项与可追溯记录。业务基线见 [docs/README.md](docs/README.md)，接口设计见 [docs/06_实现设计与模块契约.md](docs/06_实现设计与模块契约.md)。

当前版本为 **0.11.0**。新增逐任务thinking开关与可选Markdown目录协议，完整回归288项通过；实验配置与运行命令见[23号说明](docs/23_分任务thinking与Markdown目录实验.md)。0.10.0引入服务窗口20480、CPU fast tokenizer与章节优先的完整请求token规划，详见[22号说明](docs/22_token窗口与章节分块.md)。此前0.9.0新增可选文本 train provider，推荐配置见 `configs/train.thinking.example.json`；train 服务由服务端选择实际模型，配置中的 `model` 仅作审计标签。提示词按独立调用重写，默认使用可读输入；0.9.0阶段代码回归 **250 项通过**。真实服务返回模型标识 `qwen38`，冻结的35个任务级对照案例格式与固定判据均通过，输入token较基线减少6.99%；真实小工作流复测26次调用、无未处理范围和开放检查发现，仍有3项清单来源待确认；这不是整份招标文件质量验收。设计及协议边界见[19号说明](docs/19_独立任务提示与可读输入.md)，真实对照、失败历史与复现入口见[20号报告](docs/20_提示词真实模型对照测试.md)。旧运行需新建目录；下文0.7.0之前的GPU结果为历史记录。

train接口与开启thinking的命令见[21号说明](docs/21_文本train_provider与thinking实测.md)。

## 环境与启动

开发与测试使用 `bid-outline-qwen-runtime-cu128`，Python 3.11。已有环境包含运行依赖和 pytest；不需要重新部署模型或安装 CUDA。

```bash
conda activate bid-outline-qwen-runtime-cu128
export PYTHONPATH="$PWD/src"
python -m bid_outline --help
python -m bid_outline doctor --config configs/local.example.json
python -m pytest tests -q
```

也可在该环境安装本包：`python -m pip install --no-deps --no-build-isolation -e .`，随后使用 `bid-outline` 命令。运行依赖兼容范围在 pyproject.toml；现有环境的完整依赖记录保留在 environment-cu128.yml / requirements-cu128.txt。真实调用必须配置本地 fast tokenizer，使用环境已有 transformers，禁止预算模块隐式下载资源。

## 输入和真实模型配置

GPU节点上的真实DOCX测试可直接使用 `python scripts/run_gpu_test.py start`。该脚本统一管理后台运行、配置生成、状态记录、日志及恢复，详见 [scripts/README.md](scripts/README.md)。

复制示例配置并填入真实 `base_url`、served model name、逻辑窗口、服务实际窗口及必需的本地 fast tokenizer 路径。示例地址/模型名不是已经确认的服务。密钥仅通过 `BID_OUTLINE_API_KEY` 或配置指定的环境变量读取。thinking 等参数放在已核实支持的 `extra_body` 中，不能假设所有 vLLM 部署都支持相同开关。

```bash
python -m bid_outline doctor --config /path/local.json --check-service
python -m bid_outline run --reader reading \
  --input /path/reading-output --config /path/local.json --run /path/new-run
python -m bid_outline status --run /path/new-run
python -m bid_outline resume --run /path/new-run
python -m bid_outline export --run /path/new-run
```

`--reader reading` 接收阅读产物目录或 version=2 document.json。消费 corrected.fragments/sections，表格回取 tables/source_tables，保留来源与诊断。历史阅读样例可用于适配验证，但不是正确答案。`--reader sample --input test_materials` 接收原OOXML测试提取物，明确记录为样例适配器。

`--reader docx --input /path/input.docx` 会显式调用迁入的 document-reading CLI，随后读取其产物。默认查找工作区 document-reading；安装后位置不同时传 `--reading-module /path/document-reading`。该上游阶段有独立模型运行时和审计，顺序完成后才开始生成，不声称与生成执行器统一审计。Docling及上游tokenizer可能需要其既有资源；已在三份真实招标文件上完成DOCX阅读与模型生成迭代验证，结果及限制见[当前测试结果报告](docs/12_当前测试结果报告.md)。原阅读核心保持不变。

`doctor` 默认不联网；只有 `--check-service` 请求 /models，且不证明上下文、thinking参数或语义质量已经验证。真实模型名为空时禁止生成请求。

## 执行与恢复

模型默认返回约束文本，内部Pydantic对象和JSON不受此限制。初筛、保真摘取、ABC按块流水，所有生成调用共享全局并发名额；关键词复查只针对未纳入候选。骨架增补和局部替换统一提交，目录最多四级。

生成预算按完整请求计算：先扣任务提示、包装、章节上下文、思考与答案共享的生成额度及安全余量，再按token组织正文。优先完整章节，其次小节、段落、列表与表格行组；调用前再次计量。P07/P08及章节任务按完整输入分批，背景与引用候选可分页，不截断义务。真实CLI必须使用本地fast tokenizer；字节估算仅用于离线模拟。无法安全拆分的必需材料仍明确记为未处理，详见[22号说明](docs/22_token窗口与章节分块.md)。

每次实际尝试保存完整请求、响应、结束原因、解析及校验结果。审计失败禁止提交成功结果。通信重试、格式重做、补读和目录修正分别有上限，并受任务/运行总调用上限约束。

恢复依赖输入、模型配置、模板渲染和解析版本；复用已采用结果，并可补交已经保存的有效响应。目录按历史版本恢复，拒绝过时覆盖。同一运行目录通过CLI文件锁限制为一个进程；程序方式使用WorkflowRunner时调用方也必须保证独占。配置/代码版本变化要求新建运行。请求超时不保证服务端没有生成，保证的是逻辑结果只采用一次。

## 输出与状态

```text
run/
  manifest.json             运行配置、身份和终态
  run.sqlite3               任务、调用索引、版本和采用关系
  runtime.json              当前执行器请求计数及UTC心跳
  artifacts/                不可覆盖且校验哈希的产物
  calls/<call_id>/           完整请求、响应、解析、校验、元信息
  exports/backbone.md        正式编号的骨干目录
  exports/extensions.md      独立扩展建议和审核状态
  exports/unresolved.md      未解决事项、未处理范围
  exports/provenance.json    要求、来源、归属、目录、检查关联
```

状态区分 completed、completed_with_issues、incomplete、failed、cancelled。CLI退出码：无问题完成0；带问题或不完整2；故障1；用户中断130。`export`只读取采用结果，不调用模型。已扫描不等于完整提取，已分配不等于覆盖；运行审查只验证已提取要求到目录，不进行全文漏提审查。semantic_quality始终标为not_evaluated，直到另有真实语义评测。

## 测试和已知边界

截至2026-11-04，最近一次自动化回归为 **203项通过**。覆盖ABC、A保留策略、来源关联、归属增补、检查/修正、补读、扩展、恢复、局部失败，以及审计故障、限流、截断、取消、预算、并发和版本冲突；新增调用记录/输出布局及批量启动、轮询、恢复等测试。自动化测试使用预设响应或本地模拟HTTP服务，不代表真实模型语义正确。测试临时产物保留，不使用递归清理。

已使用真实Docling＋OOXML阅读模块和GPU节点上的Qwen3.5-27B vLLM服务，对安防、江苏法院、深圳三份招标文件进行迭代验证。三份最终生成运行均为 `completed_with_issues`，未处理范围为空，已导出骨干、独立扩展、未解决事项及来源记录；最终轮复用此前阅读产物和部分精确匹配的成功响应，并非全部重新执行的独立全流程测试。具体运行、调用计数和质量发现见[当前测试结果报告](docs/12_当前测试结果报告.md)。

真实运行验证了32768上下文预算下的分批处理，以及单文件并发2/4的执行；尚未进行系统性能调优或新批量脚本的真实GPU压力测试。不可安全拆分的超大要求、骨架或整合材料仍可能输出 `incomplete`。已检查样例核心结构和部分来源/归属关系，但仍存在技术与商务/资格边界、目录粒度、人员证明章节重复及覆盖检查过严等问题；没有全文人工金标准、定量准确率评测或生产语义验收，`semantic_quality` 仍为 `not_evaluated`。

保留A的首份候选结构及原文明示约束，但正确识别A名称、层级与来源强制性仍依赖模型，不能以机械检查替代语义评估。扩展被要求修改后才保留时标为待确认，尚未完成自动改写与复核闭环。阅读模块的图片跳过、表格展开语义风险及历史诊断继续保留；指标、时限和数量冲突不自动裁定。
