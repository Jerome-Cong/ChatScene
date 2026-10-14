# Baseline 公共接入手册

公共包只向 `MethodAdapter.generate(query_text, workdir)` 传一条英文 query 和新工作目录。最小入口是 command adapter：stdin 接收 UTF-8 query，产物写到相对 `artifact_path`；stdout_json_v0.1 只输出 disposition 与可选 message。失败、拒绝、澄清和超时保持原状态，全部保留在预声明的调用分母中。不得按 oracle 补参数、改道路/演员/动作，或重试后只挑成功结果。

## 当前接入状态

- **ChatScene**：保留原 Scenic 2 及旧 snapshot/bwrap 入口，已完成兼容 API 配置与运行路径重绑定。实际查询仍可能因采样或生成质量失败，运行入口可用不等价所有场景生成成功。
- **TTSG**：原生 `carla_native_json_v0.1`、CARLA 0.9.15；已完成公开开发 query 的 5 次真实生成、公共 SV/NE 与记录重载。缺失的骑行者原样保留，没有报告正式语义分数。见 [TTSG 接入报告](BW10_TTSG_NATIVE_ADAPTER_REPORT.md)。
- **Chat2scenic**：原生 Scenic 3、官方 3.0.0 wheel、CARLA 0.9.15、显式 2D 编译选项；一次机械 confirm、现有兼容模型、原代码检索库。真实 5 次生成均完成；3 条编译通过，2 条各通过全部 5 个 SV seed 及 NE，初始诊断有 2 条通过。采用逐产物独立服务，旧服务崩溃及原生成失败保留。
- **Text2Scenario**：公开演示版，原 notebook/template 转 XOSC 和 ego route，显式 `carla_xosc_bundle_v0.1`，原生 ScenarioRunner/CARLA 0.9.13。真实 5 次输入均因原生成器缺少 cyclist 分支而失败，完整生成链及 5 条不可运行 runtime 记录已校验。没有成功的方法 rollout，不能称论文完整版本复现。

后两者的模型/运行库差异、结果边界和复现步骤见 [公共 baseline 运行报告](BW10_PUBLIC_BASELINES_RUNTIME_REPORT_2026-10-14.md)。MetaDrive 是平台，不是第二生成方法；Talk2Traffic 不在本轮范围。

## 新方法接入边界

配置按 method_config.schema.json 准备新的 implementation bundle，绑定方法源码、解释器、环境控制文件、原生资源及公共 harness。真实凭据通过受控环境传递，不纳入文件清单。两套 API 可以独立提供 generation 和 embedding key；捕获到任何 key 的输出直接抑制，未关闭的流也全部抑制。

Scenic2、Scenic3、TTSG native JSON 和 XOSC 使用显式产物类型，不能只改后缀冒充可运行输入。平台 backend 必须与产物类型匹配；静态地图与实际 CARLA 地图字节一致。原 Scenic 相对地图目录可机械暂存，原源码字节不变。ego finalizer 只执行既定代理模型及尺寸标准化，不提供道路、演员、动作或生成约束修补。

CommandAdapter 限制输入、环境、输出字节和时长，但普通 command 子进程没有文件系统沙箱；query-only 参数不能证明它读不到私人文件。正式运行需受控容器/挂载及现有冻结、校准、Judge 门禁。当前 development 验证不豁免这些条件。

## 运行与证据

使用主仓库 prepare_public_baseline_generation、run_public_baseline_generation、prepare_public_baseline_runtime 和 run_public_baseline_runtime 四个脚本，分别准备/冻结生成、运行完整网格、准备/冻结 native backend、执行与重载全部记录。Text2Scenario 的 generation runner 另需独立 embedding env 路径。详细参数用各脚本 --help 查看。CARLA 复用服务的清理崩溃可用显式 --isolate-carla --graphics-adapter 已授权索引隔离；服务只按自己的 PID/argv 关闭。

环境、源文件、地图、模型目录变化后准备新配置，不覆盖旧失败和旧绑定。完整网格结束后，服务停止或 API 不可用时可 --resume 复验已完成的原始记录，不能新建 attempt 或重新生成取代旧结果。

Scenic3 和 XOSC 当前观察范围为初始状态，行为参数和动作声明不能当成动态观测。初始诊断另用明确 development 资源配置（单 CPU、2 GiB、20 CPU 秒、30 秒时限），正式 worker 原 5 CPU 秒及其他门槛未改；动态缺测保持未知；独立 native / synthetic platform smoke 必须单独标记，不替代真实 query 生成结果，不进入科学比较的调用分母。

本轮新增支持以源码为准；旧 wheel 候选未重新构建，不能用旧发行清单宣称已包含新产物类型。正式评分、完整 2D 动态证据、跨平台校准、人工 gold 和最终发布仍沿用既定验收条件。
