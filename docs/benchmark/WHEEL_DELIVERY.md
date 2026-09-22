# BW-09 单一源码与 wheel 交付

唯一可编辑实现为 `benchmark/src/bus_benchmark`。根目录同名包已退役，旧源码在 Git 历史及本地 `.review-workspace/bw09-source-backup/` 可恢复；没有保留会遮蔽安装包的根目录 shim/symlink。两个版本此前只有 schema 路径不同，现采用此前根目录实际生效的包内 schema，不修改 schema 内容。

## 安装与旧入口

旧 `scripts/bus_benchmark.py` 会遮蔽已安装的包，已改名为 `scripts/bus-benchmark.py`；推荐直接使用安装后的 CLI。

先运行 `python -m pip install ./benchmark`，再用 `bus-benchmark` 或 `python -m bus_benchmark`。开发使用 editable 安装；也可显式设置 `PYTHONPATH=benchmark/src`，但它不能用作 wheel 验收证据。旧 notebook 安装 `./benchmark[notebook]` 后仍可导入；离线 HTML 的维护者核心不依赖 IPython/ipywidgets。

旧 `launch_workbench` 的 checkout 数据定位改为 `BUS_BENCHMARK_WORKSPACE`，未指定则使用当前目录，与现有 Agent 工作区一致。继续使用旧入口时指向包含旧 scripts/query_lib/benchmark_artifacts 的原工作区，不把该目录内容拷入安装包。新的 ReviewContext/CLI 使用显式输入及输出路径。

方法运行配置中旧的源码绝对路径或冻结源码清单不能直接代表此次安装；重新准备相应清单并走原验收，不覆盖旧记录。源码位置改变不是 Judge、平台校准或冻结门槛的豁免。

## 构建与验收

`uv build benchmark` 生成 wheel 与 sdist。版本为 `0.2.0rc1`，表示候选交付，不是正式 benchmark 发布。资源声明包含 schemas、query libraries、审阅 HTML/CSS/JS/指南/练习/提案 schema 及旧 widget CSS；发行清单记录文件 hash 和环境版本。

复现干净安装：在新环境仅安装 wheel，把独立测试输入与 `tests/bus_benchmark/wheel_smoke.py` 复制到仓库外空目录，使用该环境的 `python -I wheel_smoke.py library.jsonl oracle.jsonl`。脚本核实模块来自 site-packages、无 notebook 依赖、资源可读，调用安装后的 CLI 完成 paths/export/validate/import，并拒绝失效确认。测试收据明确为合成，不是人工 gold。

维护者持久化依赖 POSIX flock，尚不宣称 Windows Python 可用。Linux 离线 Chromium 已验证，Windows 真机与实际人工试标尚待 BW-11。Judge 内核限制及其失败记录继续保留；此次打包通过不能替代正式评测和最终发布验收。
