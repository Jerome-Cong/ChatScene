# BW-10 TTSG 原生接入与运行报告（2026-10-13）

## 结论

TTSG 的真实模型→原生状态→公共编译/场景检查/固定控制器执行→语义诊断→记录重载与恢复链路已打通。2026-10-13 完整开发网格及离线恢复验收通过。本文的工程验证不代表正式方法排名、完整语义准确率或最终发布验收，Judge 内核按用户指示暂不处理。

## 已完成内容

- 入口为 `../TTSG/benchmark_query_adapter.py`，仅从 stdin 接收一条公开 query；模型 API 来自 ChatScene 的既有 `.env`，凭据仅在内存和显式环境传递，不写配置或仓库。模型 `DeepSeek-V4.1-Flash`、原温度 0.9、每阶段最多 3 次尝试、API 超时 60 秒与 SDK 重试 2 次均登记在本机配置中。
- 主产物为显式登记的 `carla_native_scene_v0.1` JSON：实际 CARLA 版本、OpenDRIVE、天气、真实 actor 蓝图/尺寸/姿态、逐步状态与控制、有效观测标记和真实终止信息。三阶段规划、道路信息和所有已有相机文件留在独立原始目录；不把图片 tar 改后缀，不截断主产物，不用 oracle 补道路、对象或动作。
- 新观察配置最多记录 30 秒实际仿真，每步 0.1 秒。`native_program_complete` 表示原程序真实提前结束；`native_observation_budget_complete` 表示完成 300 个物理步后停止本次规定观察，不宣称原无限演示自然结束。cleanup 的每个既有 tick 也真实记录；没有推进时间时不补时间戳。新观察回调失败仍完成全部原清理，再报告失败并恢复异步模式。
- 公共 finalizer 仅将 ego 蓝图、长度和宽度改为既定 Impala/5.33/2.10；原始道路、位置、actor、控制及轨迹保持不变。公共 worker 读取冻结地图与完整已安装 CARLA wheel 登记，执行编译、五次原生场景检查和固定控制器的 300 步/30 秒运行；JSON 观察器进入现有无 query、无 oracle 的语义提取路径，没有新增评分器。
- 本机 TTSG 使用 Python 3.10、CARLA 0.9.15 + 官方 AdditionalMaps、Scenic 2.1.0b4 和既有可信道路缓存。ChatScene 的 CARLA 0.9.13/Scenic 环境保持独立。原生场景检查的五次试验是对同一已捕获实现的确定性确认，与 Scenic 的五次独立采样不同；不能把它们解释为相同采样过程。

复现入口（开发验证，执行会调用真实 API，需要可用 CARLA 窗口）：

```bash
chatscene/bin/python-frozen scripts/prepare_ttsg_benchmark.py --output .carla-runtime/ttsg-config-NEW --pilot
chatscene/bin/python-frozen scripts/run_ttsg_benchmark_pilot.py --config-dir .carla-runtime/ttsg-config-NEW --output .carla-runtime/ttsg-generation-NEW
chatscene/bin/python-frozen scripts/run_ttsg_benchmark_runtime.py --config-dir .carla-runtime/ttsg-config-NEW --generation-output .carla-runtime/ttsg-generation-NEW --output .carla-runtime/ttsg-runtime-NEW
```

每次配置必须使用新目录并重新核对实现；生成和执行的 `--resume` 仅验证并复用完全相同的已落盘记录，不重采样挑选成功结果。`--pilot` 选择一条公开开发 query 的全部 5 次重复；这只有一个 query，不能支持全题库性能或统计泛化结论。原始工作区保留选项仅用于开发；正式生成不接受该诊断选项。

## 遇到的困难及处理方式

旧原生图像归档超过 64 MiB 时明确失败，原件保留；新主产物仅承担可执行状态，图像仍完整留在原始目录。原代码第 10 帧之后才保存图片，因此真实短场景可能没有 PNG；原生 JSON 模式不再用这个现象拒绝真实完整状态。

