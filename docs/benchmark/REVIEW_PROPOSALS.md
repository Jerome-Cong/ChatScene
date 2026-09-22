# BW-05：可审阅的离线修订提案

新增提案与旧 oracle 分开存储，旧 draft 及其绑定不变。每条原 atom 恰好有一个处理记录，包含原 atom 内容 hash、当前任务绑定、状态、结构化未解决原因、机器解释及精确原文位置。提案不等于 gold，也不自动替换人工表单。

## 生成与应用

```bash
python -m bus_benchmark.cli review propose \
  --library /absolute/development.jsonl --oracle /absolute/oracle.jsonl \
  --agent-review /absolute/optional-legacy-agent-draft.json \
  --output /absolute/new-proposal-directory

python -m bus_benchmark.cli review export \
  --library /absolute/development.jsonl --oracle /absolute/oracle.jsonl \
  --reviewer assigned-reviewer --suggestions --output /absolute/new-packet-directory
```

`--agent-review` 可省略。旧 Agent 记录必须与当前 query/oracle hash 和完整 atom ID 集合一致；旧解释保留为机器提示，不能变成原文 quote。

`--suggestions` 是可选试用配置。页面显示具体替换参数和原文片段；点击“采用此修订草稿”只改变草稿并记录机器来源，不产生确认。没有证据的条目保留疑问，须逐条人工处理，不能用“返回审阅”一键清掉机器疑问。最终仍需 BW-03 的本题确认和原正式校验。

## 规则范围与限制

- 数量、可选性和明确不存在：给出具体 count/layer/polarity 修改；范围、条件、可选否定及否定必要性保持疑问，不能把“至少两个”变成“恰好两个”。
- 空间与角色：仅在同句中可唯一定位对象和明示关系时建议修改/拆分；不从角色名猜方位，不把另一个对象的动作绑定过来。
- 事件与时序：有限词典识别事件/持续状态、显式 while/after/then 和 if/when 前提；可提出端点反转、并行替换、触发文本或明确对象绑定。缺少匹配的转述留作疑问。
- 风险与表述强弱：只有明示风险文字才提出风险值修订；分析元数据不自动变成要求。未知源字段必须先完整核对，不允许通过生成替换项静默删除它们。

这是固定离线规则，不是一般语言理解器；LLM 调用为 0。输出记录规则配置及实际实现文件 hash。替换 atom 的来源为机器提取的 `query_text_regex`，只有后续明确人工确认的正式转换才使用 human_review 来源。

## 开发覆盖报告

评估范围为当前完整 48 条开发 query 对应的 538 条原 atom；以下数字是规则分流数量，不是语义准确率，也不代表已完成审阅。

- 具体修改建议（proposed）：5 条，均为机器提案，未自动采纳。
- 局部文字依据、暂不建议改动（supported）：26 条，不代表整条已由人确认。
- 未解决（unresolved）：507 条；主要来自未覆盖的 predicate/措辞、未明示风险、事件/角色/顺序依据不足。

实际制品位于本机 `.review-workspace/bw05-development-proposals-verified/`，未把私人提案入库。较早诊断输出保留，最终报告以该目录为准。缺少真人正确性标注和操作测量，因此不能宣称草稿质量或效率已提升；也不应将当前高疑问量配置默认推广给所有标注者。

## 校验与兼容性

独立 `assets/review/revision_proposals.schema.json` 检查形状，Python 进一步核验源、完整映射、精确引文、操作目标和机器来源。报告也进入浏览器确认快照，导入器从可信源重建；改写机器提示后自算 hash 不能替代可信报告。

新功能不改计分、旧 oracle、旧 Agent 文件或旧 checkpoint；普通导出不强制开启提案。新 UI 字节变化会改变新题包绑定，旧题包保留原件，历史进度迁移按 BW-07 处理，不能覆盖后冒充旧版本。
