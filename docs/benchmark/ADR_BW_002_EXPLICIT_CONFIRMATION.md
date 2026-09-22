# ADR BW-002：提案、编辑与明确确认

状态：BW-03 实现；交互协议改变，计分协议与正式闭合要求不变。

## 数据与责任

机器提案是独立的不可覆盖制品：`make_proposal` / `publish_proposal` 记录完整源任务绑定、提案内容、生成来源以及 guide/registry/compiler 版本。guide 内容和词典内容也进入绑定。提案本身和人工草稿都明确 `human_gold: false`。

人工草稿引用 proposal ID，独立保存当前表单、修改序号、修改来源、疑问和确认收据。`edit_draft` 将人工编辑、继承和机器重新应用分别登记；任何一次编辑都清空确认并恢复 draft，即使编辑后内容恰好相同。机器提案不被原地覆盖。

浏览器或其他前端必须显示 `confirmation_projection` 的当前原文、全部源要求及处置、替换要求、新增要求、support、CPD 与备注。确认当前题或可见语义组时，将显示快照的内容 hash 和实际覆盖单元传给 `confirm_scope(..., explicit=True)`。打开、预填、继承、自动保存没有调用该操作的权限语义，不产生收据。

所有单元有当前版本、当前 reviewer、当前内容的明确确认后才进入 submitted；submitted 只表示已完成交互确认，仍然不是正式 gold。`compile_confirmed` 还须接收可信 assignment 的 reviewer ID，并调用原 `response_from_form` 与正式 validator。source/target ID、六项 required-check 的推导继续由原 compiler 负责。

## 拒绝条件

- 原文、任务、proposal、guide、词典或 compiler 版本变更；陈旧显示快照或外来 reviewer。
- 缺少确认单元、虚构覆盖单元、收据篡改或未解决疑问。
- 最终要求含未知字段、无效原文位置或展示词典不能解释的结构；必须先修订或暂存，不能一键全部接受。
- 正式 validator 拒绝的源错误、处理映射、CPD 或其他不一致。

原 BW-00 中使用任意合成 predicate 的旧 merge 向量仍验证旧 compiler，但不能通过新快捷确认。新模型另用已登记 predicate 验证合法 merge 的等价输出；这是显式确认边界加强，不是修改评分公式。

## 持久化与信任边界

`ReviewDraftStore` 在锁内比较旧状态 hash，再原子写入；陈旧写入、错误 reviewer、来源变更和损坏的 submitted 内容拒绝保存或恢复。加载或保存不调用 finalizer。旧 notebook checkpoint 保持原 schema，新模型使用独立文件，不隐式迁移历史进度。

确认收据与内容 hash **不是数字签名或真实人工身份认证**。前端能声明 explicit 行为，但维护者仍须控制题包分配、真实 reviewer 和导入渠道；不能靠客户端重算 hash 将伪造源变成可信输入。BW-04 的导入器必须从维护者可信原始源重建并验证，不能信任浏览器提供的任务副本。

自动确认理由以 `Human attestation:` 标记“明确确认已显示内容”，并非声称人手写该句。机器修订理由保留机器来源说明；人工自由输入不被机器理由覆盖。

旧正式 finalizer 仍要求整套完成，唯一能够生成 gold；当前新模型 API、草稿文件和浏览器局部提交均不能替代该步骤。
