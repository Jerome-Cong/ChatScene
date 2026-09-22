# BW-07：疑问、源问题与安全迁移

暂存保留表单与备注、撤销当前确认，并允许继续下一题。`source_error` 明确标为 `requires_source_fix`，其他疑问为 deferred；两者都不能进入整套定稿。机器疑问、人工疑问与系统发现的历史原因缺失分开标记。

## 导出问题单

```bash
python -m bus_benchmark.cli review issues \
  --assignment /absolute/trusted-assignment.json --submission /absolute/backup.json \
  --library /absolute/original-library.jsonl --oracle /absolute/original-oracle.jsonl \
  --output /absolute/new-issue-directory
```

问题单包含原文、query/oracle hash、subject、相关 atom、原因、备注与中间表单，维护者可以精确定位源。它只写本地文件，不发送消息或自动修源，也不生成 gold。已有输出拒绝覆盖。

## 迁移浏览器进度

先保留旧源文件，再把修订写入新文件；不要先覆盖旧库。仅升级 UI 时，两组源参数可以指向同一原件。

```bash
python -m bus_benchmark.cli review migrate \
  --assignment /absolute/old-packet/assignment.json --submission /absolute/old-backup.json \
  --old-library /absolute/old-library.jsonl --old-oracle /absolute/old-oracle.jsonl \
  --library /absolute/new-library.jsonl --oracle /absolute/new-oracle.jsonl \
  --output /absolute/new-migration-directory
```

输出包括新 `packet/review.html`、可信 `packet/assignment.json`、可恢复的 `progress.json`、逐题影响报告和只读原件目录 `originals/`。原文件不写入、不改名、不删除；逐文件快照保证归档字节就是本次使用的输入。发生错误时保留原件，不能通过修改旧 hash 强行继续。

迁移规则如下：

- **只改变 UI，单题内容与指南/实际词典/编译规则不变**：原先完整明确确认可以沿用，界面与收据标为 `carried_confirmation`，不冒充一次新作答；编辑后照常恢复未提交。
- **只改变某个 subject 的 query/oracle**：该题需要完整重审。其他 subject 即使因全库 hash 改变而获得新 task ID，也可逐题保留原确认。
- **指南或实际词典改变**：相关旧确认不能只靠复用版本字符串或旧声明 hash 继续；当前策略保守地重审共享该指南/词典的题。报告用 JSON Pointer 列出改变的位置。
- **源变更或高级表单不能直接解释**：旧表单、理由、CPD 修订及备注只读保留；不把不再适用的旧要求自动塞进新表单。页面有可读摘要与完整旧表单展开区。
- **重复迁移**：继续归档更早的原始证据到 lineage 文件。必须保留整个迁移输出目录，不能只带走 HTML/assignment 后宣称历史链完整。

## 旧 notebook checkpoint

使用相同 `review migrate` 命令，省略 `--assignment`，把 `--submission` 指向旧 checkpoint，并提供 `--reviewer ASSIGNED_REVIEWER`。当前支持 schema 0.2，调用原 checkpoint validator 验证来源与状态，不在旧文件旁创建会话或锁。

旧 checkpoint 没有新版 guide/registry 绑定，无法自动证明两次确认责任相同。因此默认保留可编辑表单和所有原件，但要求重新明确确认；不清空原 notebook 的完成状态，也不强制用户放弃旧入口。这是已说明的交互协议迁移，不是以 CSS 更新为由抹掉旧进度。

## 信任与确认边界

迁移授权只存在于**维护者掌握的可信 assignment 原件**，由工具在校验旧来源、旧完整收据、逐题内容和指南/实际词典后生成。浏览器备份不能自行追加这份授权，沿用收据也不能改变原审阅表单。导入同时重建当前 query/oracle，校验迁移授权、内容摘要和覆盖范围，并保存 confirmation_origins。

授权和 hash 都不证明真实人工身份。维护者仍须确认旧作答来自指定真人，不能把 Agent 构造的完整测试记录迁移成“真实人工证据”。沿用只保留已验证的既有确认；迁移、暂存和导入均不调用正式 finalizer。

所有备份和 migration_report/originals 必须留在评测工作区，不放入方法仓库或发行 wheel。只有当前 source bundle 全部明确提交后，现有 finalizer 才可定稿；未解决问题与旧版本保护不能绕过。

## 已验证范围

自动测试覆盖 UI-only 沿用、单题源变更、真实词典变化但伪留旧 hash、指南变化、旧 checkpoint 保留、错 reviewer/来源、收据篡改、重复迁移 lineage、编辑后失效、疑问导出与浏览器只读旧 CPD 理由。

本机另用 BW-04 的实际 12 题开发题包和**显式构造的未开始状态**验证迁移，输出位于 `.review-workspace/bw07-unstarted-demo-verified/`：12 题草稿保留、0 条沿用确认、0 gold。这不是恢复了用户的真人进度；真实旧 checkpoint 和真人试标报告仍未提供。
