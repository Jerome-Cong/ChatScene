# Chat2scenic / Text2Scenario 公共开发运行接入

运行开始于 2026-10-14，延续至北京时间 2026-10-15。本报告只记录当前公开实现及显式替换模型的开发验证，不代表论文原配置复现、正式评分或最终发布。

## 结论

两种方法已接入同一个 query-only 生成入口和公共运行记录流程。Text2Scenario 的完整开发网格已完成，5 次调用均在原 notebook 缺失分支处失败，全部失败保留；Chat2scenic 的 5 次真实生成均完成且生成链校验通过；3 条编译通过，2 条产物分别通过全部 5 个 SV seed 和 NE。公共初始状态诊断有 2 条通过。全部失败保留，没有正式分数。

只检查 1 条公开开发 query `BSG_DEV_v0_2_S01_precise`，每种方法固定调用 5 次。这里的“生成完成”指产生可经既定 ego 标准化接受的原生产物，“编译通过”指原生编译器接受，“SV 通过”指初始场景通过公共几何检查，“NE 通过”指公共控制器执行达到声明的终止条件；这些都是工程状态，不能代替 query 语义正确性，也不形成全题库性能结论。失败次数的分母始终为预声明的 5 次调用，不删除异常或改用诊断样例填补。

## 已完成内容

### 固定方法配置

- Chat2scenic 的源版本为 `54264e4`，保留公开工作流，解释后只进行一次机械 `confirm`，没有语义反馈。五个 agent 均显式使用现有 `.env` 的 `DeepSeek-V4.1-Flash`，替换原 Gemini 配置；temperature 0.2、top_p 0.9、max_tokens 16384、每个 API 请求超时 180 秒、最多两次 API 重试。方法调用总体超时仍固定为 1200 秒。API 重试不是生成失败后挑选结果。
- Chat2scenic 保留 `all-MiniLM-L6-v2` CPU 检索，完整本地模型目录纳入冻结清单。公开 Milvus 快照恢复至独立 2.6.3 standalone 实例，端口为 loopback 19531；代码集合 922 条、384 维。公开源码已注释文档检索调用，不声称本次运行了两个检索通道。
- Text2Scenario 的源版本为 `5ac1aa93335bb9f173c24779f3d2f2dc351ad788`，按用户选择只使用公开演示版。任意 query 替换原固定雾天/左转提示及特殊偏向，保留原 cosine top-8 检索。生成模型为 `DeepSeek-V4.1-Flash`、temperature 0.3，独立 `.env.embedding` 实际模型为 `GLM-Embedding-3`；真实嵌入请求观测为 2048 维，元素全部有限。它改变了原 `text-embedding-ada-002` 检索配置，不能标成原论文配置。
- Text2Scenario 原 notebook 的代码单元及模板保持原样，在新目录执行；原 XOSC、ego route JSON 分别保留，再以 `carla_xosc_bundle_v0.1` 传给公共包。两套凭据只在 harness 边界加载，未纳入源码/环境绑定清单。原 RAG 脚本中的硬编码密钥已移除；历史暴露的密钥仍需由账户所有者轮换。

### 固定运行配置

Chat2scenic 使用官方 Scenic 3.0.0 wheel、CARLA 0.9.15、Shapely 2.0.7，以及显式 `mode2D=True` / `use2DMap=True`。逐文件核实后确认官方 wheel 已含轨迹跟随和交通灯等扩展，因此没有移植 ChatScene 扩展。Scenic 源码 clone 仅用于核实，运行仍使用官方安装包。

Text2Scenario 使用 Python 3.8、upstream ScenarioRunner v0.9.13 和匹配服务端分发的 CARLA wheel。本机实际 client/server 构建都为 `0.9.13-2-g0c41f167c-dirty`；除了基准版本 0.9.13，仍要求完整构建标识相同，并登记服务器分发树及实际 OpenDRIVE 字节。不能把该分发标成未经修改的官方发布二进制。

公共处理只修改既定 ego blueprint、长和宽，不改道路、演员、动作、地图名称或生成约束。Scenic 地图文件在独立目录按原相对层级暂存，原 Scenic 字节不变；XOSC 仍交给原生 ScenarioRunner。两种类型与对应 native backend 严格匹配，无法接受的产物记录失败。

### 真实验证和记录

Text2Scenario：真实 5 次 query 输入、GLM 嵌入检索和模型参数生成后，原 notebook 均报 `NameError: name 'tree' is not defined`。公开演示版没有处理本次生成的 `cyclist` 类型；没有将它补成 `bike`、改模板或删除该对象。生成链验证通过，公共 runtime 保存 5 条不可运行记录：编译、SV、NE 均跳过，初始观察均为 `not_applicable`，5 条持久化记录重载验证通过。这里没有成功的 Text2Scenario 方法 rollout。

