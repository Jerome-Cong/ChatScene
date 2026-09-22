"""Read-only CPD review catalog; the scorer remains the authority.

Selectors are copied from the existing evaluator contract. Descriptions are UI
explanations, never alternate extraction or scoring implementations.
"""
import copy
from .cpd import QUERY_BLIND_TARGET_SELECTORS

CATALOG_VERSION = "1"


def policy_catalog():
    definitions = {
        "actor_longitudinal_distance_bin": {
            "label": "目标参与者的纵向距离",
            "values": {"near": "近：不超过 10 米", "medium": "中：超过 10 米且不超过 30 米", "far": "远：超过 30 米"},
            "explanation": "按初始时刻相对自车纵向距离的绝对值分箱；现有提取器固定使用 10/30 米。",
            "bin_definition_m": {"near_max": 10.0, "medium_max": 30.0},
        },
        "optional_road_feature_presence": {
            "label": "可选道路设施是否存在", "values": {},
            "explanation": "只允许本题明确列出的设施出现或缺失，不放宽文字中的必须项。",
        },
        "yield_realization_mode": {
            "label": "让行的实现方式", "values": {"decelerate": "减速让行", "hold": "保持等待"},
            "explanation": "变化的是让行方式，不是是否遵守必须满足的让行要求。",
        },
        "merge_gap_relation": {
            "label": "相对目标参与者的并入位置",
            "values": {"ahead_of_gap_actor": "并入目标参与者前方", "behind_gap_actor": "并入目标参与者后方"},
            "explanation": "只能在本题允许的位置之间变化；目标必须能唯一确定。",
        },
    }
    for name, selector in QUERY_BLIND_TARGET_SELECTORS.items():
        definitions[name]["target_selector"] = copy.deepcopy(selector)
    return {"version": CATALOG_VERSION, "dimensions": definitions,
            "preservation": "CPD 主指标保留自车门槛、各表述共同必须项和禁止项，并要求证据完整；当前文字必须项仍参与原有符合度计分。",
            "platforms": "共享规则面向 CARLA 与 MetaDrive，不代表本机 Judge 或跨平台验收已通过。目标缺失或无法唯一绑定时，该维度不可用。"}
