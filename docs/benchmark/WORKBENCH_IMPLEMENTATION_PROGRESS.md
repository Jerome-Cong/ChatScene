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

BW-07 至 BW-12 尚未开始。每包完成后在此追加实际文件、检查结果、未验证事项、兼容性变化和回滚方式；正式发布仍受 Judge/仿真及真人试标验收约束。

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

## BW-03：提案—人工编辑—明确确认

状态：完成。

实际文件：两套源码新增 `review_model.py`、`review_draft_store.py`；新增 `test_review_model.py` 和 `ADR_BW_002_EXPLICIT_CONFIRMATION.md`。提案独立不可覆盖存储，草稿按修改来源和版本记录；确认收据覆盖当前题/可见语义组，全部覆盖后才能编译。

验证：`PYTHONPATH=tests/bus_benchmark .carla-runtime/benchmark/bin/python -m unittest test_review_model test_review_presentation test_review_context test_workbench_compatibility`，23 项全部通过（1.549 秒）。包括零 gold 生命周期、部分确认拒绝、修改/继承失效、显示快照变化、错 reviewer/源/guide、未知字段/疑问、CAS、不可覆盖机器提案以及新旧有效操作输出等价。独立复审无阻断。

兼容性：旧 checkpoint 和 notebook 未迁移；新模型为独立 API/状态文件。确认理由只在明确确认后的编译阶段展开；旧 formal validator/finalizer、评分公式与整套闭合不变。收据不证明人工身份，维护者可信源与真实人工控制仍必要。

未验证：新模型尚未接入离线浏览器（BW-04），未执行真实人工试标。回滚：撤销本包新增模块、测试和 ADR；旧工作流不受影响，新草稿保留供审计，不能改名当旧 checkpoint。

## BW-04：离线 HTML 与维护者题包流程

状态：独立实现与 Linux 浏览器验证完成；Windows 人工复验保留至 BW-11，未宣称跨平台实测通过。

实际文件：两套源码新增 `review_wire.py`、`review_packet.py`、`review_cli.py` 与三份 HTML/JS/CSS 资源，`cli.py` 注册 `review export/validate/import/finalize`；新增 `test_review_packet.py`、`test_review_browser.py`、`OFFLINE_REVIEW_WORKFLOW.md`；`.gitignore` 排除 `.review-workspace/`。12 题开发试标包已生成于本机 `.review-workspace/bw04-dev-pilot/packet/review.html`，未提交私人题包或生成任何真实 gold。

验证：Python packet/model/context/固定向量共 22 项通过（2.026 秒）；真实离线 Chromium 10 项全部通过（3.079 秒）。命令为 `BUS_BENCHMARK_BROWSER_TESTS=1 PLAYWRIGHT_BROWSERS_PATH=.../.carla-runtime/browsers PYTHONPATH=tests/bus_benchmark chatscene/bin/python-frozen -m unittest test_review_browser -v`。浏览器在沙箱外启动、测试 context 禁网；首次测试遇到严格 CSP 与 Playwright 等待器不兼容，修正测试等待方式，没有放宽应用 CSP。确认整套导入与部分拒绝仍由原正式 validator/finalizer 决定；复审发现的无实际变化/重复拆分已修复并有真实浏览器拒绝后修正成功测试。

兼容性：浏览器备份是新格式，不能冒充旧 checkpoint 或 gold；未知字段、坏原文依据、陈旧备份、来源/身份错误、双标签编辑、存储拒绝均有明确处理。正式 hash 规则不改；跨语言收据使用独立 wire 编码并有 Unicode/浮点共享向量。回滚：撤销新入口与资源，不删除本地题包/备份，继续使用原 notebook。

未验证：Windows 浏览器实测、真实人工效率、全量候选版性能与最终发布按 BW-11/BW-12 验收；不能将当前 12 题开发子包视为整套 confirmed oracle。所有审阅资产仍与生成器输入分离，实际方法进程可见性由 BW-10 检查。

