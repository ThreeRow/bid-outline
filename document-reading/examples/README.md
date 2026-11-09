# 历史阅读输出样例

四份样例来自 `output_integrated/20261025_fusion_e2e/<case>/reading/`，是历史阅读阶段产物，不是后续目录蓝图，也不是本次精简副本的新运行结果。四份均为 version=2；语义质量字段均为 `not_evaluated`。本次未读取原DOCX。

| 样例 | 原块数 | 一二级节段数 | 原表数 | 诊断数 | 历史执行 / 结构状态 |
|---|---:|---:|---:|---:|---|
| [深圳：AI合规审查工具](shenzhen/PREVIEW.md) | 903 | 21 | 22 | 5 | incomplete / organized |
| [福建：考场实时智能巡查云服务](fujian/PREVIEW.md) | 1149 | 56 | 25 | 1 | incomplete / unresolved |
| [南京机场：信创云项目](nanjing/PREVIEW.md) | 715 | 80 | 33 | 8 | incomplete / organized |
| [江苏法院：网络安全服务](jiangsu/PREVIEW.md) | 601 | 53 | 13 | 4 | incomplete / unresolved |

每例 `PREVIEW.md` 提供标题、全文、重点技术章节、结构化结果和表格摘录入口。完整复制的历史文件保持字节一致，来源与哈希见 `SOURCE_MANIFEST.json`；`SUMMARY.json` 和 `PREVIEW.md` 是本次整理生成。

未复制原DOCX、原始模型调用日志或历史运行配置。保留 `reading.md`、`headings.md`、`document.json`、`source_map.json`、`validation.json` 和全部章节及索引。用于展示接口可返回的内容与格式；不作为正确答案或当前版本验收依据。
