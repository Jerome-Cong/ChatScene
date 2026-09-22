# BusScene：2D 场景生成 Benchmark 与标注 Workbench 改进实施计划

日期：2026-09-22  
状态：2026-09-23 BW-00 独立审阅基线已完成；已获准继续独立审阅与打包工作，Judge/仿真未通过记录保留，正式评测及最终发布前补齐。逐包实际状态见 [实施记录](WORKBENCH_IMPLEMENTATION_PROGRESS.md)，历史环境证据见 [基线记录](WORKBENCH_REFACTOR_BASELINE.md)。
工作区：`linux-5880:chatscene:main`；scope：`planning`。  
目标：保留已经定义的评测口径，降低 query oracle 标注的理解与操作成本，并让同一 benchmark 包接入 ChatScene、TTSG、Text2Scenario、Chat2Scenic，而不是在每个仓库重复复制和标注。Talk2Traffic 不在本轮 baseline 范围。

## 0. 核心决策

**不建议继续把主要精力放在 ipywidgets 的局部按钮和折叠面板优化，也不建议推翻现有 metrics。**

建议将当前“人工逐字段关闭一份内部 Schema”改为“人工审阅当前 query 对应的完整、可理解的评分要求草稿，只修改异议”，再由共享转换器展开成现有正式提交格式。标注人员使用离线 HTML；项目维护者使用 Python 完成准备、导入、校验与定稿。现有 `human_workflow` 校验和最终定稿器继续作为权威。

本轮真正需要同时解决四件事：

1. 草稿是否已经把语义讲清楚，而不是只把元数据、事件标签和疑问转交给人。
2. 人是否必须反复确认机器格式字段，而不是确认一次真正的语义判断。
3. 标注是否脱离 Jupyter、远程 Python 和仓库目录，可以委托给普通浏览器用户。
4. 安装包是否真正独立于 ChatScene checkout，并通过第二种方法的接入证明可迁移。

所有未注明“已核实”的接口、目录和验收阈值，均为本计划的建议，不表示仓库已经实现或测试通过。

## 1. 本轮证据与限制

### 1.1 固定源码视图

| 项目 | 值 |
|---|---|
| view_id | `view_1se9_oQ4CojaQyfQNhuJbPHGWfqEMzey` |
| 捕获时间 | `2026-09-22T09:04:44.144949+00:00` |
| content_digest | `12bc82be3a0f69d050bdb4331ddcced92dc94400fda0b9ecff57b068b7ad94c3` |
| 范围 | 1,067 个获准的已保存文件；不是整个仓库审计 |
| 一致性 | `verified_non_atomic`，不包括未保存编辑器缓冲区 |
| Git | unavailable；不提供或推断 branch、commit、dirty 状态 |
| 复核 | `verify_workspace` 于 `2026-09-22T09:11:53.853724+00:00` 返回 unchanged=true，新增/删除/修改均为 0 |
| 执行权限 | 实际开放 tree/read/search/evidence/verify；本轮没有项目执行或审计任务运行 |

`query_lib/bus_ego_topdown_2d_dev_query_library_v0_2.jsonl` 的读取返回 `PATH_NOT_ALLOWED`。没有通过其他副本绕过该限制。下文的题量和工作量是所读文档报告的数量，不是本轮重算的实时数量；实际私人 checkpoint 的已完成比例、单题耗时和浏览器性能未核实。

### 1.2 关键源码依据

以下 R 编号仅为本计划的证据索引，不是仓库已有的工作包编号。行号对应上述固定视图。

| 索引 | 实际文件与范围 | 支持的结论 |
|---|---|---|
| R01 | `CONTEXT.md:1–212` | query-only 输入、生成与驾驶诊断分离、冻结输出、既有 SRS/IEC/CPD 等边界 |
| R02 | `docs/benchmark/HUMAN_REVIEW_WORKFLOW.md:1–270` | 当前题量、已有五步 UI、批审、派生检查、条件理由、precise 继承 |
| R03 | `docs/benchmark/HUMAN_REVIEW_WORKFLOW.md:271–480` | 草稿/完成/定稿边界、锁与 CAS、源错误处理、正式 finalizer |
| R04 | `docs/benchmark/HUMAN_REVIEW_GUIDE.md:1–260` | query/platform/calibration/post-test 生命周期；design-visible 声明；非数值差异覆盖不足 |
| R05 | `bus_benchmark/query_review_workbench.py:1–210` | 仓库相对路径、固定题库文件/数量、判断选项与术语 |
| R06 | 同文件 `343–472` | `_human_token` fallback；语义说明与元数据依据显示 |
| R07 | 同文件 `625–977` | 空表单、`response_from_form`、六检查闭合、继承实现 |
| R08 | 同文件 `1660–1884` | 状态与进度、逐题验证、全部完成后定稿、不可覆盖输出 |
| R09 | 同文件 `2437–2633` | 多状态筛选、机器预填和清空混合、CSS 读取、指南与不确定操作 |
| R10 | `bus_benchmark/oracle.py:1–250` | 角色到空间启发式、元数据事件标签、列表顺序推 before、风险草稿 |
| R11 | `benchmark/README.md:1–53`；`benchmark/pyproject.toml:1–30`；`benchmark/setup.py:1–6` | 已有独立包和 command 接口；包资源配置仍需完整安装验证 |
| R12 | `benchmark/src/bus_benchmark/paths.py:1–27`；`adapters/__init__.py:1–40` | 已有资产/工作区路径入口和 query-only MethodAdapter |
| R13 | `bus_benchmark/review_field_widgets.py:1–180` | 通用结构化字段编辑已有实现，不应声称现有 UI 只支持原始 JSON |
| R14 | `tests/bus_benchmark/test_query_review_workbench.py:1–175`；测试目录树 | 已有大量 widget/契约测试；所读测试不是实际标注效率证据 |
| R15 | `bus_benchmark/native_observer.py:1–180` | 原生平台观察器已有独立环境边界，不读取 oracle/ontology |
| R16 | `annotation/annotation-ui.md:1–107` | 现有离线、单击、草稿辅助等交互设计资料；任务语义不同，不能直接当作当前 formal 协议 |

