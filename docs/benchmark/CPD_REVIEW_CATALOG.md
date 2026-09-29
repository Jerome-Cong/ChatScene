# CPD 逐项标注与维护者技术核对

## 标注者需要做什么

CPD 卡片标题为“哪些细节可以变化？”。每项显示变化对象和允许的不同安排，标注者对照当前原文选择：

- 原文允许这些变化。
- 原文限制了这些变化。
- 信息不足，无法判断。

所有项目默认未选。最后还需检查是否遗漏了其他可以变化的细节；草稿未列任何变化时仍有遗漏检查。出现异议，在该项下填写原因或修改建议，不必重复写入全题备注。异议和未回答项不能作为无异议题提交，但可以随进度备份保存、交回维护者。未写完的原因会保留为“尚未填写具体原因”，不代写为标注者意见。

“维护者技术资料”及其共享规则折叠区不是普通标注者的必读内容。普通提交只确认可见的语义问题，不代表确认目标选择、距离分箱、平台实现或 CPD eligibility。题包保存完整策略用于核对来源，字节绑定不等于对隐藏内容的人类确认。

修改要求或策略后，旧 CPD 选择失效，原因文字保留供参考，需要重新逐项判断。仅修改全题备注不会改变语义依据，但仍清除整题确认收据。通用“返回本题审阅”不能清空 CPD 异议，须回到对应项目修改判断。旧进度迁移时，源或指南变化使当前答案清空，旧答案和原因在只读历史中保留。

## 维护者回收与定稿

`review validate` / `review import` 校验每项回答、对应源和当前编辑。导入结果的 `responses` 为空；`semantic_responses` 明确使用 `pending_technical_review`，不能直接交给原正式 finalizer。`subjects_submitted` 只表示完成普通可见语义确认，不表示技术核对完成。

`review import` 同时生成 `cpd_technical_review_template.json`，默认 `approved=false`、维护者姓名为空。维护者应检查每题完整策略与当前修订后的要求，包括 candidate/eligibility、维度、目标唯一性、分箱和跨平台约定。在保持 packet、backup 和逐题 policy 摘要不变的前提下，填入实际技术审阅者标识并明确批准，保存为单独的可信核对记录；不要采用标注者自行附带的批准文件。这是职责区分，不强制要求第二个不同的人，真实人员身份仍由外部交接记录保证。

```bash
bus-benchmark review finalize \
  --library /data/library.jsonl --oracle /private/oracle.jsonl \
  --assignment /private/packet/assignment.json \
  --submission /private/progress.json \
  --cpd-technical-review /private/approved_cpd_review.json \
  --output /private/new-finalized-directory
```

未明确批准、错误题包/备份/策略、未完成题或未解决疑问都会阻断定稿。批准只适用于那一份确切备份，后续编辑需要重新核对。通过后才编译原正式 response，CPD decision 与 required check 保持一致，记录技术审阅者并调用原 validator/finalizer。若策略需要修改，继续使用已有专家编辑流程准备新策略/题包并让标注者重新核对，不手改已确认备份。

## 共享规则与兼容边界

目录仍复制既有 selector。距离按初始纵向相对距离绝对值计算，现有提取器固定近≤10米、中≤30米、远>30米。不同阈值与未知维度继续阻断页面快捷确认，不新增评分或分箱实现，不为任何 baseline 特殊放宽。

CPD 主指标保留自车门槛、core_required 和 forbidden，并要求证据完整；surface_required 仍参与原有符合度计分。目录/指南内容均绑定题包，旧题包须按原迁移流程处理，原件不覆盖。Judge 兼容内核、真实平台校准和最终发布门槛继续保留。
