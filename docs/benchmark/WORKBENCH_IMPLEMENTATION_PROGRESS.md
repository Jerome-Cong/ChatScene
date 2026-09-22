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

BW-03 至 BW-12 尚未开始。每包完成后在此追加实际文件、检查结果、未验证事项、兼容性变化和回滚方式；正式发布仍受 Judge/仿真及真人试标验收约束。

## BW-01：可移植审阅服务与显式上下文

状态：完成。

实际改动：两套源码新增 `review_context`、`review_forms`、`review_session`、`review_store`、`review_presentation`、`review_vocabulary`、`review_legacy`；原 notebook 改为调用并重新导出服务；`paths.py` 统一禁止安装目录状态写入。新增 `test_review_context.py`，原故障注入测试更新到实际 I/O 模块。路径和 CLI 用法见 `WORKBENCH_SERVICE_MIGRATION.md`。

验证：`PYTHONPATH=tests/bus_benchmark .carla-runtime/benchmark/bin/python -m unittest test_query_review_workbench test_review_context test_workbench_compatibility` 联合 65 项通过（120.475 秒）；复审补齐旧入口安装目录保护后，更新后的 context/固定向量 7 项再次通过（0.647 秒）。提取的 compiler 函数逐个 AST 对照与旧实现相同；独立复审无剩余阻断。

兼容性：原 public 导入、表单 canonical payload、checkpoint schema、确认/finalizer 语义保持；新上下文允许任意非空 suite 标签，48/252 仅保留于历史套件 manifest。缺失来源、未知 workspace、错 manifest 数量与安装目录写入被拒绝。核心在异目录运行且禁止导入 ipywidgets/IPython 的测试通过。

未验证：本包未执行 Windows Python 存储测试、脱离 checkout 的 wheel 验收或仿真/Judge；分别属于后续入口/打包/环境验收。回滚：反向撤销本包代码拆分和路径新增；旧 checkpoint 无需数据转换，保留此前私钥修复。

## BW-02：完整语义投影、共享词典与指南

状态：完成。

实际文件：两套源码的 `review_registry.py`、`review_presentation.py`、`review_field_widgets.py`、`query_review_workbench.py`；两套包内 `assets/review/guide_zh.md`、`practice.json`；新增 `test_review_presentation.py`。词典登记当前套件全部 predicate/字段，并同时提供卡片说明、编辑标签、提示与错误名称。

行为：所有参数、计分层级、正反极性、备注和未知原值可见；未知字段、缺失必要参数、无效原文位置阻断整题快捷预填/确认资格。有效原文位置单独显示精确文字，机器元数据不冒充原文依据。未知角色/事件标记保留原值及可读空格形式，不自动推断其含义。事件缺失参与者、触发、结束信息时明示未明确。

复审曾发现禁止层被错误地额外取反，现已修正：既有计分器按 polarity 判定，展示不新增第二次取反；禁止层配正向 polarity 会警告并禁快捷通过。练习与测试直接用既有计分器核对有/无参与者的结果，没有改 metrics。六个合成练习仅供培训，不产生 gold。

验证：`test_review_presentation test_review_field_widgets test_query_review_workbench test_workbench_compatibility` 首轮 95 项中仅 1 项源归属展示兼容断言失败；保留原标记并补可读空格显示后，`test_review_presentation test_review_field_widgets test_workbench_compatibility` 加该失败用例共 39 项全部通过（2.558 秒）。测试遍历当前全部草稿的字段覆盖，并逐项改变参数验证显示差异；独立复审无剩余阻断。

兼容性：正式表单、源码题库、计分与 finalizer 不变；可读文案和编辑字段标签发生有意变化。未知字段不再允许整题机械预填，专家逐条编辑入口保留。未验证：实际新人练习/效率与浏览器流程属于 BW-04/BW-11，未宣称收益。回滚：反向撤销本包展示/标签/快捷保护及新增培训资产，不转换已有 checkpoint。