源文件校验值：

- `query_review_workbench.py`：`a2005964feafda3a782da410514a85fcb318c2c0ffae36e3237be5dbc4f52e94`
- `human_workflow.py`（目录记录）：`5ee495ed9e6c4bc1c7340535aa8e610cd5eb6ac0eca3db6840d9f813ab9ba900`
- `oracle.py`：`e44f6fa377d040b2cb54dc24c27f4b639717b4ddf88ee30d15af74fff9f18735`
- `HUMAN_REVIEW_WORKFLOW.md`：`6d37e1e4b72be0c15e708d638e373120199a45a2b07400e619974f7845e6de04`
- `benchmark/pyproject.toml`：`d589fd7f5c69b86115659cea89959492ac5b72407bf0e22183dacde0b337c7e7`

附件 `annotation_ui.html` 的实际 SHA-256 为 `b3e362eb56455b54fe3510af6f849681db2376f3909313676e1cd3c3d3e5e774`，与当前视图目录记录中的 `annotation/annotation_ui.html` 一致。它可以直接作为当前仓库内的参考资产；这不是一次浏览器运行验收。

### 1.3 外部接口核对的用途

仅核对与适配有关的方法差异，不引入这些论文自己的指标。作者资料表明：TTSG 包含 prompt analysis、road retrieval、agent planning、road ranking；Chat2Scenic 的交互模块包含用户确认/迭代，再生成 Scenic 并执行于 CARLA；Text2Scenario 采用分层场景库和 DSL 生成。故“同样使用 CARLA”不等于“同一原生产物或同一自动化入口”。Text2Scenario 和 TTSG 的实际本地可执行接口仍应在各自接入任务中核实。

核对来源：TTSG 作者项目主页；TUM-AVS/Chat2scenic 官方 README；Text2Scenario 原论文 arXiv:2503.02911v1。本文不声称已读取另外三个 baseline 的本地代码或已完成其适配。

## 2. 当前问题：不是缺少某几个常见功能

### 2.1 已有能力必须保留

现有 workbench 已有自动保存、完成并继续、同语义批审、precise 向 partial/vague 的草稿继承、四项高层检查自动派生、条件化理由、卡片级错误定位、结构化高级编辑、锁和 CAS。[R02/R03/R07/R08/R09]

不能再把“增加自动保存/批审/下一题/中文标签”当成主要交付。应在这些机制上改变人机分工，并用已有测试保证不退化。

### 2.2 标注者仍被当作 Schema 编辑者

文档报告 300 个 query subject 内有 3,601 个 atom、1,800 个 required-check、300 个 CPD 判断，共 5,701 个序列化判断字段。但四个高层检查已经自动派生，所以 **5,701 既不是当前必需点击次数，也不是 5,701 个独立人工语义判断**。[R02/R07]

真正的问题是：很多人工动作仍对应底层字段、source/target、校验关系，而不对应一个自然的“这条要求对不对”的判断。前台应围绕语义要求组织；后端继续完整展开全部字段。

### 2.3 草稿质量把语义工作推给人工

`oracle.py` 从角色名猜空间关系；把事件标签装入 `event_spec`；对某些时序类型按事件列表顺序生成 before；conditional_trigger 的绑定留待人工补充。它也明确把风险元数据是否属于生成要求交给人判断。[R10]

这意味着标注者不是简单复核，还在重建需求。文档中的 Agent 建议有 3,778 项 `human_judgment_required`，但建议本身不提供可执行的 `proposed_oracle` 修订。[R02/R03] 该数字是历史文档统计，不是本轮测量。

### 2.4 中文显示不等于语义已经完整表达

`_human_token` 的 fallback 是把下划线换为空格；`event_spec` 的常见说明只显示事件名称；大量依据仍是元数据字段，而不是当前 query 的原文依据。[R06]

需要“字段完整、对象清楚、原文可追溯”的说明，不只是把机器标签换成中文标题。若草稿本身没有角色、触发或终止信息，显示层必须标明“草稿未明确”，不能自行补成事实。

### 2.5 包已存在，但 workbench 未完全脱离仓库

`benchmark` 已有独立安装元数据、资产路径入口和 command adapter；与此同时，workbench 仍硬编码仓库父目录、导出脚本、题库/草稿路径和 48/252 数量。[R05/R11/R12]

两个目录 `bus_benchmark/` 与 `benchmark/src/bus_benchmark/` 同时存在，多数已对照模块 hash 相同，`schema.py` 的 hash 不同。这里能够确认“两套树存在”，不能据此断言所有差异都是错误或由人工双维护。迁移必须先查明构建关系、再收敛唯一编辑源。[R11/目录证据]

CSS 由模块旁的 `.css` 文件加载，而所读 package-data 配置只显式列出 schema/query assets。是否实际进入 wheel 尚未执行验证；必须增加脱离仓库的安装测试，而不是仅凭源码目录能运行就宣称可移植。[R09/R11]

## 3. 本轮不可改变的边界

保持当前 metrics、计分分母、支持/不支持口径、generation repetition、修复预算、冻结输出和主要/诊断指标区分，不通过 UI 改造重新设计 benchmark。[R01]

