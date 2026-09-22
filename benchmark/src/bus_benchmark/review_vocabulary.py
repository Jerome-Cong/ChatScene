"""Shared review labels and canonical confirmation reasons."""




WORKBENCH_CHECKPOINT_SCHEMA_VERSION = "0.2"


SURFACE_ORDER = {"precise": 0, "partial": 1, "vague": 2}


CHECK_LABELS = {
    "support_and_response_disposition": "这个请求是否应由生成器正常处理",
    "cardinality_and_polarity": "所有要求的内容、数量以及必须出现/不得出现是否正确",
    "road_and_spatial_decomposition": "道路和空间要求是否完整且没有重复",
    "event_graph_and_actor_binding": "参与者、空间、外部事件、时序及规则/风险语义是否正确",
    "surface_layering": "每条要求的计分严格程度是否符合当前 query 文本",
    "cpd_common_eligibility": "是否适合评价同一 query 下的合理多样性（CPD）",
}


CHECK_SHORT_LABELS = {
    "support_and_response_disposition": "请求处理方式",
    "cardinality_and_polarity": "要求内容、数量与必须/禁止",
    "road_and_spatial_decomposition": "道路与空间",
    "event_graph_and_actor_binding": "参与者、事件、规则与风险语义",
    "surface_layering": "计分严格程度",
    "cpd_common_eligibility": "CPD",
}


CHECK_HELP = {
    "support_and_response_disposition": (
        "supported 表示应生成场景；unsupported 表示应明确拒绝。若源数据本身标错，"
        "请选择“源数据有误”，本 subject 不能完成。"
    ),
    "cardinality_and_polarity": (
        "检查每张要求的内容参数、数量和必须出现/不得出现极性；只按当前 query 判断，"
        "不从同 intent 的 precise 文本补信息。"
    ),
    "road_and_spatial_decomposition": (
        "检查道路结构、车道设施、参与者位置和数值距离是否应计分。"
    ),
    "event_graph_and_actor_binding": (
        "检查参与者、空间、外部事件、时序及规则/风险语义；公交制动、等待、让行等"
        "反应应由 ego policy 决定。"
    ),
    "surface_layering": (
        "必须满足、仅当前表述必须、允许但不强制、禁止出现，这四种严格程度必须分清。"
    ),
    "cpd_common_eligibility": (
        "只有跨平台可判断、目标明确且允许合理变化的维度，才能进入 CPD_common。"
    ),
}


ATOM_CATEGORY_LABELS = {
    "actor": "参与者",
    "event": "外部事件",
    "temporal": "事件时序",
    "road": "道路结构",
    "spatial": "空间关系",
    "normative": "规则与风险",
}


LAYER_LABELS = {
    "core_required": "必须满足：同一 intent 的三种表述都要求",
    "surface_required": "当前文本必须满足：只由本条 query 明确要求",
    "permitted": "允许但不强制：出现可以，缺失不扣分",
    "forbidden": "禁止出现：出现即不符合要求",
}


ATOM_VERDICT_OPTIONS = [
    ("尚未判断", ""),
    ("当前要求内容和计分方式都正确", "accept"),
    ("不应作为评分要求", "reject"),
    ("内容或计分严格程度需要修改", "modify"),
    ("高级：一条要求应拆成多条", "split"),
    ("高级：与其他要求重复，需要合并", "merge"),
]


ATOM_VERDICT_LABELS = {value: label for label, value in ATOM_VERDICT_OPTIONS}


ATOM_STEP_LABELS = {
    "actors_events": "第1步：参与者与外部事件",
    "road_space": "第2步：道路与空间",
    "rules_risk": "第3步：规则与风险",
}


ATOM_STEP_TABS = {"actors_events": 0, "road_space": 1, "rules_risk": 2}


REVIEW_STEP_TITLES = (
    "1 参与者与外部事件",
    "2 道路与空间",
    "3 规则与风险",
    "4 最终一致性确认",
    "5 CPD 与备注",
)


