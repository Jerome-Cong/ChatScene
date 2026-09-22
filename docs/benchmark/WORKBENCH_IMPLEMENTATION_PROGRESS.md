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

BW-08 至 BW-12 尚未开始。每包完成后在此追加实际文件、检查结果、未验证事项、兼容性变化和回滚方式；正式发布仍受 Judge/仿真及真人试标验收约束。

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

## BW-07：疑问队列、源反馈与安全迁移

状态：完成；真实旧人工进度尚未提供，未冒充真人迁移验收。

实际文件：两套源码新增 `review_history.py`、`review_migration.py`、`review_issues.py`，更新 model/packet/wire/CLI 与离线页面；新增 `test_review_migration.py`，扩展实际浏览器迁移测试；交付 `REVIEW_MIGRATION_AND_ISSUES.md` 并更新离线手册。

行为：defer/requires_source_fix 保留编辑、撤销确认，问题单绑定原文/query/oracle/atom。迁移只读归档原始字节与跨次 lineage；纯 UI 更新可沿用原确认并显式标为 carried_confirmation，源变更只重审对应subject；指南或实际词典变化不得偷用旧声明hash保留complete。旧0.2 checkpoint无新指南绑定，保留表单但需重新确认。旧表单全部理由、required checks和CPD修订均可只读展开，JSON Pointer定位变化；浏览器不能在备份中自造迁移授权。

验证：Python migration/packet/model/surface/固定向量 33 项通过（3.976 秒）；真实离线浏览器 14 项通过（5.032 秒）。覆盖实际词典改动却伪留旧hash、纯UI沿用、单题源变化、错reviewer/源、编辑失效、原件不变、只读权限、重复迁移lineage和完整旧CPD理由。独立复审无阻断。

本机对实际BW-04开发题包执行了明确标记的未开始状态迁移：`.review-workspace/bw07-unstarted-demo-verified/` 保留12题草稿，0沿用确认，0gold；不是用户真人进度。兼容性与信任边界：迁移授权来自维护者可信assignment，仍不证明人类身份；模型编译器及finalizer保持原语义。浏览器数字精度限制也覆盖嵌套JSON字符串，无法安全解释的旧编辑留在原件/只读区，不静默舍入。

未验证：真实人工试标、真实旧checkpoint迁移、Windows实测，仍在后续验收范围。回滚：撤销新迁移/问题入口但保留完整输出目录、只读originals与lineage；旧入口继续可用，不能删除迁移证据后声称旧进度仍完整可追溯。

## BW-08：CPD 共享目录与普通审阅投影

状态：完成；Judge/正式跨平台验收仍保留原环境门槛。

实际文件：两套源码新增 `review_cpd.py`，packet 将共享目录纳入实际 dictionary 绑定，普通页面展示本题允许变化、目标、理由、锁定要求说明，完整策略/selector 放入只读专家展开区。复杂异议进入既有问题队列，清除旧确认；原 `CPDPolicyEditor` 保留。使用说明见 `CPD_REVIEW_CATALOG.md`。

验证：CPD/计分链、catalog、packet/model/migration 联合 51 项通过（4.174 秒）；真实离线 Chromium 15 项通过（5.536 秒）。独立复审无阻断。涵盖固定十对分母、失败惩罚、selector 不变、双字段一致、逐题策略不同、目录变化及旧无目录包触发重审、专家异议撤销确认。初轮新增顶层字段导致迁移拒绝，已修为 dictionary 内目录并加入反例测试；记录保留于本机测试日志。

兼容性：没有修改 scorer、提取器、冻结材料或正式 finalizer。距离目录解释现有 10/30 米阈值，不接受 UI 自创阈值；未知维度/不同分箱须专家处理。普通页只确认或提出异议，不新增自由 JSON 编辑入口。未验证：真人理解/效率、Windows 真机、兼容内核 Judge 和真实平台校准。回滚：撤销目录投影代码但保留旧题包/备份；不同词典的确认不得无审阅沿用。

## BW-09：唯一源码与完整 wheel 候选交付

