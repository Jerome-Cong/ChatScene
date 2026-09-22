# BW-04：离线审阅题包

标注者只需要维护者发出的 `review.html`。使用支持 Web Locks 的 Chrome/Edge 打开，断网可用，不需要 Python、CARLA 或 Jupyter。页面内含一页指南与六个练习；练习不修改真实题目答案。

## 标注者操作

1. 阅读常驻原文与全部草稿，只修改异议；不确定时保留编辑并暂存，仍可切换题目。
2. 核对对象、参数、计分层级、出现方向和 CPD 后，点击唯一的本题提交按钮。无变化的“修改”、重复拆分和未形成有效组的合并不会通过。
3. 定期导出进度备份，确认文件出现在浏览器下载记录中，再交给维护者。页面的“已保存到浏览器”不等于已保存磁盘文件；存储失败时会明确显示“仅内存”。

同一题包只允许一个标签页编辑；另一个标签页只读。损坏、错 reviewer、错题包或过期备份不会覆盖当前进度。修改已提交内容会恢复待提交。浏览器提交仍须维护者校验，不能自行称为正式 gold。

初版嵌套复杂参数保留原值并提示交给专家；CPD 修改异议进入疑问区，原 notebook 专家入口继续保留。角色/事件值来自草稿，未翻译标记仍保留原值，不能靠名称推断缺失语义。

## 维护者准备与回收

以下在 checkout 中使用 `python -m bus_benchmark.cli`；BW-09 安装后使用同名 `bus-benchmark` 命令。

```bash
python -m bus_benchmark.cli review export \
  --library /absolute/development.jsonl --oracle /absolute/oracle.jsonl \
  --reviewer assigned-reviewer --output /absolute/new-packet-directory

python -m bus_benchmark.cli review validate \
  --library /absolute/development.jsonl --oracle /absolute/oracle.jsonl \
  --assignment /absolute/new-packet-directory/assignment.json \
  --submission /absolute/browser-backup.json

python -m bus_benchmark.cli review import \
  --library /absolute/development.jsonl --oracle /absolute/oracle.jsonl \
  --assignment /absolute/new-packet-directory/assignment.json \
  --submission /absolute/browser-backup.json --output /absolute/new-import-directory
```

`assignment.json` 必须是维护者原先保存的可信原件，不能采用浏览器用户另交的替代版本。导入器从可信 library/oracle 重建 task、proposal 和绑定，核对 guide/UI/词典版本、完整题目列表、reviewer、内容摘要与确认范围；客户端自洽地重算 hash 不足以伪造源。

导入只保存审计备份与已验证的非 gold 草稿。全部 assigned subjects 均明确提交后，维护者才可用同样参数调用 `review finalize --output NEW_DIRECTORY`，转交已有正式 finalizer；任何暂存/缺项都阻断。一个开发子包完成只表示这个明确 source bundle 完成，不表示当前 300 subject 套件或跨平台最终冻结已完成。

所有输出目录必须全新；错误保留原件。题包、备份和导入状态建议放在忽略入库的 `.review-workspace/`，不进入方法源码、生成器上下文或 wheel。工具未改变生成器输入；实际运行隔离边界仍由 BW-10 验证，不能由“query-only”参数宣称文件系统零泄漏。

## 已验证与未验证

Python 的源重建/防篡改/不可覆盖/闭合测试通过；真实 Linux Chromium 在断网模式验证编辑、提交、文件下载、恢复、多标签互斥、存储配额失败、损坏记录保护、脚本注入和练习隔离。

Windows Chrome/Edge 的人工双击测试尚未执行，列为 BW-11 跨平台验收事项；不以 Linux Chromium 测试冒充 Windows 实测。真人效率和语义错误发现能力尚未测量。Judge 与仿真环境限制继续单独保留。

Windows 复验步骤：断网双击 HTML → 输入中文备注并切题 → 导出文件并关闭 → 重开续做 → 恢复备份 → 打开第二标签验证只读 → 尝试旧/错题包备份验证原进度不变。记录浏览器版本与任何失败，试标不计正式 gold。

## 工程实现与回滚

源码模块为 `assets/review/app.js`、`app.css`、`template.html`，导出时合为单文件，无 CDN/远程调用；动态内容使用文本节点，嵌入 JSON 转义脚本闭合字符，CSP 禁止网络和 eval。Python/浏览器收据使用带类型树与有限 binary64 数字的共享摘要规则；正式 compiler/hash 不改。

旧 notebook 与 checkpoint 无迁移要求。撤回此包时保留所有导出和备份供审计，不把浏览器文件改名为旧 checkpoint。后续 UI 更新与源修订的安全迁移在 BW-07 明确处理。
