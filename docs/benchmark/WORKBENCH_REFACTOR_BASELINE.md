# Workbench 重构基线（BW-00）

核实日期：2026-09-22。源码基点：`ca6ecdb4a942d79154f6e4ee738b51522a5f0f5b`。

状态：**BW-00 阻塞，未验收完成；BW-01 至 BW-12 未开始。** 本文记录实际执行结果，不表示原实施计划已经实现。维护者需要先提供可复现的基线运行环境及缺失资产，才能区分后续重构回归与既有失败。

## 已核实的源码与版本

- 根目录 `bus_benchmark/` 和 `benchmark/src/bus_benchmark/` 都是实际文件；排除运行生成的 `__pycache__` 后，没有单侧独有文件，唯一内容差异为 `schema.py`。
- `benchmark/src` 源码树在 `ca6ecdb`（cleanup and reorganize）进入版本控制。当前构建配置没有声明自动同步步骤；不得把两份源码当成自动镜像。
- 仓库根目录运行 Python 优先导入根目录包；`benchmark/pyproject.toml` 的 wheel 从 `src` 构建。根目录 `schema.py` 直接使用包内 schemas，安装源码版本先检查 `PACKAGE_ROOT.parent/benchmark_configs/schemas`，不存在时才使用包内资源。
- query library 为 v0.2；实际可读开发 library 和开发 oracle 各有 48 行。benchmark/schema 常量为 0.1，包版本为 0.1.0，workbench checkpoint schema 为 0.2。这些版本不应互相替代。
- guide 的当前版本以本基点中的 `HUMAN_REVIEW_GUIDE.md` 内容为准，没有在本轮另行升级。正式协议定稿未在本轮核实；`protocol_decisions_draft.json` 仍是草稿材料。

当前 `benchmark_artifacts/human_review/pending/README.md` 明确将目录内历史双人审阅材料标为不可定稿的预览。本轮没有拿到真实人工 checkpoint 或操作录像；真实 assignment 绑定及进度为 **unknown**。不从这些历史预览推断人工完成率。

## 已执行验证

下文“通过数”是测试用例通过数量，不是人工标注数，也不证明实际标注效率。

执行环境：Linux；优先使用已有 `/home/ubuntu/Documents/shijie/mdsn/BusScene/.venv/bin/python`，没有安装依赖、修改环境或运行仿真。

```bash
/home/ubuntu/Documents/shijie/mdsn/BusScene/.venv/bin/python -m unittest discover -s tests/bus_benchmark -p test_query_review_workbench.py
/home/ubuntu/Documents/shijie/mdsn/BusScene/.venv/bin/python -m unittest discover -s tests/bus_benchmark -v
```

- workbench：59 项，全部通过，113.930 秒。
- 全量：465 项，353 项通过、2 项失败、108 项错误、2 项跳过，195.435 秒。通过数由总数减去失败、错误与跳过计算；这是未修改生产代码时的基线结果。
- 单独生成链复现：`-p test_generation.py`，37 项中 33 项错误。错误不应被包装为重构引起的回归。
- 为定位解释器问题，额外使用已存在的真实 `/home/ubuntu/miniconda3/bin/python3.9` 运行同一生成链测试，仍有 33 项错误：缺少正式环境要求的 `pyvenv.cfg`，以及旧机器实现文件缺失。该诊断运行不替代约定的项目环境。

原始本机日志：`/tmp/bw00-tests.log`、`/tmp/bw00-workbench.log`、`/tmp/bw00-generation.log`、`/tmp/bw00-generation-regular.log`。这些是临时诊断日志，未作为发行资源或正式运行证据提交。

## 阻碍与恢复条件

1. 指定 venv 的 Python 是指向 Conda Python 的符号链接；生成链要求启动进程的解释器本身是普通文件。应提供符合既定验证要求的运行环境；不允许通过放宽校验或运行后替换 `sys.executable` 冒充。
2. `chatscene_carla_draft.json` 的 implementation bundle 仍为 draft，绑定旧机器路径。首个绑定 `/home/shijie20/.local/bin/bwrap` 不存在；CARLA 0.9.13 egg、旧环境清单及其他实现文件也缺失。需要原环境或维护者认可的、按真实文件重新登记的新开发环境，不能只改路径保留旧 hash。
3. MetaDrive 测试依赖 `/home/shijie20/CodeSpace/mdsn/metadrive/metadrive/component/algorithm/BIG.py`；另有测试依赖 `/home/shijie20/micromamba/envs/scenarionet/bin/python`。当前文件不存在，需要对应源码和环境。
4. Judge 测试报告 `Linux sealed memfd execution support` 不满足，包含 23 项错误及一项断言失败。尚未区分解释器能力与执行环境限制，需要在符合要求的环境中复测。
5. `test_uqh_attest_private_key_permissions_wrong_key_and_repo_path` 预期抛出异常但没有抛出。根因尚未核实，不能归因于上述路径问题，也不能宣称安全基线通过。

GitHub 只读连接已在沙箱外验证成功，远端 HEAD 与上述基点一致；沙箱内 DNS 失败不是仓库权限失败。

## 回归材料与未完成交付

现有可复用测试入口见 [兼容性样例清单](WORKBENCH_COMPATIBILITY_CASES.md)。这是已定位的测试覆盖索引，不冒充新建的完整固定表单/payload 样例集；后者仍需在 BW-00 恢复后补齐并保存 canonical 对照。

操作测量定义见 [测量说明](WORKBENCH_OPERATION_MEASUREMENT.md)。真实人工判断时间、机械操作次数、界面延迟和语义材料不清成本全部为 unknown；未执行真人试标。不得声称效率改善。

## 兼容性与回滚

本次只新增审计文档并更新计划状态；没有修改生产源码、题库、oracle、metrics、环境、旧 checkpoint 或绑定配置。无需数据迁移。撤销这些文档增补即可回到原有行为，保留原实施计划的规划正文。

下一步：恢复上述真实开发环境及资产，复测失败并完成固定兼容样例，再验收 BW-00；验收前不开始 BW-01。