尤其保留：

- 每个方法收到同一个英文 `query_text`；oracle、人工标签、CPD policy、其他表述、评测事件图不能作为生成器输入。
- 仅评估现有协议中的 2D 道路、参与者、空间关系、事件/时序等维度。不新增外观真实感、渲染质量、纹理、3D 感知等 metrics。
- `IEC_spec` 与执行诊断分开。不能把运行时没观察到某事件直接当作生成器未正确指定事件。
- 普通 query 的 ego 初始意图与其他参与者事件，不等于强制 ego 的制动/让行/等待反应。
- 允许出现、不要求出现、明确禁止、材料无法判断是不同含义，不可为简化 UI 合并。
- 生成结果不允许由评测器补齐/修复；方法原生且预先声明的修复预算单独记录。
- one-reviewer/one-gold、外部真实人工控制、草稿不能成为 gold、源绑定、全部完成后正式定稿继续保留。
- 当前套件的 design-visible 性质不因 UI 升级变成 untouched held-out test。[R04]

修改“如何由人确认”应有明确交互协议 ADR；这不等于更改“怎么算分”。禁止把交互流程变更伪装成完全没有协议影响的 CSS 修改。

## 4. 拆开三类工作，不让普通标注者承担全部流程

| 工作 | 人回答的问题 | 可复用范围 | 不应混入 |
|---|---|---|---|
| Query oracle 审阅 | 当前文字到底要求什么，什么只是允许的补全？ | 同一个冻结 query suite 对所有方法共用一次 | 某方法输出、CARLA 能力限制、Judge 预测 |
| 平台/规则与校准审阅 | 解释器、平台证据及难判语义是否可靠？ | 相同平台与适用的证据版本共享；变化时按依赖重新验证 | 要求每个 query 标注者重新设计平台能力表 |
| 输出审计 | 这个已生成产物是否满足已冻结要求？ | 按现有采样/校准协议，与具体产物关联 | 反过来修订某个方法的 query 标准 |

本轮优先交付第一行。后两行可以复用浏览器外壳和播放器，但使用独立任务类型、字段、权限和盲审约定。不要为了统一 UI，把三个任务的标签含义合并。

## 5. 新的标注体验

### 5.1 正常路径

`打开离线题包 → 确认代号一次 → 阅读当前英文 query 与要求草稿 → 修改异议/处理疑问 → 确认本题并继续 → 导出提交文件`

普通标注者不需要启动 Python、安装 Jupyter、连接 CARLA、复制 source/target atom ID、寻找 JSON 或配置输出目录。维护者仍负责 Python 导入和 canonical finalizer。

### 5.2 页面结构

左侧保留题目队列与进度，主区固定显示当前 query。其下直接显示精简但完整的要求草稿，包括参与者、道路/空间、事件与时序、规则和必要约束。每条要求旁标明原文依据、当前计分属性、机器建议/人工修改来源。

“需要决定的地方”放在草稿摘要之后，集中列出缺失、歧义、冲突和跨表述差异。不能为了差异优先把其余要求完全隐藏；当前确认覆盖的完整草稿必须可读，无法解释的字段阻止整题快捷确认。

只保留一个主动作：**确认本题并继续**。其他动作是修改、暂存疑问并下一题、返回；备份/导出放在稳定位置。复杂合并/拆分保留专家入口，不再成为普通修改的必经步骤。

### 5.3 机器建议与人工判断分开存储

首次显示可直接展示可理解的机器草稿，不要求每题先进入“机器辅助与清空操作”并确认覆盖。但：

- `proposal` 不等于 `human_decision`；页面载入、打开指南、切换题目、保存草稿不能自动产生人工 accept。
- 明确的整题或可见语义组确认，应记录当前草稿内容摘要、覆盖的具体条目、当时所有修订和人工确认动作。
- 编译器据此生成逐 atom 的 accept/reject/modify/split/merge 记录，以及同一套派生检查；不是删掉 required fields。
- 对仍有疑问、未渲染字段、失效依据或不兼容修订的题目，不提供“全部默认正确”捷径。
- 不允许跨全部 300 题的一键确认；不把滚动、停留时间或单击当作可以证明真人身份/认真阅读的凭证。

整题确认减少机械动作，但不能缩减本应审阅的内容。

### 5.4 对标注者使用的词

| 内部概念 | 前台表达 | 处理要求 |
|---|---|---|
| oracle | 评分要求草稿／已确认评分要求 | 不让人把它理解成模型答案 |
| atom | 一条要求 | IDs 仅技术详情可见 |
| accept | 这条要求及计分方式正确 | 不是“生成结果满足它” |
| reject atom | 从评分要求中移除 | 不是“该对象不得出现在场景中” |
| permitted | 可以出现，但没有也不扣分 | 与删除要求、明确禁止区分 |
| forbidden | 明确不得出现 | 只有有依据时使用 |
| source/target | 修改前／修改后 | mapping 由编译器维护 |
| CPD | 保持要求不变时，哪些内容可以合理变化？ | 公式、selector、分箱放维护者视图 |
| locally_valid | 隐藏为内部校验状态 | 不与“已人工确认”混为一谈 |
| unsupported query | 请求不符合本基准任务范围或存在规定的冲突 | 不等于某个 baseline 做不到 |

`core_required` 和 `surface_required` 的差异仍保留，可用“共同核心要求／本条文字额外要求”的解释；不得因为标签太多把两个底层概念合并成一个数据值。

### 5.5 指南不是词汇表堆积