Chat2scenic：5 次真实生成均为 complete，生成链校验通过，原生产物及 native 工作区均保留。按 repetition 0--4：编译状态为 passed/failed/failed/passed/passed；0 的 5 个 SV seed 均因 None.orientation 失败，1/2 因生成代码未定义 egoSpawnPt 编译失败后跳过，3/4 均通过全部 5 个 SV seed。最终独立服务网格的 NE 为 skipped/skipped/skipped/passed/passed：3 执行 300 步、30 秒达到时间预算，4 执行 94 步、9.4 秒达到原生终止条件。初始观察状态为 failed/failed/failed/passed/passed；前 3 条保留原生成/采样错误。5 条 runtime 重载校验通过。

最初复用同一 CARLA 服务的网格中，3 完成 NE 后服务崩溃，4 因 10 秒 RPC 超时失败；这是基础设施污染，不作为方法失败解释。新增显式 --isolate-carla / --graphics-adapter 逐产物独立服务模式，对同一 5 个产物、同样 5 个 seed 重新运行，未重调模型、未改产物。原完整网格、导入失败/旧预算失败和服务日志保留在原目录，新结果位于独立目录。5 个受控服务均确认退出；本机 UE4 在关闭时仍可能以 Signal 11 结束，exit_code=-11 如实记录，不把它改成正常退出。隔离使这个关闭问题不再污染下一个产物。

初始诊断使用正常包导入共享 extract，避免旧 fixture 动态导入路径缺失；其独立开发 profile 为单 CPU、2 GiB、20 CPU 秒、30 秒总体时限、native 子进程 20 秒时限、8 MiB stdout/stderr、16 MiB 文件及 64 FD。NumPy 默认线程在 2 GiB 下导入失败已复现，单 CPU 可避免；冷地图解析可能超过正式 worker 原 5 CPU 秒。本次不改变正式 worker/预算，所有成功、失败和跳过条目都带 formal_extractor_budget=False。当前成功观察不能冒称正式提取门禁通过。

独立诊断不纳入上述 5 次分母：

- Chat2scenic 的早期真实单次工作流生成原生代码，编译通过；SV seed 0/1 因演员越出地图失败，2/3/4 通过。NE 在第 0 步因生成行为的 `object is not in a lane` 被 Scenic 拒绝；保留原约束和位置。此前一次服务进程收到 Signal 11，新实例同 seed 详细诊断复现行为拒绝且服务存活，两个事件单独留档。
- Text2Scenario 原 notebook 的固定样例 XSD 编译通过，但 5 次 SV 均无法生成全部演员。按原 XML 独立复现确认：ego 成功、另一车因 `Spawn failed because of collision at spawn position` 被拒绝。XML 的 `h=270` 被原生解析成弧度后对应约 15469.86 度，地图 hash 与登记 Town05 一致；没有修改朝向、位置或删演员。
- ScenarioRunner 官方跟车、行人横穿样例经既定 ego 标准化后，也被公共 footprint 检查拒绝；保留这些失败，不放宽检查条件。另行定义的单车平台 smoke 在车道中心放置既定 ego，经原生 XSD 编译、5 次 SV 和 300 步/30 秒公共控制器 NE 全部通过；记录校验通过。这个场景从单车天气样例定义时将 lane offset 1.0 改为 0.0，route 字段仅为控制器不使用的载体占位。它没有方法生成、没有 query 对齐，不能替代真实 5 次生成失败，也不是未修改的官方样例通过证据。

语义诊断仅使用产物及已登记的解释器、地图、编译选项，观察初始位置；Scenic3 不读取行为参数中的计划路线，XOSC 不把动作声明当成观测到的运动。动态缺测保持未知，不据此新增行驶/让行事实或正式分数。

## 遇到的困难及处理方式