状态：完成；交付版本为 0.2.0rc1，未宣称最终发布验收。

实际文件：唯一实现收敛至 `benchmark/src/bus_benchmark`，根目录副本已可恢复备份并退役；schema 使用此前生效的包资源。补齐 wheel 的全部审阅资源、query README、notebook optional extras；旧脚本改名 `scripts/bus-benchmark.py` 防止遮蔽安装包；legacy 工作区使用显式环境/cwd，路径相关脚本与测试使用导入包根目录。入口/构建说明见 `WHEEL_DELIVERY.md`，确切产物见 `WHEEL_RELEASE_CANDIDATE_MANIFEST.json`。

验证：44 项核心/迁移/固定兼容测试通过（4.645 秒），52 项 schema/题库通过（0.795 秒）；旧 UI/widget/native-worker 合计90项，初轮86通过、2错误、2原条件跳过，脚本遮蔽修复后2错误定向复验通过（26.634秒）；真实离线浏览器15项通过（5.380秒）。最终wheel的161个包文件与唯一源码逐字节一致，无多余包文件；sdist不含私有输入/环境。独立复审通过，并修复其指出的旧文档入口命令。

干净安装：新项目内 uv 环境只安装wheel及核心依赖，在仓库外新建目录用 `python -I` 执行可复现smoke；所有导入来自site-packages，无IPython/ipywidgets，paths/资源读取/两题合成确认传输/CLI校验及导入通过；失效确认和安装目录写状态被拒绝。合成收据不构成人工gold，未执行正式finalize。旧兼容脚本也从仓库外验证。

困难处理：系统Python缺distutils，使用已有uv Python 3.8.20构建；初次目录审计发现query README漏打包，已补齐并重新构建、安装、验证最终wheel。最终产物保存在 `.review-workspace/bw09-verified-wheel/`，不把私有审阅工作区提交到Git。开发需先安装包或显式PYTHONPATH；旧冻结配置的源码路径/清单需重新准备和验证，不能复用旧hash冒充新安装。

未验证/门槛：POSIX维护者持久化未声称支持Windows Python；Windows浏览器、真人效率/错误发现、第二真实方法及兼容内核Judge仍待后续验收。回滚：从Git历史恢复根目录和对应入口，或用本地源码备份；保留全部历史题包/进度与原失败记录，不能删除后冒充完成。

## BW-10：第二真实方法接入待提供工作区

状态：阻碍，未完成；按用户要求在无法自行补齐的输入处停止，未跳过本包宣称 BW-11/BW-12 完成。

已核对：公共 `MethodAdapter.generate(query_text, workdir)` 和 `AdapterOutcome` 已在唯一源码/wheel 中；ChatScene 旧 wrapper 仍在。计划指定的其他方法是 TTSG、Text2Scenario、Chat2Scenic，MetaDrive 是平台而不是第二生成方法，Talk2Traffic 不在本轮范围。已检查 `/home/ubuntu/Documents/shijie/` 及 `mdsn/` 的已知工作区目录，未找到上述三个方法的仓库；不据此推断整台机器绝对不存在。

需要用户提供：优先接入的第二方法的本地仓库路径（或准确仓库地址/版本）及可实施修改的工作区；其余两个方法的路径可随后提供。推荐优先 Text2Scenario 以检验不同原生产物边界，但需先读实际接口，不能预设其输出就是 Scenic。真实生成需要的运行配置/交互规则/凭据仍需在对应工作区按现有安全规则核验；不要把密钥写入文档或 Git。

尚未完成：本包 fake adapter 隔离/失败路径专项验收、ChatScene 当前运行配置重绑定、第二真实方法闭环、2D证据投影接口验收。旧 ChatScene 配置仍引用 `/home/shijie20/`，不会覆盖旧冻结配置来伪造当前可用性。BW-11 真人试标/Windows实测与 BW-12 最终发布仍待后续；Judge兼容内核门槛按用户授权延期补齐，保留原失败记录，不是此次停下的原因。