首次只显示一页任务说明和几道非正式练习，之后按卡片提供简短提示与正反例。最少讲清：当前文字是依据、不要用 precise 补 vague、不是判断生成结果、不是设计控制器反应、移除≠禁止、如何暂存疑问。

例子必须来自有授权的开发材料或明确标注的合成练习，不拿正式 test 输出反向调整规则。中文辅助解释不能替换原始英文 query，也不能成为生成器输入。

## 6. 附件应复用的内容与不应照搬的内容

附件已具备单文件离线页面、题目队列、按任务显示字段、草稿/提交分离、修改后重新提交、版本绑定、备份恢复、冲突保护，以及 2D 时间轴、车身矩形和缺测提示。

建议复用其交互外壳、状态设计和播放器思想；源码仍拆成可维护模块，由构建步骤生成单文件交付，不把几千行 JS 或大量题目数据手工塞进源模板。

不照搬：附件的 `behavior`/`pair`、positive/negative、事件边界秒数是轨迹定标问题。本轮 query oracle 尚未看方法输出，不需要强制播放轨迹，也不应人为生成“参考场景”作为文字要求的唯一解释。

查询标注包只包含必要文本与结构化要求；轨迹播放器留给输出审计/校准类型。不同任务可以使用同一 browser shell，但不能声称整个 LangTrajEval、BusScene、SCTG 的标签和数据处理后端都可共用。

## 7. 降低重复工作的四个具体改造

### 7.1 先把草稿从“提示”升级成“可审阅的修订提案”

保留原始 oracle draft，不静默重写。新增独立 proposal 层，记录当前 query/source hash、涉及要求、候选修改、原文片段/来源、解释、未解决原因。可在离线准备阶段调用规则或 LLM，但标注 UI 不实时调用模型。

对可明确的问题给出具体建议，例如“该距离仅存在 precise，本条未规定，建议改为 permitted”，而不是仅显示 `human_judgment_required`。原文没有证据时允许保持未知；LLM 解释不自动成为证据。一般性隐含条件必须引用已冻结的定义或列明推理依据，并接受人工异议。

保留每个旧 atom 的处理映射；草稿变简单不能通过偷偷删除难项实现。所有语义更改仍要由人工接受后编译进入正式 oracle。

### 7.2 语义组确认，而不是重复确认一个对象的每个字段

例如一个骑行者的类型、角色和空间关系可以形成一组完整可读要求，确认组时明确覆盖其各条内部 atom；人修改某个属性时只需要改那部分。序列化仍是原有原子条目，避免改变 SRS 权重和统计含义。

自动派生的四项 consistency checks 继续自动生成，不重新做成四次人工点击。support/disposition、CPD 必须在本题摘要中有明确可审阅内容，不能因被归入“自动”而无声通过。

### 7.3 从已有继承升级为完整的跨表述差异审阅

precise/partial/vague 可作为同 intent 的导航组，但仍有三个独立 subject 和三次明确的表述级确认。

继承时不仅检查 atom ID，还检查当前表述的文本依据及允许继承范围。差异包括数量、否定、对象角色、相对位置、事件是否出现、触发条件、先后/同时、修饰语和数值；不能只覆盖数值变化。[R04/R07]

特别检查现有 `_inherited_surface_form` 中人工新增要求的继承：precise 新增内容不能仅因为属于同一个 intent 就进入 vague。当前实现保留 unconfirmed，因此这不是已核实的错误 gold；风险在于确认负担和容易被忽略的默认值。[R07]

说明性合成片段：precise 的 “A cyclist approaches from 10 m behind the bus.” 与较模糊的 “A cyclist approaches near the bus.” 不能共享一个强制“10 m、后方”的已确认要求。它们不是本轮读到的真实 query。

系统仅建议安全继承的部分；不能确定差异是否完整时显式标记并退回整条审阅。已有人工例外和已完成记录不被覆盖。

### 7.4 将现有批审降为可选的辅助工具

当前批审按正式语义精确分组，但要求阅读其中所有 query，所以有可能反复展示相同句子。[R02] 默认改为按 query/intent 连贯审阅，批审保留给确实跨多题共享且可说明上下文的操作。

批审必须保持 split 隔离，只填未处理草稿、显示覆盖清单、记录来源，支持撤销本次草稿批写；不能以同一个 predicate/hash 证明不同文字语境都要求它。

## 8. 疑问和源错误必须有完整出口

新增工作流层的“暂存疑问”与“需要修正源材料”，不是添加新的计分标签。普通标注者选择结构化原因后可去下一题，不再用“空白字段+备注”隐式表达同一种情况。[R09]

建议原因：文字歧义、缺少信息、草稿遗漏/矛盾、术语不清、界面无法表达、题库源标记错误；只有“其他”强制自由文本。

待处理记录保存完整编辑和版本，进入单独队列；它们不生成 gold、不从正式套件分母消失、不使整套定稿绕过未完成检查。

维护者处理源错误后提供新的、合法绑定的 assignment 或修订提案，并给出旧→新映射和差异报告。绝不通过编辑 checkpoint hash、伪造 complete、覆盖旧文件解除阻塞。未受影响记录能否保留，要由源/语义摘要与迁移规则证明；语义受影响记录必须重新确认。

普通前台不暴露 FINALIZE、输出路径和高风险清空控制。完整定稿仍由维护者完成；这只是职责分离，不改变每个 subject 的最终人工确认责任。

## 9. CPD：准备工作集中，query 判断仍独立

CPD 很多控制项属于本基准的共同规则，而不应每道 query 都从头创建：维度定义、允许值词典、query-blind selector、分箱和跨平台可观察性应由维护者维护版本化目录。