## BW-05：具体修订提案与覆盖报告

状态：完成；仅保守规则能力，未证明真实效率或语义准确率改善。

实际文件：两套源码新增 `review_proposals.py`、`assets/review/revision_proposals.schema.json`；修改 `review_cli.py`、`review_packet.py`、`review_model.py` 与前端；新增 `test_review_proposals.py`、扩展真实浏览器提案应用测试；交付 `REVIEW_PROPOSALS.md`。CLI `review propose` 可适配旧 Agent 源绑定，`review export --suggestions` 可选生成离线建议视图。

机器替换项保留 query_text_regex 来源及精确引文，未伪装为 human_review；每个源 atom 都有记录。未知源字段不允许通过提案删除；数量范围、条件、可选否定、静态角色描述与另一个对象的动作均保留疑问。采用建议只产生机器来源的草稿编辑，疑问仍需逐条处理，最后才明确确认。

验证：Python proposals/packet/model/固定向量 25 项通过（1.966 秒）；浏览器全回归 11 项通过（4.032 秒），最终规则收紧后又定向复验提案应用至可信导入路径通过。独立复审无剩余阻断。

覆盖：完整开发集 48 题、538 原 atom，最终 5 条具体修订提案、26 条局部文字依据、507 条未解决；这些是规则分流数量，不是准确率。原文仅描述“through cyclist”等角色时不能证明穿越动作，复审后已从此前7条候选中剔除2条。LLM调用为0；输出记录配置和实现hash，制品在忽略入库的 `.review-workspace/bw05-development-proposals-verified/`。

兼容性：旧 oracle、题库、Agent 制品和计分不改；普通导出默认不开启该高疑问量配置。新题包字节绑定变化，旧包不得覆盖，BW-07 处理迁移。未验证：真实人工提案准确性、操作耗时、未覆盖措辞，不能把有限规则扩展成一般语言理解声明。回滚：撤销提案模块/可选入口并保留已有报告作审计，既有人工进度不删除。

## BW-06：完整表述差异、安全继承与草稿撤销

状态：完成。

实际文件：两套源码新增 `surface_diff.py`、`review_action_audit.py`，更新 forms/session、notebook、packet 与离线页面；新增 `test_surface_diff.py`，调整旧继承测试以明确新安全边界；更新生产流程文档并新增 `ADR_BW_003_SURFACE_INHERITANCE.md`。BW-00 旧继承向量及 hash 保留，没有重写历史证据。

行为：文字、数量、否定、角色、关系、触发、时序、修饰语、数值和要求增删都可对照；未知差异完整重审，相同 ID 不足以继承。只有同文同要求且范围一致才复制未确认草稿，新增条目记录目标文字适用依据，CPD 不同留空。已有目标编辑/完成状态保留。批审键按 dataset_split 隔离；before/after/source/target-checkpoint 审计支持只撤销未变化、未确认的目标草稿，不撤销 source 确认。

验证：旧 UI、surface 和固定向量联合 67 项通过（119.685 秒）；最终核心/可信导入/导航 15 项通过（2.142 秒）；真实离线浏览器 11 项通过（3.973 秒）。独立复审及 notebook 差异展示补充复审无阻断。测试包括 precise 人工新增不进入 vague/partial、跨 split、后续编辑/确认保护、CAS 失败准备记录不可撤销、独立 subject 数量与零 gold。

兼容性：有意减少旧自动继承，不清空旧 checkpoint；原其他 compiler 输出、计分与 finalizer 不变。sidecar 审计是新资源，不能丢弃后宣称仍可完整追溯。未验证：真人差异视图使用效率；未用词法分类宣称完整自然语言等价识别。回滚与旧更宽松行为的风险见 ADR，不能把回滚描述为无行为变化。

BW-06 补充：前端词典改为只查自身属性，未知 predicate/token 与 `constructor` 等 JavaScript 内置属性同名时仍完整显示并拒绝确认；新增实际浏览器反例，12 项全部通过（4.213 秒），独立复审无阻断。