首次 30 秒配置发现原 cleanup 可推进多个真实时间步，却只记录最终状态，严格校验据此拒绝。现为各 manager 与相机的原有 tick 添加只读回调，不增 tick、不改清理顺序、不修旧时间戳。另修复首帧回放多出的物理步、包导入、坏/过深 JSON 中断整轮、wheel 登记在 schema/请求/重载中的遗漏，以及采集失败遗漏清理。

历史目录 `ttsg-real-query-20261012`、`ttsg-benchmark-pilot-20261012-v2`、`ttsg-benchmark-pilot-20261012-budget30` 和旧日志保留。旧无限演示网格包含一个真实 timeout、一个因相机门槛失败的输出和中断现场；首次预算网格因时序校验异常未完成，不改称成功，也不与新配置网格合并。组合 CARLA 目录中的两个 SQLite 链接先备份，再以目标原字节的普通文件替换，以满足原有目录绑定；基础下载包未覆盖。

## 未解决门槛与验证边界

正式评测仍需真人 gold、平台/提取器校准、受控方法读取边界及完整 Python 运行环境封闭登记；既有正式 validator 未放宽。Judge 的内核校验与原未通过记录保留，正式评测和最终发布前仍要在兼容环境补齐。

语义诊断的 `complete_categories` 保持空列表，不把未观察到的内容自动判为不存在。现有共享提取器会对所有对象使用共同有效观察前缀，以避免旧规则延长已经消失对象的轨迹；因此不会声称覆盖前缀之外的事件。完整有效标记和真实时间仍保存在原生产物中。原生控制录制与公共固定 ego 控制的回放分别保留，回放只验证可执行性，不宣称复现原程序的闭环反应或与 CARLA 0.9.13 完全等价。

## 下一步与验证证据

公共定向检查 27 项，26 通过、1 项因该测试解释器缺少原生库而跳过；该项已在原生 ChatScene 环境单独通过。TTSG 离线检查 18 项通过，独立复审无剩余代码阻断。旧 runtime 测试中的部分初始化仍引用本机不存在的 `/home/shijie20/` 依赖，未冒称完整旧测试集通过。

本次只有一个公开 query、五次方法调用。工程“通过”指产生有效原生 JSON、完成既定执行步骤且记录一致，不表示 query 的全部语义被满足：五个输出全部完成编译；每个输出的五次确定性场景检查均通过；五次公共固定控制器执行均记录 300 步/30 秒、301 行实际轨迹。语义诊断全部由公共无 oracle 提取器产生，未生成正式分数。

五个实际 actor 数依次为 2、1、1、1、1；后四个原生产物没有出现 query 请求的骑行者，没有补对象或以成功执行掩盖这一缺失。旧失败网格不与此新观察配置合并。运行使用用户允许的 GPU 1 共享窗口，SCTG 训练未停止；CARLA 已关闭，不据共享条件报告公平耗时比较。

CARLA 关闭且 API 地址不可用时，生成与运行 `--resume` 均完成；165 个原始生成/执行文件的内容与修改时间保持不变，没有新增 attempt 文件。独立复审先在私人临时目录克隆并验证一份真实记录，再对编译、全部五个场景检查和执行分别删除/篡改 SDK 登记，14 项均按对应阶段规则拒绝；24 个被复审原文件保持不变。

本机新配置 `.carla-runtime/ttsg-local-20261013-verified/`、完整生成输出 `.carla-runtime/ttsg-benchmark-pilot-20261013-verified/`，公共运行目录 `.carla-runtime/ttsg-runtime-20261013-verified/` 及恢复/复审/资源证据 `.carla-runtime/ttsg-integration-audit-20261013/` 均为私人运行证据；旧失败、raw 和 API 输出保留，不并入 wheel、gold 或 Git。回滚可使用提交前版本与保留的文档/链接备份；不删除历史产物或重写既有评测记录。