前台只显示“本题允许哪些变化、哪些被文字锁定”及简短理由，使用已有 policy 的可读投影。人可以接受、提出异议或暂存；复杂 policy 编辑继续使用专家入口，输出仍通过原有 CPD 校验。

保持 method-independent 的 CPD 定义，不为某方法增加白名单、不因某方法能力弱就缩小分母。相同维度目录可复用，不代表不同 query 的 eligibility/policy 可以未经确认直接继承。

这不增加一条强制的第二人工复核流程。维护者准备的目录或提案与当前 subject 的最终人工确认，仍按既有责任约定区分。

## 10. 可迁移包的目标边界

### 10.1 一个公共实现，不是每仓库一个副本

建议最终唯一可编辑实现放在 `benchmark/src/bus_benchmark/`，并在 BW-00 核实当前构建/复制关系后执行。根目录 `bus_benchmark/` 若需兼容，应成为薄入口而非另一份独立业务实现。两个 `schema.py` 的差异必须先解释再处理。

公共包分成以下逻辑模块；这是建议的职责划分，不要求一次搬完所有文件：

```
bus_benchmark/
  ...现有 metrics / schema / generation / runtime ...
  review/
    context.py          # 显式 suite、source、workspace 与版本
    service.py          # 人工编辑、检查、提交的纯 Python 服务
    compiler.py         # 可读审阅状态 -> 现有正式 payload
    presentation.py     # 术语、字段投影、完整语义显示
    proposals.py        # 机器提案，独立于人工决定
    surface_diff.py     # 跨表述差异与安全继承
    issues.py           # 疑问队列与修订关联
    packets.py          # 离线题包导出、回收、验证
    migrations.py       # 显式版本迁移与差异报告
    web_src/            # 模块化 HTML/CSS/JS 源
    web_dist/           # 构建出的单文件离线界面
```

`human_workflow.py` 的最终验证语义优先保持；先抽出已有 `response_from_form` 和会话业务，避免一边重写 finalizer、一边更换 UI。新旧前端共同使用一个权威服务。

### 10.2 目录和依赖分离

包内只放只读资产和代码；题包、草稿、提交、导出和日志放 caller-owned workspace。复用已有 `asset_path()` 与 `workspace_root()`，不在安装目录旁创建 `benchmark_artifacts`，不依赖 `scripts/` 的源码路径。[R05/R12]

核心包不依赖 Jupyter/ipywidgets/CARLA；旧 notebook 支持成为可选依赖。评测 Python 与各方法/平台 Python 分离，通过已有子进程和文件契约交换数据。

建议命令名（待实现，不是当前可直接运行的命令）：

```
bus-benchmark review export --manifest ... --assignment ... --output ...
bus-benchmark review import --assignment ... --submission ... --output ...
bus-benchmark review validate --assignment ... --submission ...
bus-benchmark review finalize --assignment ... --submission ... --output ...
```

导出/导入仍允许分包工作，但正式整套闭合规则按现有协议。不能把部分已提交结果命名为整套 confirmed oracle。

### 10.3 三类制品分开放置

- 公共方法输入：公开的 query 输入与预先声明的统一配置。
- 评测私有资产：oracle、人工审阅、Judge/CPD 等，在评测 workspace 维护，不复制为生成器可读取的上下文。
- 方法运行结果：不可变原生产物、过程日志、运行/平台证据，引用同一 suite/oracle/protocol hash。

query-only 的 Python 参数不自动等于文件系统安全隔离。接入验收要说明进程实际可见的目录、环境与文件；能隔离时不挂载评测私有资产，不能证明时明确采用何种信任边界，不能宣称零泄漏已被保证。

## 11. Baseline 与平台适配

保持已有 `MethodAdapter.generate(query_text, workdir)` 及 command adapter 思路。[R11/R12] 每个方法仓库仅增加自己的入口 wrapper 和配置，不复制 oracle 标注或评分代码。

方法适配负责原生生成入口和产物归档；平台适配负责 compile/SV/NE、地图与轨迹观测。两者不能绑死成“CARLA == Scenic”。

| 对象 | 接入重点 | 必须避免 |
|---|---|---|
| ChatScene | 保留现有生成阶段及既定 proxy 处理，先作为回归基线 | 为通过新评分标准额外插入事件/道路修复 |
| TTSG | 核实其道路选择、agent plan 与可执行产物的实际边界，再接外部接口 | 未读原接口就强制改成 Scenic 生成器 |
| Text2Scenario | 核实实际版本、DSL、场景库与程序入口，记录全部构造依赖 | 仅凭论文名称承诺已适配 |
| Chat2Scenic | 明确交互确认的自动化 profile、确认/澄清/停止规则 | 使用人工或 oracle 回答追加问题以提高主榜分数 |
| BusScene | 输出自身原生产物，由对应平台适配提取同一语义证据 | 使用更有利的专属评分标准 |

Chat2Scenic 主实验建议采用预先固定、无额外人工语义输入的自动化配置，公开说明与交互式用法的差别。机械性的继续操作可以预定义；需要新信息的追问不能从 oracle 补答案。带人工澄清的版本若做，只能作为明确区分的辅助实验，不能混入同一 query-only 主结果。

共享的 2D 审阅视图应是现有证据的投影，不是第二套计分真值。最小信息包括明确的坐标/时间单位、actor identity、尺寸、姿态、有效观测 mask、必要地图/车道关联和原生产物 hash。轨迹来源需要区分“产物指定的计划”和“实际运行观测”；静态单点、缺失数据不能伪装成完整 rollout。

