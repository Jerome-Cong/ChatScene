# Workbench 工作包实施记录

本文件为实际交付状态；原计划正文保留设计意图。2026-09-23 用户授权先推进独立审阅与打包工作，Judge 校验与历史失败记录保留，正式评测和最终发布前须在兼容内核补齐。

## BW-00：审阅基线

状态：完成（按用户授权的独立审阅范围）；Judge/仿真限制仍保留。

交付：`WORKBENCH_REFACTOR_BASELINE.md`、`ADR_BW_001_COMPATIBILITY.md`、`WORKBENCH_OPERATION_MEASUREMENT.md`、`WORKBENCH_COMPATIBILITY_CASES.md`；新增 `tests/bus_benchmark/fixtures/workbench_compatibility_v1.json` 与 `test_workbench_compatibility.py`。

固定样例来自获准开发题和明确标记的合成编辑，覆盖 accept、permitted、exclude、modify、split、merge、新增、support 源错误和 CPD 修订。每个输入表单固定旧 compiler 正式 payload 的 canonical SHA-256；同时调用权威 validator 检查合法/阻断结果。样例记录 query/oracle/guide/protocol 源版本、assignment bundle hash 和 reviewer 标签，不生成真实 human gold。

继承与并发恢复由新增固定表单/checkpoint hash 和原有 workbench 回归测试共同固定：已有进度不覆盖、已完成 precise 继承为未确认草稿、陈旧会话不得覆盖或定稿。真实人工操作耗时与真实 checkpoint 为 unknown；测量定义已预先登记，未宣称效率提升。

验证命令：

```bash
PYTHONPATH=tests/bus_benchmark .carla-runtime/benchmark/bin/python -m unittest \
  test_workbench_compatibility test_query_review_workbench test_human_workflow
```

结果：联合回归 85 项全部通过（133.389 秒）；复审后补齐继承/冲突固定向量与精确错误检查，更新后的兼容测试 2 项单独通过（0.301 秒）。既有 metrics 52 项通过，私钥安全回归已修复并推送；全量原始失败及 Judge 内核限制见基线记录，不因本包完成而抹除。

兼容性：未改题库、oracle、metrics、编译器或正式 finalizer。回滚：反向撤销新增样例、测试和本包文档更新即可，不影响历史人工进度。CARLA 完整发布包已下载完成；尚未解压或启动服务器。

## 后续工作包

BW-01 至 BW-12 尚未开始。每包完成后在此追加实际文件、检查结果、未验证事项、兼容性变化和回滚方式；正式发布仍受 Judge/仿真及真人试标验收约束。