- Python/库版本不兼容：使用独立 `runtimes/<runtime>/.venv`；Scenic3 的 Shapely 2.1 points ABI 不兼容已复现，固定 `<2.1`。Python 3.8 的 frozen executable 同时保留其 origin-relative `libpython`。
- Chat2scenic 原配置仍硬编码 Gemini：共享模型工厂保留原默认行为，只在明确 override 时使用现有兼容模型，实际 agent profile 落盘。4096-token 探针的 reasoning 用完预算但 content 为空，16384-token/60 秒探针超时；旧失败保留，当前固定 16384/180 秒。
- 公共 command 输入原本误接为 JSON：修为默认读取原 UTF-8 query；JSON 只由诊断显式选择。早期两种方法各 5 条输入协议失败保留，未冒称为真实模型调用结果。
- CARLA 完整版本后缀被误拒绝：修为基准版本及完整 client/server build 同时核对；新增版本不匹配反例。旧错误记录保留，不重写成通过。
- 服务生命周期：端口空闲与 ready 校验，仅核对自建 PID/argv 后关闭；启动、保存、清理异常作为 infrastructure outcome 留档，不伪造方法失败或丢响应分母。resume 不启动服务、不执行缺失 canonical record，也不抹掉旧清理失败；每条服务 receipt 与隔离声明一致。
- 凭据边界：generation 与 embedding key 分别传递，stdout/stderr 若出现任一 key 则抑制输出；无法关闭的捕获流直接全部抑制，防止部分字节漏出。四种独立 key/流组合和未关闭流反例已加入回归。

## 未解决阻碍与验收边界

当前公开演示版的类型分支不一致、角度字段和原生成/采样错误是实现或产物限制，不能通过生成后语义修补消除。现有工程接入允许完整记录这些结果；它们不是成功 rollout 的证据。当前网格不建立科学比较，也不宣称 Text2Scenario 论文完整版本已接入。

正式人工 gold、提取器/平台校准、受控文件读取边界、完整 Python 环境封闭登记、兼容内核 Judge、完整 2D 动态证据关联及最终发布门槛保留。Judge 内核按用户要求未处理，没有替代其正式门禁。旧 wheel 候选未随本次源码接入重新构建，不能沿用旧 wheel 清单宣称包含这两个新运行类型。

定向验证已通过主仓库 native/Scenic3/XOSC/提取器、进程归属与完整失败网格 42 项，以及源仓库 Chat2scenic 3 项、Text2Scenario 4 项。旧 generation/runtime 全量套件仍含旧机器绝对路径、退役 schema 等失败；没有宣称全量测试通过。真实完整网格及重载已通过；CARLA 停止且新方法/runtime worker 调用被哨兵拒绝时，两种方法的 generation/runtime --resume 均通过。Chat2scenic 的 36 个生成原始文件、90 个 runtime 原始文件及 Text2Scenario 的 21 个生成原始文件、5 个 runtime 原始文件，内容摘要与 mtime 均未变；aggregate 汇总和独立诊断可重写，不纳入这个原始文件统计。

## 下一步与复现

两种方法的本轮开发接入已完成，正式扩大题库前按现有口径处理 gold、校准、正式提取资源预算及 Judge。独立服务与开发初始观察预算需要作为实际运行配置记录，不得省略差异。

主仓库四个入口分别负责准备生成配置、执行完整生成网格、准备原生运行配置、执行并重载运行网格。配置和输出目录必须是新的；源码、环境或原生资源变化后准备新配置，不覆盖旧记录。Text2Scenario 的生成 runner 另需 `--embedding-env ../Text2Scenario/.env.embedding`。

```bash
chatscene/bin/python-frozen scripts/prepare_public_baseline_generation.py --method METHOD --output NEW_GENERATION_CONFIG
chatscene/bin/python-frozen scripts/run_public_baseline_generation.py --config-dir NEW_GENERATION_CONFIG --output NEW_GENERATION_OUTPUT
chatscene/bin/python-frozen scripts/prepare_public_baseline_runtime.py --method METHOD --output NEW_RUNTIME_CONFIG --server-root SERVER_ROOT --maps MAP_DIRECTORY --port RPC_PORT
chatscene/bin/python-frozen scripts/run_public_baseline_runtime.py --config-dir NEW_GENERATION_CONFIG --runtime-config-dir NEW_RUNTIME_CONFIG --generation-output NEW_GENERATION_OUTPUT --output NEW_RUNTIME_OUTPUT --isolate-carla --graphics-adapter AUTHORIZED_VULKAN_INDEX
```

`METHOD` 为 `chat2scenic` 或 `text2scenario`。隔离选项要求已授权 GPU 的明确 Vulkan index；本机 physical GPU 1 对应 index 2。普通模式使用已有服务，resume 不启动任何服务。凭据文件不进入命令日志、bundle 或 Git；API 不可用/服务停止时只用 `--resume` 验证已完成记录，不新增生成或运行尝试。当前原始材料保存在忽略入库的 `.carla-runtime/*-20261014-v7`、`.carla-runtime/chat2scenic-public-runtime-isolated-20261015`、`.carla-runtime/baseline-preflight-20261014` 及各 native 诊断目录，原临时工作区保留并额外复制至项目目录。