坐标轴翻转、yaw 单位、尺寸、global time、跨平台 actor-role 绑定需要变换测试。仅看 2D 不意味着可以忽略车道连接或把上下层道路误当成碰撞；拓扑用于正确解释平面证据，不新增 3D 质量评分。

同一个 CARLA 版本/地图/控制器可满足时优先统一；方法确有版本约束时显式登记平台 profile 和可比范围，不强装到同一 Python 环境，也不把平台差异隐去。

## 12. 分发给 Codex 的工作包

前缀 **BW** 仅用于本轮 benchmark/workbench 改造，不与 BusScene 原有 WP 编号混用。每包必须交回：实际改动文件、测试命令及结果、未验证事项、兼容性变化、回滚方式。不得把本计划的建议状态写成“已完成”。

### BW-00｜锁定行为基线与真实标注负担

**优先级/依赖：** P0；无。

**范围：** 当前 workbench、human workflow、oracle 生成器、包构建配置和测试；维护者授权可读的真实开发题包/旧 checkpoint。涉及私有题包须由实际执行环境确认访问权。

**实施：**

- 建立重构不变项与兼容性 ADR，记录 query/oracle/protocol/guide/assignment 版本。
- 查清两套包目录的来源关系、导入优先级和 `schema.py` 差异，不先删除任何目录。
- 从获准开发材料建立固定回归样例：accept、permitted、exclude、modify、split、merge、新增、support 源错误、CPD 修订、继承、冲突恢复。
- 记录真实用户操作：活动耗时、页面切换、展开、重复确认、自由输入、失败校验、暂存原因。不得仅从 serialized slot 数推点击负担。
- 分开统计“人的判断耗时”“界面延迟”“语义材料不清”三种成本。

**交付：** `docs/benchmark/WORKBENCH_REFACTOR_BASELINE.md`、兼容性样例、操作测量定义、重构 ADR。

**验收：** 有真实基线或明确 unknown；已有测试可重复执行；没有 metrics/题库变更；不能读取的数据明确登记，不伪造。

### BW-01｜抽离可移植的审阅服务与显式上下文

**优先级/依赖：** P0；BW-00。

**范围：** `query_review_workbench.py` 的 context、form converter、session 业务；`paths.py`；脚本入口。UI 展示暂不重写。

**实施：** 提取无 ipywidgets 依赖的 context/service/compiler/store；显式传入源资产和 workspace；复用现有 formal validator；旧 notebook 调用新服务。48/252 是当前 manifest 校验内容，而不是所有 suite 的硬编码路径假设。

**交付：** 纯 Python 审阅服务、旧入口兼容适配、路径迁移说明。

**验收：** 同一旧表单得到相同 canonical payload；离开仓库根目录能处理显式 source；不向 site-packages 写状态；未知 workspace/source 直接报可解释错误。

### BW-02｜完整可读语义投影、词典与上下文指南

**优先级/依赖：** P0；BW-00，可与 BW-01 并行。

**范围：** `_human_token`、`_human_atom_statement`、`_human_atom_evidence`、字段编辑定义及指南。

**实施：** 为现有 predicate 建立显示/编辑登记表：参数、对象、数量/极性、关系、单位、时序/触发等必须可见；原文依据与元数据建议分开；未知字段不静默丢失。词典同时驱动卡片、提示、练习、错误描述。展示层不得为缺少信息的 draft 自动编造细节。

**交付：** presentation registry、原文证据显示、简明中文指南和练习题包。

**验收：** 已支持字段全部可解释；参数变化必须体现在显示差异里；未知字段显著提示并禁用整题快捷确认；排除/允许/禁止正反例全部通过。

### BW-03｜建立“提案—人工修改—明确确认”的交互模型

**优先级/依赖：** P0；BW-01、BW-02。

**范围：** 新 review model、compiler、会话格式；不改 metrics 公式与 finalizer 的闭合要求。

**实施：** 机器 proposal 独立存储；提供当前题/可见语义组显式确认；由 compiler 展开全部旧 required fields；自动处理 source-target IDs 和派生检查；记录确认内容 hash、覆盖条目和修改来源；修改已提交内容恢复未提交。

**交付：** 新交互状态契约、正式 payload 转换、确认收据、协议差异 ADR。

**验收：** 仅打开页面/预填/继承/自动保存产生零 human gold；已确认内容逐条完整映射；未知/疑问不能全选通过；新旧等价操作生成语义等价 payload；真实人工责任未被机器默认值替代。

### BW-04｜离线 HTML 主入口与可委托题包

**优先级/依赖：** P0；BW-02、BW-03。

**范围：** 参考 `annotation/annotation_ui.html`，新增 query-review task frontend、packet builder/importer。

**实施：** 模块化开发、单文件离线交付；常驻 query、完整简明草稿、疑问区、逐题队列和一个主提交动作；导入/导出不需 Python；无 CDN/实时 LLM；browser schema 与 Python schema 分工；备份、恢复、独占编辑/冲突保护、准确信息保存状态；维护者导入时运行 canonical 检查。

**交付：** 自包含 query-review HTML、维护者导出/导入命令、少量开发试标包。

**验收：** Windows 主流浏览器断网打开并续做；无需 CARLA/Jupyter；主流程除必要备注/语义数字外无需代码式输入；损坏/过期/他人备份不会覆盖；浏览器已提交不冒充正式 gold；生成器输入没有收到题包内容。

### BW-05｜把 Agent 提示变成可审阅的具体修订提案

**优先级/依赖：** P1；BW-02、BW-03，可与 BW-04 并行。

**范围：** `oracle.py`、`agent_query_review.py`、已有 draft scripts；不直接覆盖已绑定旧 oracle。

