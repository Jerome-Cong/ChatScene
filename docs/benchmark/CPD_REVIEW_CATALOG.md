# CPD 普通审阅与专家准备

普通审阅页按当前题目的策略展示允许变化、目标类别和理由，不把相同维度名视为相同 query policy。核对时必须同时看原文和要求卡片；整题明确提交才覆盖 CPD。对策略有异议可点击“对 CPD 有异议，交专家处理”，补充备注并导出备份，由既有问题单入口交给维护者；该动作清除本题确认。

共享目录在 `review_cpd.policy_catalog()`，selector 复制自既有 `cpd.QUERY_BLIND_TARGET_SELECTORS`。距离按初始纵向相对距离绝对值计算，现有提取器固定近≤10米、中≤30米、远>30米；目录只解释，不新增评分或分箱实现。不同阈值和未知维度会阻断页面快捷确认。完整 query policy 和共享目录在只读专家展开区，不让普通审阅者重复配置 selector。

复杂策略修改继续使用已有 notebook 的 `CPDPolicyEditor`；离线普通页面仅保留当前策略或提出异议，不提供自由修改 selector 的入口。该编辑器返回既有表单格式，`response_from_form` 同步 CPD decision 与 `cpd_common_eligibility` required check，最终仍由既有校验器决定是否合法。不要手改已确认备份。

CPD 主指标保留自车门槛、core_required 和 forbidden，并要求证据完整；surface_required 仍参与原有符合度计分，不被这里删除。目标不唯一或缺失时维度不可用，不给任一 baseline 特殊待遇。

目录实际内容纳入题包完整词典绑定。目录说明变化、或者旧包没有目录时，迁移保留旧表单与原件，但要求重新确认；不借“只是 UI 更新”沿用不同解释下的确认。原评分公式、固定分母、selector、原题库和 oracle 均未修改。

本机 Judge 内核不兼容、真实跨平台 CPD 校准及真人试标仍待补齐。目录中的 CARLA/MetaDrive 说明不代表上述验收通过。