REQUIRED_VERDICT_OPTIONS = [
    ("尚未确认", ""),
    ("当前总结准确", "accept"),
    ("我已修改相关要求", "revise"),
    ("源数据有误，当前 subject 不能完成", "reject"),
]


REQUIRED_VERDICT_LABELS = {
    value: label for label, value in REQUIRED_VERDICT_OPTIONS
}


CPD_VERDICT_OPTIONS = [
    ("尚未判断", ""),
    ("当前 CPD 结论正确", "accept"),
    ("我需要修改 CPD 结论或维度", "revise"),
    ("源数据有误，当前 subject 不能完成", "reject"),
]


CPD_VERDICT_LABELS = {value: label for label, value in CPD_VERDICT_OPTIONS}


AUTO_REASON_PREFIX = "Human attestation:"


MECHANICAL_REASON_PREFIX = "Mechanical retention proposal:"


STRUCTURED_REASON_PREFIX = "Human rationale code:"


ATOM_ACCEPT_REASON = (
    "Human attestation: verified against the current query; the requirement "
    "content and scoring strictness are correct."
)


CHECK_ACCEPT_REASON = (
    "Human attestation: the completed requirement decisions support the current "
    "high-level summary."
)


CHECK_REVISE_REASON = (
    "Human attestation: source-bound atom or CPD changes are reflected in this "
    "high-level summary."
)


CPD_ACCEPT_REASON = (
    "Human attestation: the current CPD candidate, eligibility, dimensions, and "
    "cross-platform judgment are correct."
)


BATCH_PERMITTED_REASON = (
    "当前批次覆盖的 query 未把该细节作为必须条件；允许生成，但缺失时不应扣分"
)


DERIVED_REQUIRED_CHECKS = (
    "cardinality_and_polarity",
    "road_and_spatial_decomposition",
    "event_graph_and_actor_binding",
    "surface_layering",
)


CPD_REVISION_REASON_OPTIONS = [
    ("请选择主要修改原因", ""),
    ("是否存在合理变化的判断需要修改", "candidate_classification"),
    ("跨平台可判断性需要修改", "cross_platform_judgeability"),
    ("变化维度或允许值需要修改", "dimension_definition"),
    ("目标参与者或选择器需要修改", "target_binding"),
    ("其他原因（需要文字说明）", "other"),
]


CPD_REVISION_REASON_TEXT = {
    "candidate_classification": (
        "Human rationale code: CPD candidate classification requires revision."
    ),
    "cross_platform_judgeability": (
        "Human rationale code: CPD cross-platform judgeability requires revision."
    ),
    "dimension_definition": (
        "Human rationale code: CPD dimensions or allowed values require revision."
    ),
    "target_binding": (
        "Human rationale code: CPD target actor or selector requires revision."
    ),
}


TOKEN_LABELS = {
    "bicycle": "自行车",
    "pedestrian": "行人",
    "motorcycle": "摩托车",
    "vehicle": "机动车",
    "bus": "公交车",
    "single_event": "单一事件",
    "sequential": "依次发生",
    "parallel": "同时发生",
    "compliant": "遵守规则",
    "violating": "违反规则",
    "low": "低",
    "medium": "中",
    "high": "高",
    "multiple": "多个（精确数量未限定）",
    "optional": "可选（可以有，也可以没有）",
    "has_crosswalk_zone": "人行横道区域",
    "has_cycle_crossing_zone": "自行车过街区域",
    "has_nonmotor_lane": "非机动车道",
    "has_turn_lane": "转向车道",
    "motor_lanes_same_direction": "同方向机动车道数量",
}


CHECKPOINT_KEYS = {
    "schema_version",
    "artifact_type",
    "human_gold",
    "formal_submission",
    "split",
    "reviewer_id",
    "packet_id",
    "source_binding",
    "agent_review_artifact_id",
    "entries",
    "checkpoint_id",
}