**实施：** 依据当前 query 生成结构化提案与文本证据；针对角色猜测、事件/状态/触发混淆、列表顺序、风险元数据和表述强弱提供具体变化；未解决原因结构化；必要时离线有限调用 LLM，记录来源与配置。人工仅接受/改正提案，不需手写 proposed_oracle。

**交付：** proposal schema/generator/validator；旧 draft 到提案的适配器；质量和覆盖报告。

**验收：** 每条提案绑定精确源；机器解释不能伪装成人工依据；无证据条目保持未解决；原 atom 处理映射无静默遗漏；没有在 UI 中实时等待模型。

### BW-06｜完整 surface 差异与安全继承

**优先级/依赖：** P1；BW-03、BW-05。

**范围：** `_inherited_surface_form`、现有 batch 逻辑、新 diff 模块。

**实施：** 对数量、否定、角色、关系、触发、顺序、修饰语和数值分别建立差异；人工新增条目必须有 target-surface 适用依据；不明差异回退完整审阅；一个 intent 导航下保留每个 surface 的独立提交。批审仅为可选工具、split-local、无覆盖、可追溯草稿撤销。

**交付：** surface_diff、继承适用性规则、差异优先视图、批审来源记录。

**验收：** precise 专属要求不进入 vague 的确认答案；相同 atom ID 不足以免除表述核对；已有例外/已完成条目不被覆盖；批审/继承单独产生零 gold；300 subject 不缩减为100 gold。

### BW-07｜疑问队列、源问题反馈与安全迁移

**优先级/依赖：** P0 基础暂存、P1 完整迁移；BW-03。

**范围：** session workflow status、issue package、旧 checkpoint/import/migration。

**实施：** 显式 defer/requires_source_fix；结构化原因；保留中间编辑且允许下一题；维护者接收可定位源的问题包；旧记录只读备份；修源产生新绑定，精确标记需重审的内容。业务语义未改变的 UI 修订不得无说明清空所有进度；语义变更不得偷换旧 hash 保留 complete。

**交付：** 疑问队列、问题单导出、迁移工具和影响报告。

**验收：** 暂存不计 gold、不消失、不绕过全套定稿；旧备份错版本被保护；来源有变重审；无关记录能否保留有确定规则和测试；出错保留原件。

### BW-08｜CPD 专家准备与普通审阅投影

**优先级/依赖：** P1；BW-02、BW-03。

**范围：** CPD 展示/编辑和 policy catalog；既有 CPD 评分保持。

**实施：** 将共享维度定义、selector/分箱/跨平台说明集中管理；普通 query 只展示允许变化与文字锁定内容；异议进入专家编辑/疑问队列；最终仍产生现有 CPD 与 required-check 双字段一致的结果。

**交付：** catalog 可读投影、query policy editor 精简视图、既有专家编辑兼容。

**验收：** 不让标注者每题配置相同 selector；不同 query 的 policy 不自动视为相同；不引入 baseline 特定优惠；既有 CPD 校验与计分回归不变。

### BW-09｜单一源码与完整 wheel 交付

**优先级/依赖：** P0 可安装最小版，P1 完整发布；BW-01、BW-04。

**范围：** 两套源码树、`benchmark/pyproject.toml`、入口、schema/query/HTML/CSS/guide 资源。

**实施：** 按 BW-00 的来源分析收敛唯一可编辑实现；将维护者 CLI 纳入安装包；声明资源与 notebook optional extras；从空目录运行已安装 wheel；方法源码和私人 review 资产不混入发行包。

**交付：** wheel、版本清单、干净安装测试、兼容入口退役说明。

**验收：** 在不含 ChatScene checkout 的环境执行 paths、题包导出、提交导入与校验；核心不要求 ipywidgets；CSS/HTML/指南实际可读；导入文件来源确为 wheel；无根目录同名包遮蔽。

### BW-10｜Baseline 适配与 2D 证据接口验收

**优先级/依赖：** P1；BW-09；可与后续复杂标注功能并行。

**范围：** 公共 MethodAdapter/platform contract、ChatScene wrapper、其余方法的薄 wrapper；另三个方法需在其实际获准 workspace 内实施。

**实施：** 先无仿真 fake adapter 验证隔离与失败路径，再回归 ChatScene；第二个真实方法优先选入口/原生产物有差异的方法来证明可迁移。逐个登记原生产物、版本、运行profile、交互、冻结点与日志。共享已有 semantic evidence，2D viewer 只投影，不另建评分器。

**交付：** adapter SDK/示例、每方法接入清单、第二方法的真实闭环报告、其余方法分别列实现与未验证状态。

**验收：** 公共包在至少两种真实接入中无需分叉；query-only 实际输入可检查；未借 oracle 修复或补参数；失败/拒绝/超时保留原口径；平台/Scenic 版本和交互差异被披露；2D 时间、朝向、尺寸、mask、角色和产物关联一致。单个 mock 通过不能等同所有 baseline 支持。

### BW-11｜兼容性、浏览器与实际可用性验收

**优先级/依赖：** P0 试标、P1 全量候选版；BW-04、BW-05、BW-07；BW-06/BW-08 上线时各自进入验收。

**范围：** 现有 Python 测试、共享契约向量、浏览器 E2E、人工开发试标。

**实施：** 用第13节矩阵验证；选择12–24条开发/合成样例覆盖全部关键困难，不依赖固定小样本得出统计泛化；实际试标比较老/新流程，交叉分配相当难度且不同的题，避免同一题重复记忆。试标不是正式 gold。

**交付：** 自动测试日志、真实操作报告、未通过项和录制/步骤证据、上线或退回结论。

**验收：** 安全与计分不变项全部通过；机械动作和有效耗时有改善；植入的已知语义错误能被发现，不能以更快接受错误换速度。无真实试标不得写“标注效率已提升”。

### BW-12｜发布、续标与迁移手册

**优先级/依赖：** P1；BW-09、BW-11；完整跨方法可移植声明还需 BW-10 的第二方法验收。

**范围：** 文档、release manifest、题包/进度迁移说明、现有 notebook 兼容入口。

**实施：** 发布三个入口文档：标注者一页指南、维护者手册、baseline 接入手册；冻结通过验收的 UI/guide/proposal/compiler 版本；逐步迁移旧 assignment；保留旧 UI 只读回看，试点后再停止维护旧主界面。

**交付：** 可安装发布包、示例包、真实迁移报告、功能/限制矩阵。

**验收：** 新标注者只读一页指南能开始；baseline 接入不复制评分与标注实现；旧进度可追溯；没有把 CARLA-stage 产物改名冒充最终跨平台 freeze。

## 13. 测试与验收矩阵

| 类别 | 必测场景 | 判定 |
|---|---|---|
| 计分不变 | 既有固定样例、supported/unsupported、失败分母、CPD、IEC 主/诊断 | 同一冻结输入下结果与基线一致；明确禁止 UI 重构改公式 |
| 转换完整 | accept/reject/permitted/modify/split/merge/add；派生六检查 | 每个源要求都能追溯；旧正式 validator 接受合法、拒绝非法 |
| 确认真实性边界 | 仅加载、自动建议、批审、继承、保存、defer | 全部不产生 gold；仅合法明确提交才进入后续 finalizer |
| 编辑生命周期 | 已提交后改值、撤销、返回、重复导入 | 改动恢复待提交；不重复计数；不丢原记录 |
| 语义显示 | 参数变更、否定、数量、角色、时序、未知字段 | 人看到的内容与 formal proposal 一致；未知不静默隐藏 |
| 表述隔离 | precise 独有数值/方位/因果/新增要求；partial/vague | 不越界继承，不把差异不全伪装成已完全检查 |
| 状态恢复 | 断网、刷新、存储拒绝/配额、双标签页、旧备份、错reviewer | 清楚区分磁盘/浏览器/内存状态；不能假称已持久化；冲突不覆盖 |
| 文件可信边界 | 更改题目、注入字段、重算非可信hash、错source、旧guide | 维护者以原始可信源重建并验证，拒绝自洽但伪造的提交 |
| UI 输入安全 | query/notes 中 HTML、脚本闭合符、恶意文件/JSON | 作为文本安全显示；不执行；packet escaping 与allowlist检查 |
| 实际交互 | 鼠标/键盘、IME输入、焦点、浏览器不同尺寸 | 无须读 JSON；快捷键不抢输入、不误提交；错误能定位 |
| 包安装 | 空目录、wheel、无源码路径、只读site-packages | 全套审阅准备/回收可用；资源齐全；不依赖 notebook |
| 方法适配 | 超时、失败、拒绝、产物不存在、重试、版本不同 | 维持已冻结口径；不补场景、不选最好输出、不传oracle |
| 2D 证据 | 轴反射、yaw/单位、尺寸、缺测、计划vs观测、global time | 几何与时序一致、来源可追溯、没有伪造完整轨迹 |
| 性能 | 打开/切题/保存/校验/恢复，大题包 | 先测瓶颈；如缓存按源/内容hash失效，提交与finalize仍权威全验 |

建议试标目标（尚未实测，不是已实现收益）：

- 正常无异议题不再逐 atom 重复点“正确”；完整审阅后用一次明确的本题确认提交。
- 相当难度任务的机械操作数下降约50%，单题活动耗时中位数下降至少30%；分别报告简单/复杂题与p90，不用平均值掩盖极端难题。
- 已知关键语义错误不得因简化 UI 被漏掉；保存/恢复、源错误、未完成闭合等阻断项零妥协。
- 使用帮助次数、自由输入量、校验失败后返工和疑问积压应报告；不把“unknown减少”单独作为成功指标。

阈值可以在 BW-00 看到基线后预先校准，但不能看到新 UI 的结果后再选择有利阈值。小规模试标只支持工程上线判断，不支持论文级统计优势声明。

## 14. 实际推进顺序

**第一段：恢复可用性，而不是等待全部架构完成。** BW-00 后并行 BW-01/BW-02；随后 BW-03/BW-04，纳入 BW-07 的基本暂存与现有草稿导入；提供一个只覆盖 query oracle 的离线试点。此时高级 split/merge/CPD 可以继续使用旧专家入口，不强迫首版一次重写所有控件。

**第二段：减少真正的语义返工。** 并行完成 BW-05、BW-06、BW-07 的迁移部分和 BW-08。先改善草稿和文字解释，再验证跨表述的差异优先；不把全部 ambiguous 条目默认为正确。

**第三段：证明跨仓库交付。** BW-09 干净 wheel、BW-10 第二方法接入可以与第二段并行。标注包可早于真实 CARLA rollout 准备；native executability、平台校准和最终跨平台 freeze 不能因此被宣称完成。

**第四段：验收后续标。** BW-11 实际试标通过再扩大题量，BW-12 发布并迁移历史进度。无需等所有 baseline 训练/仿真完成才确认方法无关的 query oracle，但不能跳过现有冻结依赖。

最终交付不是“功能更多的 workbench”，而应是：

> 同一份可信评分标准只审阅一次；标注者面对的是文字和要求，不是内部 Schema；各 baseline 只接自己的原生入口；所有正式计分仍由同一版本的公共包完成。
