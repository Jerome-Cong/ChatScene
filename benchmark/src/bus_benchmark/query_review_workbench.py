"""Compatibility notebook UI backed by the portable review service."""
import asyncio
import copy
import html
import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence
from .errors import ValidationError
from .human_workflow import (
    atom_semantic_projection,
    query_check_projection,
    query_check_scope_description_zh,
)
from .oracle import REQUIRED_REVIEW_CHECKS
from .review_field_widgets import AtomListEditor, CPDPolicyEditor
from .review_forms import (
    _atom_batch_id,
    _atom_form_decision_has_content,
    _blank_form,
    _cpd_batch_id,
    _cpd_form_decision_has_content,
    _cpd_review_projection,
    _cpd_revision_reason_code,
    _form_from_response,
    _inherited_surface_form,
    _is_generated_reason,
    _json_copy,
    _json_sha256,
    _merge_inherited_surface_form,
    _normalize_human_atom,
    _normalized_auto_reason,
    _pretty,
    _read_json_text,
    _reason_for_verdict,
    response_from_form,
)
from .review_legacy import (
    AGENT_REVIEW_PATH,
    ASSIGNMENT_EXPORTER,
    DEFAULT_ASSIGNMENT_DIR,
    HUMAN_REVIEW_ROOT,
    LEGACY_ASSIGNMENT_DIR,
    REPOSITORY_ROOT,
    SPLIT_SOURCES,
    _artifact_matches,
    _live_agent_review,
    ensure_review_assignment,
    session_from_assignment,
)
from .review_presentation import (
    _human_agent_recommendation,
    _human_atom_evidence,
    _human_atom_statement,
    _human_token,
)
from .review_session import QueryReviewSession
from .review_presentation import quick_confirmation_issues
from .review_store import _checkpoint_guard
from .review_vocabulary import (
    ATOM_ACCEPT_REASON,
    ATOM_CATEGORY_LABELS,
    ATOM_STEP_LABELS,
    ATOM_STEP_TABS,
    ATOM_VERDICT_LABELS,
    ATOM_VERDICT_OPTIONS,
    AUTO_REASON_PREFIX,
    BATCH_PERMITTED_REASON,
    CHECKPOINT_KEYS,
    CHECK_ACCEPT_REASON,
    CHECK_HELP,
    CHECK_LABELS,
    CHECK_REVISE_REASON,
    CHECK_SHORT_LABELS,
    CPD_ACCEPT_REASON,
    CPD_REVISION_REASON_OPTIONS,
    CPD_REVISION_REASON_TEXT,
    CPD_VERDICT_LABELS,
    CPD_VERDICT_OPTIONS,
    DERIVED_REQUIRED_CHECKS,
    LAYER_LABELS,
    MECHANICAL_REASON_PREFIX,
    REQUIRED_VERDICT_LABELS,
    REQUIRED_VERDICT_OPTIONS,
    REVIEW_STEP_TITLES,
    STRUCTURED_REASON_PREFIX,
    SURFACE_ORDER,
    TOKEN_LABELS,
    WORKBENCH_CHECKPOINT_SCHEMA_VERSION,
)


class QueryReviewWorkbench:
    """Lazy-rendered ipywidgets UI for one ``QueryReviewSession``."""

    def __init__(self, session: QueryReviewSession) -> None:
        try:
            import ipywidgets as widgets
        except ImportError as exc:  # pragma: no cover - environment-specific
            raise RuntimeError("query review workbench requires ipywidgets") from exc
        self.widgets = widgets
        self.session = session
        self.current_query_id: Optional[str] = None
        self.dirty = False
        self._autosave_handle = None
        self._suppress_query_change = False
        self._required_widgets: Dict[str, Dict[str, Any]] = {}
        self._atom_widgets: Dict[str, Dict[str, Any]] = {}
        self._atom_locations: Dict[str, Dict[str, Any]] = {}
        self._conditional_reason_refreshers: List[Any] = []
        self._rendering = False
        self._closed = False
        self._build_shell()
        self._refresh_query_options()

    @staticmethod
    def _safe_pre(value: Any) -> str:
        return "<pre style='white-space:pre-wrap'>" + html.escape(_pretty(value)) + "</pre>"

    @staticmethod
    def _mapped_source_atom_ids(
        task: Mapping[str, Any], check: str
    ) -> List[str]:
        """Derive source-card membership from the production projection itself."""

        draft = task["oracle_draft"]
        baseline = query_check_projection(check, draft)
        mapped = []
        for atom in draft.get("atoms", []):
            without_atom = copy.deepcopy(draft)
            without_atom["atoms"] = [
                candidate
                for candidate in draft["atoms"]
                if candidate["atom_id"] != atom["atom_id"]
            ]
            if query_check_projection(check, without_atom) != baseline:
                mapped.append(atom["atom_id"])
        return mapped

    def _check_mapping_html(
        self,
        task: Mapping[str, Any],
        check: str,
        atom_ids: Sequence[str],
    ) -> str:
        atoms_by_id = {
            atom["atom_id"]: atom for atom in task["oracle_draft"].get("atoms", [])
        }
        if not atom_ids:
            coverage = "<b>关联 requirement 卡：</b>无"
            details = ""
        else:
            category_counts: Dict[str, int] = {}
            for atom_id in atom_ids:
                category = atoms_by_id[atom_id]["category"]
                category_counts[category] = category_counts.get(category, 0) + 1
            category_text = "、".join(
                "{} {} 张".format(
                    ATOM_CATEGORY_LABELS.get(category, category), count
                )
                for category, count in sorted(category_counts.items())
            )
            coverage = (
                "<b>关联 requirement 卡：</b>{} 张（{}）".format(
                    len(atom_ids), html.escape(category_text)
                )
            )
            details = (
                "<details><summary>展开查看本条 query 的具体关联卡</summary><ul>{}</ul></details>".format(
                    "".join(
                        "<li>{}</li>".format(
                            html.escape(_human_atom_statement(atoms_by_id[atom_id]))
                        )
                        for atom_id in atom_ids
                    )
                )
            )
        return (
            "<div style='margin:7px 0;padding:7px 9px;background:#f7fafc;"
            "border-left:3px solid #718096'><b>正式关联规则：</b>{}<br>{}{}</div>".format(
                html.escape(query_check_scope_description_zh(check)),
                coverage,
                details,
            )
        )

    @classmethod
    def _close_widget_tree(cls, widget: Any, seen: Optional[set] = None) -> None:
        """Close a detached ipywidgets subtree so long reviews stay bounded."""

        seen = seen if seen is not None else set()
        identity = id(widget)
        if identity in seen:
            return
        seen.add(identity)
        related = list(tuple(getattr(widget, "children", ()) or ()))
        for attribute in ("layout", "style"):
            candidate = getattr(widget, attribute, None)
            if candidate is not None and hasattr(candidate, "close"):
                related.append(candidate)
        for child in related:
            cls._close_widget_tree(child, seen)
        close = getattr(widget, "close", None)
        if callable(close):
            close()

    def _discard_rendered_subject(self) -> None:
        editors = [
            widgets.get("replacement_editor")
            for widgets in self._atom_widgets.values()
        ]
        editors.extend(
            getattr(self, name, None)
            for name in (
                "cpd_policy_editor",
                "added_atoms_editor",
                "merge_target_editor",
            )
        )
        for editor in editors:
            close = getattr(editor, "close", None)
            if callable(close):
                close()
        seen: set = set()
        for child in tuple(self.content.children):
            self._close_widget_tree(child, seen)
        self.content.children = ()
        self._required_widgets = {}
        self._atom_widgets = {}
        self._atom_locations = {}
        self._conditional_reason_refreshers = []
        self.atom_step_panels = {}

    def close(self) -> None:
        """Release every comm/model owned by this workbench instance."""

        if self._closed:
            return
        self._cancel_autosave()
        self._close_widget_tree(self.widget)
        self._closed = True

    @staticmethod
    def _batch_instance_status(instance: Mapping[str, Any]) -> str:
        if instance.get("human_confirmed"):
            return "已完成：{}".format(instance.get("verdict") or "已记录")
        if instance.get("has_content"):
            return "实例草稿：{}".format(instance.get("verdict") or "未完成")
        return "未决定"

    @staticmethod
    def _batch_counts(batch: Mapping[str, Any]) -> Dict[str, int]:
        instances = batch["instances"]
        return {
            "total": len(instances),
            "complete": sum(bool(item.get("human_confirmed")) for item in instances),
            "decided": sum(bool(item.get("has_content")) for item in instances),
            "blank": sum(not item.get("has_content") for item in instances),
        }

    def _atom_batch_label(self, batch: Mapping[str, Any]) -> str:
        counts = self._batch_counts(batch)
        statement = _human_atom_statement(batch["atom"])
        return "[未决定 {}/{}] {}".format(
            counts["blank"], counts["total"], statement
        )

    def _cpd_batch_label(self, batch: Mapping[str, Any]) -> str:
        counts = self._batch_counts(batch)
        policy = batch["policy"]
        dimensions = policy.get("dimensions", [])
        return "[{} · 未决定 {}/{}] candidate={} / eligible={} / dimensions={}".format(
            batch["surface_style"],
            counts["blank"],
            counts["total"],
            policy.get("candidate"),
            policy.get("eligible"),
            len(dimensions) if isinstance(dimensions, list) else "?",
        )

    def _batch_query_table(self, batch: Mapping[str, Any]) -> str:
        rows = []
        for instance in batch["instances"]:
            rows.append(
                "<tr>"
                "<td style='padding:5px;border-bottom:1px solid #edf2f7'>{}</td>"
                "<td style='padding:5px;border-bottom:1px solid #edf2f7'>{}</td>"
                "<td style='padding:5px;border-bottom:1px solid #edf2f7'>{}</td>"
                "<td style='padding:5px;border-bottom:1px solid #edf2f7'>{}</td>"
                "</tr>".format(
                    html.escape(instance["surface_style"]),
                    html.escape(instance["subject_id"]),
                    html.escape(self._batch_instance_status(instance)),
                    html.escape(instance["query_text"]),
                )
            )
        return (
            "<div style='max-height:430px;overflow:auto;border:1px solid #e2e8f0'>"
            "<table style='width:100%;border-collapse:collapse'>"
            "<thead><tr><th>表述</th><th>Query</th><th>实例状态</th>"
            "<th>必须逐条阅读的原文</th></tr></thead><tbody>{}</tbody></table></div>"
        ).format("".join(rows))

    @staticmethod
    def _batch_agent_summary(batch: Mapping[str, Any]) -> str:
        recommendations = sorted(
            {
                str(item.get("agent_recommendation", "unavailable"))
                for item in batch["instances"]
            }
        )
        reasons = sorted(
            {
                str(item.get("agent_reason", "")).strip()
                for item in batch["instances"]
                if str(item.get("agent_reason", "")).strip()
            }
        )
        return (
            "<div style='padding:7px 9px;background:#fffaf0'>"
            "<b>机器提示仅显示一次：</b>{}<br>{}</div>"
        ).format(
            html.escape("、".join(recommendations)),
            "<br>".join(html.escape(reason) for reason in reasons)
            or "无额外理由",
        )

    def _render_atom_batch(self) -> None:
        batch_id = self.atom_batch_select.value
        batch = self._live_atom_batches.get(batch_id)
        if batch is None:
            self.atom_batch_detail.value = "没有可审核的 atom 批次。"
            self.atom_batch_instance.options = []
            return
        counts = self._batch_counts(batch)
        atom = batch["atom"]
        self.atom_batch_detail.value = (
            "<div style='padding:10px;border-left:4px solid #3182ce;background:#ebf8ff'>"
            "<b>同内容 requirement：</b>{}<br>"
            "<b>计分严格程度：</b>{}<br>"
            "覆盖 {} 个实例；{} 个已有草稿，{} 个 subject 已完成。"
            "批量按钮只填写未决定、未完成的实例；已有内容自动保留为例外。"
            "</div>{}{}"
            "<details><summary>正式语义分组键</summary>{}</details>"
        ).format(
            html.escape(_human_atom_statement(atom)),
            html.escape(LAYER_LABELS.get(atom.get("layer"), str(atom.get("layer")))),
            counts["total"],
            counts["decided"],
            counts["complete"],
            self._batch_agent_summary(batch),
            self._batch_query_table(batch),
            self._safe_pre(atom),
        )
        self.atom_batch_instance.options = [
            (
                "{} | {} | {}".format(
                    item["surface_style"],
                    self._batch_instance_status(item),
                    item["subject_id"],
                ),
                item["subject_id"],
            )
            for item in batch["instances"]
        ]

    def _render_cpd_batch(self) -> None:
        batch_id = self.cpd_batch_select.value
        batch = self._live_cpd_batches.get(batch_id)
        if batch is None:
            self.cpd_batch_detail.value = "没有可审核的 CPD 批次。"
            self.cpd_batch_instance.options = []
            return
        counts = self._batch_counts(batch)
        self.cpd_batch_detail.value = (
            "<div style='padding:10px;border-left:4px solid #805ad5;background:#faf5ff'>"
            "<b>{} 表述的同策略 CPD 批次</b><br>覆盖 {} 个实例；{} 个已有草稿，"
            "{} 个 subject 已完成。partial/vague 即使 policy 相同也不会跨表述合并。"
            "</div>{}{}<details><summary>完整 CPD policy</summary>{}</details>"
        ).format(
            html.escape(batch["surface_style"]),
            counts["total"],
            counts["decided"],
            counts["complete"],
            self._batch_agent_summary(batch),
            self._batch_query_table(batch),
            self._safe_pre(batch["policy"]),
        )
        self.cpd_batch_instance.options = [
            (
                "{} | {} | {}".format(
                    item["surface_style"],
                    self._batch_instance_status(item),
                    item["subject_id"],
                ),
                item["subject_id"],
            )
            for item in batch["instances"]
        ]

    def _refresh_batch_options(self) -> None:
        atom_current = getattr(self, "atom_batch_select", None)
        atom_current = atom_current.value if atom_current is not None else None
        cpd_current = getattr(self, "cpd_batch_select", None)
        cpd_current = cpd_current.value if cpd_current is not None else None
        self._live_atom_batches = {
            batch["batch_id"]: batch for batch in self.session.atom_review_batches()
        }
        self._live_cpd_batches = {
            batch["batch_id"]: batch for batch in self.session.cpd_review_batches()
        }
        self._suppress_batch_change = True
        try:
            atom_batches = list(self._live_atom_batches.values())
            atom_batches.sort(
                key=lambda batch: (
                    self._batch_counts(batch)["blank"] == 0,
                    -self._batch_counts(batch)["blank"],
                    self._atom_batch_label(batch),
                )
            )
            self.atom_batch_select.options = [
                (self._atom_batch_label(batch), batch["batch_id"])
                for batch in atom_batches
            ]
            atom_ids = set(self._live_atom_batches)
            self.atom_batch_select.value = (
                atom_current
                if atom_current in atom_ids
                else atom_batches[0]["batch_id"] if atom_batches else None
            )
            cpd_batches = list(self._live_cpd_batches.values())
            cpd_batches.sort(
                key=lambda batch: (
                    self._batch_counts(batch)["blank"] == 0,
                    SURFACE_ORDER[batch["surface_style"]],
                    -self._batch_counts(batch)["blank"],
                    self._cpd_batch_label(batch),
                )
            )
            self.cpd_batch_select.options = [
                (self._cpd_batch_label(batch), batch["batch_id"])
                for batch in cpd_batches
            ]
            cpd_ids = set(self._live_cpd_batches)
            self.cpd_batch_select.value = (
                cpd_current
                if cpd_current in cpd_ids
                else cpd_batches[0]["batch_id"] if cpd_batches else None
            )
        finally:
            self._suppress_batch_change = False
        self._render_atom_batch()
        self._render_cpd_batch()

    def _open_batch_instance(self, query_id: Optional[str]) -> None:
        if not query_id or not self._save_dirty_checkpoint():
            return
        self.filter.value = "all"
        self.search.value = ""
        values = [value for _, value in self.query_select.options]
        if query_id in values:
            self.query_select.value = query_id

    def _apply_atom_batch(self, _: Any) -> None:
        if not self.atom_batch_ack.value:
            self._notify("请先确认你已阅读批次内全部 query 原文。", error=True)
            return
        if not self._save_dirty_checkpoint():
            return
        try:
            result = self.session.apply_atom_batch_decision(
                self.atom_batch_select.value,
                self.atom_batch_action.value,
                reason=self.atom_batch_reason.value,
            )
            self.atom_batch_ack.value = False
            self._refresh_batch_options()
            self._refresh_header()
            self._refresh_query_options()
            self._notify(
                "atom 批次已写入 {} 个未决定实例；保留 {} 个已有草稿和 {} 个已完成实例。"
                "所有 subject 仍需逐题完成。".format(
                    result["applied"],
                    result["skipped_existing"],
                    result["skipped_complete"],
                )
            )
        except Exception as exc:
            self._notify(str(exc), error=True)

    def _apply_cpd_batch(self, _: Any) -> None:
        if not self.cpd_batch_ack.value:
            self._notify("请先确认你已阅读批次内全部 query 原文。", error=True)
            return
        if not self._save_dirty_checkpoint():
            return
        try:
            result = self.session.apply_cpd_batch_decision(
                self.cpd_batch_select.value,
                self.cpd_batch_verdict.value,
                reason=self.cpd_batch_reason.value,
                replacement_policy_json=self.cpd_batch_replacement.value,
            )
            self.cpd_batch_ack.value = False
            self._refresh_batch_options()
            self._refresh_header()
            self._refresh_query_options()
            self._notify(
                "CPD 批次已写入 {} 个未决定实例；保留 {} 个已有草稿和 {} 个已完成实例。"
                "所有 subject 仍需逐题完成。".format(
                    result["applied"],
                    result["skipped_existing"],
                    result["skipped_complete"],
                )
            )
        except Exception as exc:
            self._notify(str(exc), error=True)

    def _build_batch_panel(self) -> Any:
        w = self.widgets
        self._suppress_batch_change = False
        self._live_atom_batches: Dict[str, Dict[str, Any]] = {}
        self._live_cpd_batches: Dict[str, Dict[str, Any]] = {}

        self.atom_batch_select = w.Dropdown(
            description="内容批次", layout=w.Layout(width="100%")
        )
        self.atom_batch_detail = w.HTML()
        self.atom_batch_action = w.Dropdown(
            description="批量草稿",
            options=[
                ("保留当前要求", "accept"),
                ("不作为评分要求", "reject"),
                ("改为允许但不强制", "permitted"),
            ],
            value="accept",
        )
        self.atom_batch_reason = w.Textarea(
            description="分歧依据",
            placeholder="批量不计分时必填；改为允许时可留空使用标准理由",
            layout=w.Layout(width="75%", height="72px"),
        )
        self.atom_batch_ack = w.Checkbox(
            description="我已逐条阅读上方该批覆盖的全部 query 原文", indent=False
        )
        self.atom_batch_apply = w.Button(
            description="写入未决定实例草稿", button_style="info"
        )
        self.atom_batch_instance = w.Dropdown(
            description="实例例外", layout=w.Layout(width="80%")
        )
        self.atom_batch_open = w.Button(description="转到逐题处理")

        self.cpd_batch_select = w.Dropdown(
            description="策略批次", layout=w.Layout(width="100%")
        )
        self.cpd_batch_detail = w.HTML()
        self.cpd_batch_verdict = w.Dropdown(
            description="批量草稿",
            options=[
                ("接受当前 CPD", "accept"),
                ("修改 CPD policy", "revise"),
                ("CPD source 有误", "reject"),
            ],
            value="accept",
        )
        self.cpd_batch_reason = w.Textarea(
            description="修改依据",
            placeholder="修改或拒绝时必填",
            layout=w.Layout(width="75%", height="72px"),
        )
        self.cpd_batch_replacement = w.Textarea(
            description="新 policy",
            value="{}",
            placeholder="仅 revise 时填写 JSON object",
            layout=w.Layout(width="75%", height="130px"),
        )
        self.cpd_batch_ack = w.Checkbox(
            description="我已逐条阅读上方该批覆盖的全部 query 原文", indent=False
        )
        self.cpd_batch_apply = w.Button(
            description="写入未决定实例草稿", button_style="info"
        )
        self.cpd_batch_instance = w.Dropdown(
            description="实例例外", layout=w.Layout(width="80%")
        )
        self.cpd_batch_open = w.Button(description="转到逐题处理")

        self.atom_batch_select.observe(
            lambda change: None
            if self._suppress_batch_change
            else self._render_atom_batch(),
            names="value",
        )
        self.cpd_batch_select.observe(
            lambda change: None
            if self._suppress_batch_change
            else self._render_cpd_batch(),
            names="value",
        )
        self.atom_batch_apply.on_click(self._apply_atom_batch)
        self.cpd_batch_apply.on_click(self._apply_cpd_batch)
        self.atom_batch_open.on_click(
            lambda _: self._open_batch_instance(self.atom_batch_instance.value)
        )
        self.cpd_batch_open.on_click(
            lambda _: self._open_batch_instance(self.cpd_batch_instance.value)
        )

        tabs = w.Tab(
            children=[
                w.VBox(
                    [
                        w.HTML(
                            "<b>相同 requirement 内容集中审核。</b>批量操作只写草稿，"
                            "不覆盖实例例外、不完成 subject；修改/拆分/合并等复杂例外转到逐题处理。"
                        ),
                        self.atom_batch_select,
                        self.atom_batch_detail,
                        w.HBox([self.atom_batch_action, self.atom_batch_reason]),
                        self.atom_batch_ack,
                        self.atom_batch_apply,
                        w.HBox([self.atom_batch_instance, self.atom_batch_open]),
                    ]
                ),
                w.VBox(
                    [
                        w.HTML(
                            "<b>相同 CPD policy 按 precise/partial/vague 分开审核。</b>"
                            "批量操作只写草稿，实例差异转到逐题处理。"
                        ),
                        self.cpd_batch_select,
                        self.cpd_batch_detail,
                        w.HBox([self.cpd_batch_verdict, self.cpd_batch_reason]),
                        self.cpd_batch_replacement,
                        self.cpd_batch_ack,
                        self.cpd_batch_apply,
                        w.HBox([self.cpd_batch_instance, self.cpd_batch_open]),
                    ]
                ),
            ]
        )
        tabs.set_title(0, "相同要求批审")
        tabs.set_title(1, "CPD 策略批审")
        return tabs

    def _build_shell(self) -> None:
        w = self.widgets
        self.header = w.HTML()
        self.dirty_badge = w.HTML()
        self.progress = w.IntProgress(min=0, max=1, value=0, description="已审核")
        self.progress_text = w.HTML()
        self.filter = w.Dropdown(
            options=[
                ("全部", "all"),
                ("需优先处理", "attention"),
                ("必须人工判断", "human_judgment"),
                ("未开始", "pending"),
                ("草稿/未通过", "draft"),
                ("本地有效", "locally_valid"),
                ("需修源/阻塞", "blocked"),
                ("已确认", "complete"),
            ],
            value="attention",
            description="筛选",
        )
        self.search = w.Text(description="搜索", placeholder="query / intent / 文本")
        self.query_select = w.Dropdown(description="Query", layout=w.Layout(width="72%"))
        self.previous_button = w.Button(description="← 上一项")
        self.next_button = w.Button(description="下一项 →")
        self.save_button = w.Button(description="保存当前草稿", button_style="info")
        self.complete_button = w.Button(description="完成本条审核", button_style="success")
        self.complete_next_button = w.Button(
            description="完成并继续 →", button_style="success",
            tooltip="校验通过后，进入当前筛选和搜索范围内的下一条未完成请求",
        )
        self.guide_button = w.Button(description="标注指南", icon="book")
        self.batch_button = w.Button(description="同内容批审", icon="th-list")
        self.mechanical_ack = w.Checkbox(
            description="我确认用机器草稿预填并覆盖当前未完成内容",
            indent=False,
        )
        self.mechanical_button = w.Button(
            description="预填机器草稿（仍需逐条复核）", disabled=True
        )
        self.reset_ack = w.Checkbox(
            description="我确认清空当前 subject 的 checkpoint", indent=False
        )
        self.reset_button = w.Button(description="清空当前项", disabled=True)
        self.finalize_confirmation = w.Text(
            description="最终确认", placeholder="全部完成后输入 FINALIZE"
        )
        default_final = self.session.checkpoint_path.parent / "finalized" / self.session.split
        self.finalize_output = w.Text(
            description="输出目录", value=str(default_final), layout=w.Layout(width="80%")
        )
        self.finalize_button = w.Button(description="定稿整个 split", button_style="danger")
        self.message = w.Output(layout=w.Layout(border="1px solid #ddd"))
        self.content = w.VBox()
        self.batch_panel = self._build_batch_panel()

        self.filter.observe(self._on_filter_change, names="value")
        self.search.observe(self._on_filter_change, names="value")
        self.query_select.observe(self._on_query_change, names="value")
        self.mechanical_ack.observe(
            lambda change: setattr(self.mechanical_button, "disabled", not change["new"]),
            names="value",
        )
        self.reset_ack.observe(
            lambda change: setattr(self.reset_button, "disabled", not change["new"]),
            names="value",
        )
        self.previous_button.on_click(lambda _: self._navigate(-1))
        self.next_button.on_click(lambda _: self._navigate(1))
        self.save_button.on_click(lambda _: self._save_current(False))
        self.complete_button.on_click(lambda _: self._save_current(True))
        self.complete_next_button.on_click(self._complete_and_continue)
        self.mechanical_button.on_click(self._load_mechanical)
        self.reset_button.on_click(self._reset_current)
        self.finalize_button.on_click(self._finalize)

        warning = w.HTML(
            "<div style='padding:10px 12px;border-left:4px solid #2b6cb0;background:#ebf8ff'>"
            "<b>你正在创建人工评分标准，不是在修改场景。</b> "
            "每张要求卡都问：生成结果是否必须满足这件事，还是只允许出现、不得出现或根本不该计分。"
            "机器草稿和自动保存都不是 human gold；只有全 split 定稿后才产生 gold。"
            "</div>"
        )
        helper_panel = w.VBox(
            [
                w.HTML(
                    "<b>机器辅助</b><br>建议先独立阅读 query。只有需要加速填写时才预填；"
                    "预填不会代表你已同意任何判断。"
                ),
                w.HBox([self.mechanical_ack, self.mechanical_button]),
                w.HTML("<hr><b>危险操作</b>"),
                w.HBox([self.reset_ack, self.reset_button]),
            ]
        )
        self.helper_accordion = w.Accordion(children=[helper_panel])
        self.helper_accordion.set_title(0, "机器辅助与清空操作（默认不需要）")
        self.helper_accordion.selected_index = None

        finalization_panel = w.VBox(
            [
                w.HTML(
                    "只有当前 split 的每条 query 都完成审核后才使用。定稿会调用正式 finalizer；"
                    "输出目录必须是尚不存在的新目录。"
                ),
                w.HBox([self.finalize_confirmation, self.finalize_button]),
                self.finalize_output,
            ]
        )
        self.finalization_accordion = w.Accordion(children=[finalization_panel])
        self.finalization_accordion.set_title(0, "整个 split 的最终定稿（全部完成后）")
        self.finalization_accordion.selected_index = None
        self.completion_issues = w.VBox()
        self.completion_issues.layout.display = "none"
        self.review_heading = w.HTML(
            "<b>逐条审核</b> · 对照当前请求，选择每张要求卡的结论。",
        ).add_class("review-start")
        self.batch_accordion = w.Accordion(children=[self.batch_panel])
        self.batch_accordion.set_title(0, "批审：相同要求与相同 CPD 策略（只填草稿）")
        self.batch_accordion.selected_index = None
        self.guide_accordion = w.Accordion(children=[self._build_review_guide()])
        self.guide_accordion.set_title(0, "标注指南：计分要求、判断依据与完成规则")
        self.guide_accordion.selected_index = None
        for panel in (self.guide_accordion, self.batch_accordion):
            panel.layout.display = "none"
            panel.observe(
                lambda change, layout=panel.layout: setattr(
                    layout, "display", "none" if change["new"] is None else ""
                ),
                names="selected_index",
            )
        self.guide_button.on_click(
            lambda _: setattr(
                self.guide_accordion, "selected_index",
                0 if self.guide_accordion.selected_index is None else None,
            )
        )
        self.batch_button.on_click(
            lambda _: setattr(
                self.batch_accordion, "selected_index",
                0 if self.batch_accordion.selected_index is None else None,
            )
        )
        styles = w.HTML(
            "<style>{}</style>".format(
                Path(__file__).with_suffix(".css").read_text(encoding="utf-8")
            )
        )
        self.widget = w.VBox(
            [
                styles,
                warning,
                self.header,
                w.HBox([self.progress, self.progress_text, self.dirty_badge]).add_class("review-toolbar"),
                w.HBox([self.filter, self.search, self.guide_button, self.batch_button]).add_class("review-toolbar"),
                self.guide_accordion,
                self.batch_accordion,
                w.HBox([self.previous_button, self.query_select, self.next_button]).add_class("review-toolbar"),
                self.completion_issues,
                self.review_heading,
                self.content,
                w.HBox([self.save_button, self.complete_button, self.complete_next_button]).add_class("review-actions"),
                self.message,
                self.helper_accordion,
                self.finalization_accordion,
            ]
        ).add_class("query-review-workbench")
        self._refresh_batch_options()
        self._refresh_header()

    def _build_review_guide(self) -> Any:
        """Use the same labels and criteria as the cards, without changing answers."""

        layers = "".join("<li>{}</li>".format(html.escape(label)) for label in LAYER_LABELS.values())
        checks = "".join(
            "<li><b>{}</b>：{}</li>".format(html.escape(CHECK_SHORT_LABELS[check]), html.escape(CHECK_HELP[check]))
            for check in REQUIRED_REVIEW_CHECKS
        )
        return self.widgets.HTML(
            "<div class='review-guide'><b>先读当前请求，再判断候选要求。</b>"
            "同 intent 的其他表述仅供对照，不能用它们补齐当前文本没有说出的细节。"
            "<h4>计分严格程度</h4><ul>{}</ul>"
            "<h4>常用操作</h4><ul>"
            "<li>内容与严格程度均正确：点击“正确”。</li>"
            "<li>不应计分：点击“不应计分”并说明依据；这不会删除场景元素。</li>"
            "<li>细节可以出现但缺失不应扣分：点击“允许但不强制”。</li>"
            "<li>内容有误或存在重复、遗漏：使用高级修改、拆分、合并或补充漏项。</li>"
            "</ul><h4>五步审核的共同判据</h4><ul>{}</ul>"
            "<h4>草稿、疑问与完成</h4>"
            "<p>“待选”只统计尚未选择结论的项目；“已选”不表示校验通过。"
            "不确定时可保留未判断并写备注、保存草稿，后续从“草稿/未通过”继续。"
            "源数据错误需要修源，不能强行完成。</p>"
            "<p>四项一致性总结自动派生；请求处理方式与 CPD 仍需人工判断。"
            "机器提示、批审、继承和自动保存均不替代人工确认。"
            "“完成并继续”只在正式逐题校验通过后跳到当前筛选和搜索内的下一条未完成请求；"
            "全部完成后仍须单独定稿整个 split。</p></div>".format(layers, checks)
        )

    def _notify(self, text: str, *, error: bool = False) -> None:
        from IPython.display import display

        with self.message:
            self.message.clear_output()
            color = "#b00020" if error else "#155724"
            display(self.widgets.HTML("<span style='color:{}'>{}</span>".format(color, html.escape(text))))

    def _clear_completion_issues(self) -> None:
        children = tuple(self.completion_issues.children)
        self.completion_issues.children = ()
        self.completion_issues.layout.display = "none"
        seen: set = set()
        for child in children:
            self._close_widget_tree(child, seen)

    def _refresh_header(self) -> None:
        p = self.session.progress()
        self.progress.max = p["subjects_total"]
        self.progress.value = p["subjects_complete"]
        self.progress_text.value = (
            "<b>{}/{}</b> 条已确认；<b>{}/{}</b> 项已填；定稿前尚未生成正式人工标准"
        ).format(
            p["subjects_complete"],
            p["subjects_total"],
            p["decision_slots_completed"],
            p["decision_slots_total"],
        )
        self.header.value = (
            "<b>审核批次：</b>{} &nbsp; <b>标注者：</b>{}"
            "<details><summary>任务绑定与保存位置（技术参考）</summary>"
            "<b>Packet:</b> <code>{}</code><br>"
            "<b>Checkpoint:</b> <code>{}</code></details>"
        ).format(
            html.escape(self.session.split),
            html.escape(self.session.reviewer_id),
            html.escape(self.session.packet["packet_id"][:16]),
            html.escape(str(self.session.checkpoint_path)),
        )
        self.finalize_button.disabled = p["subjects_complete"] != p["subjects_total"]

    def _query_label(self, query_id: str) -> str:
        task = self.session.task(query_id)
        record = task["query_record"]
        return "[{}] {} | {} | {}".format(
            self.session.status(query_id),
            record["intent_group_id"],
            record["surface_style"],
            query_id,
        )

    def _refresh_query_options(self, change: Any = None) -> None:
        query_ids = self.session.filtered_query_ids(self.filter.value, self.search.value)
        current = self.current_query_id if self.current_query_id in query_ids else None
        self._suppress_query_change = True
        try:
            self.query_select.options = [
                (self._query_label(query_id), query_id) for query_id in query_ids
            ]
            self.query_select.value = (current or query_ids[0]) if query_ids else None
        finally:
            self._suppress_query_change = False
        if query_ids:
            self.current_query_id = self.query_select.value
            self._render_current()
        else:
            self.current_query_id = None
            self._clear_completion_issues()
            self._discard_rendered_subject()
            self.content.children = [self.widgets.HTML("没有符合筛选条件的 query。")]
        for button in (self.save_button, self.complete_button, self.complete_next_button):
            button.disabled = not bool(query_ids)
        self.review_heading.layout.display = "" if query_ids else "none"

    def _set_dirty(self, value: bool) -> None:
        self.dirty = bool(value)
        self.dirty_badge.value = (
            "<span style='color:#b00020'><b>当前 subject 有未保存更改</b></span>"
            if self.dirty
            else "<span style='color:#155724'>当前 subject 已保存</span>"
        )

    def _cancel_autosave(self) -> None:
        if self._autosave_handle is not None:
            self._autosave_handle.cancel()
            self._autosave_handle = None

    def _schedule_autosave(self) -> None:
        self._cancel_autosave()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        query_id = self.current_query_id
        self._autosave_handle = loop.call_later(
            0.75, self._autosave_current, query_id
        )

    def _autosave_current(self, query_id: Optional[str]) -> None:
        self._autosave_handle = None
        if query_id == self.current_query_id and self.dirty:
            self._save_dirty_checkpoint(notify=False)

    def _save_dirty_checkpoint(self, *, notify: bool = True) -> bool:
        if not self.dirty or not self.current_query_id:
            return True
        self._cancel_autosave()
        try:
            self.session.save_form(self.current_query_id, self._collect_form())
            self._set_dirty(False)
            self._refresh_header()
            if notify:
                self._notify(
                    "已保存当前 checkpoint；它不是 submission 或 gold。"
                )
            return True
        except Exception as exc:
            self._notify(str(exc), error=True)
            return False

    def _on_filter_change(self, change: Mapping[str, Any]) -> None:
        if self._save_dirty_checkpoint():
            self._refresh_query_options()

    def _on_query_change(self, change: Mapping[str, Any]) -> None:
        if self._suppress_query_change:
            return
        query_id = change.get("new")
        previous = self.current_query_id
        if not query_id or query_id == previous:
            return
        if not self._save_dirty_checkpoint():
            self._suppress_query_change = True
            try:
                if previous in [value for _, value in self.query_select.options]:
                    self.query_select.value = previous
            finally:
                self._suppress_query_change = False
            return
        self.current_query_id = query_id
        self._render_current()

    def _observe_dirty(self, widget: Any) -> None:
        def _changed(_: Any) -> None:
            self._structured_changed()

        widget.observe(_changed, names="value")

    def _configure_conditional_reason(
        self,
        verdict: Any,
        reason: Any,
        *,
        auto_reasons: Mapping[str, str],
        manual_verdicts: Sequence[str],
        manual_prompt: str,
        conditional_auto_verdicts: Optional[Mapping[str, Any]] = None,
        unavailable_prompt: str = "",
    ) -> Any:
        """Show free text only when the selected verdict needs human prose.

        Per-verdict drafts are retained in memory while the reviewer changes a
        selection, so switching to an automatic rationale never destroys an
        unfinished manual explanation.  Only the active verdict is serialized.
        """

        status = self.widgets.HTML()
        drafts = {verdict.value: reason.value}
        previous = [verdict.value]
        conditional_auto_verdicts = dict(conditional_auto_verdicts or {})

        def _render(selected: str, *, initial: bool) -> None:
            if selected in auto_reasons:
                available = conditional_auto_verdicts.get(selected)
                if available is not None and not available():
                    current = str(reason.value or "").strip()
                    reason.layout.display = (
                        "" if current and not _is_generated_reason(current) else "none"
                    )
                    status.value = (
                        "<small style='color:#9c2f00'><b>尚不能自动确认：</b>{}</small>".format(
                            html.escape(
                                unavailable_prompt
                                or "尚未检测到与该总结对应的结构化修改。"
                            )
                        )
                    )
                    return
                if not initial:
                    candidate = drafts.get(selected, "")
                    reason.value = _normalized_auto_reason(
                        candidate, auto_reasons[selected]
                    )
                reason.layout.display = "none"
                current = str(reason.value or "").strip()
                suffix = (
                    "已有人工自定义依据，将原样保留。"
                    if current and not _is_generated_reason(current)
                    else "保存时会写入标准人工确认，不需要手写。"
                )
                status.value = (
                    "<small style='color:#2f855a'><b>自动审计依据：</b>{}</small>".format(
                        html.escape(suffix)
                    )
                )
                return
            if selected in manual_verdicts:
                if not initial:
                    candidate = drafts.get(selected, "")
                    if not candidate:
                        orphan = drafts.get("", "")
                        if orphan and not _is_generated_reason(orphan):
                            candidate = orphan
                    reason.value = "" if _is_generated_reason(candidate) else candidate
                reason.layout.display = ""
                status.value = (
                    "<small style='color:#9c2f00'><b>需要人工说明：</b>{}</small>".format(
                        html.escape(manual_prompt)
                    )
                )
                return
            orphan = str(reason.value or "").strip()
            reason.layout.display = "" if orphan else "none"
            status.value = (
                "<small style='color:#b7791f'><b>未归类理由草稿：</b>"
                "选择需要解释的 verdict 后会自动带入。</small>"
                if orphan
                else "<small style='color:#4a5568'>先选择结论；只有分歧或语义修改才需要手写依据。</small>"
            )

        def _verdict_changed(change: Mapping[str, Any]) -> None:
            old = change.get("old", previous[0])
            drafts[old] = reason.value
            previous[0] = change.get("new", "")
            _render(previous[0], initial=False)

        _render(verdict.value, initial=True)
        verdict.observe(_verdict_changed, names="value")
        self._conditional_reason_refreshers.append(
            lambda: _render(verdict.value, initial=True)
        )
        return status

    def _structured_changed(self) -> None:
        for refresh in tuple(self._conditional_reason_refreshers):
            refresh()
        self._set_dirty(True)
        self._refresh_review_progress()
        self._schedule_autosave()

    def _refresh_review_progress(self) -> None:
        """Display selection progress only; the production validator decides completion."""

        if self._rendering or not self.current_query_id or not hasattr(self, "review_tabs"):
            return
        pending = [0] * len(REVIEW_STEP_TITLES)
        for atom_id, controls in self._atom_widgets.items():
            verdict = controls["verdict"].value
            if not verdict:
                pending[ATOM_STEP_TABS[self._atom_locations[atom_id]["step"]]] += 1
            controls["status"].value = "<span class='review-status'>{}</span>".format(
                html.escape("已选：" + ATOM_VERDICT_LABELS[verdict] if verdict else "待判断")
            )
            for value, button in controls["quick_buttons"].items():
                button.button_style = "success" if verdict == value else ""
        pending[3] = int(not self._required_widgets["support_and_response_disposition"]["verdict"].value)
        pending[4] = int(not self.cpd_verdict.value)
        for index, title in enumerate(REVIEW_STEP_TITLES):
            self.review_tabs.set_title(index, "{} · {}".format(
                title, "待选 {}".format(pending[index]) if pending[index] else "已选齐",
            ))

    def _preview_proposed_oracle(self) -> Optional[Dict[str, Any]]:
        """Build the current structured proposal with undecided items retained.

        This preview is used only to decide whether a high-level ``revise`` is
        backed by the same before/after projection as the production closure
        validator.  It never creates a response, checkpoint, or gold record.
        """

        if not self.current_query_id or not self._atom_widgets:
            return None
        try:
            atom_decisions = []
            for atom_id, widgets in self._atom_widgets.items():
                verdict = widgets["verdict"].value or "accept"
                replacements = widgets["replacement_editor"].value()
                if verdict in ("accept", "reject"):
                    replacements = []
                atom_decisions.append(
                    {
                        "atom_id": atom_id,
                        "verdict": verdict,
                        "reason": widgets["reason"].value or ATOM_ACCEPT_REASON,
                        "replacement_atoms_json": _pretty(replacements),
                    }
                )
            cpd_verdict = self.cpd_verdict.value or "accept"
            probe = {
                "required_check_decisions": {
                    check: {"verdict": "accept", "reason": CHECK_ACCEPT_REASON}
                    for check in REQUIRED_REVIEW_CHECKS
                },
                "atom_decisions": atom_decisions,
                "cpd_decision": {
                    "verdict": cpd_verdict,
                    "reason": self.cpd_reason.value or CPD_ACCEPT_REASON,
                    "replacement_policy_json": _pretty(
                        self.cpd_policy_editor.value()
                    ),
                },
                "added_atoms_json": _pretty(self.added_atoms_editor.value()),
                "notes": self.notes.value,
            }
            return response_from_form(
                self.session.task(self.current_query_id), probe
            )["proposed_oracle"]
        except (AttributeError, KeyError, TypeError, ValueError, ValidationError):
            return None

    def _required_check_has_structural_change(self, check: str) -> bool:
        proposed = self._preview_proposed_oracle()
        if proposed is None:
            return False
        draft = self.session.task(self.current_query_id)["oracle_draft"]
        return query_check_projection(check, draft) != query_check_projection(
            check, proposed
        )

    def _cpd_policy_has_structural_change(self, form: Mapping[str, Any]) -> bool:
        """Compare the saved CPD editor value even when its verdict ignores it."""

        try:
            replacement = _read_json_text(
                str(form["cpd_decision"].get("replacement_policy_json", "{}")),
                "CPD replacement policy",
                dict,
            )
            replacement["decision_status"] = "draft"
            draft = self.session.task(self.current_query_id)["oracle_draft"]
            edited = copy.deepcopy(draft)
            edited["cpd_policy"] = replacement
            return query_check_projection(
                "cpd_common_eligibility", draft
            ) != query_check_projection("cpd_common_eligibility", edited)
        except (AttributeError, KeyError, TypeError, ValueError, ValidationError):
            return False

    def _render_current(self) -> None:
        w = self.widgets
        self._cancel_autosave()
        query_id = self.current_query_id
        if not query_id:
            return
        self._rendering = True
        self._clear_completion_issues()
        self._discard_rendered_subject()
        task = self.session.task(query_id)
        form = self.session.form(query_id)
        record = task["query_record"]
        mapped_atom_ids_by_check = {
            check: self._mapped_source_atom_ids(task, check)
            for check in REQUIRED_REVIEW_CHECKS
        }
        mapped_checks_by_atom: Dict[str, List[str]] = {
            atom["atom_id"]: [] for atom in task["oracle_draft"].get("atoms", [])
        }
        for check, atom_ids in mapped_atom_ids_by_check.items():
            if check == "cpd_common_eligibility":
                continue
            for atom_id in atom_ids:
                mapped_checks_by_atom[atom_id].append(check)
        triplet_rows = []
        for sibling_id in self.session.intent_query_ids(query_id):
            sibling = self.session.task(sibling_id)
            triplet_rows.append(
                "<tr><td style='padding:5px'>{}</td><td style='padding:5px'>{}</td></tr>".format(
                    html.escape(
                        {
                            "precise": "精确表述",
                            "partial": "部分信息",
                            "vague": "模糊表述",
                        }.get(
                            sibling["query_record"]["surface_style"],
                            sibling["query_record"]["surface_style"],
                        )
                    ),
                    html.escape(sibling["query_text"]),
                )
            )
        expected_support = record.get("expected_support")
        query_summary = w.HTML(
            "<div style='padding:14px;border:1px solid #cbd5e0;border-radius:6px'>"
            "<div style='color:#4a5568'>{} · {} · {}</div>"
            "<h3 style='margin:8px 0'>当前要审核的用户请求</h3>"
            "<div style='font-size:17px;line-height:1.55'>{}</div>"
            "<p><b>预期处理：</b>{}　<b>checkpoint 状态：</b>{}</p>"
            "<div style='padding:8px;background:#fffaf0'>"
            "<b>本条审核只以这段文字为计分依据。</b> 同 intent 的其他表述只用于发现信息泄漏，"
            "不能把它们的细节补进当前要求。"
            "</div></div>".format(
                html.escape(query_id),
                html.escape(
                    {
                        "precise": "精确表述",
                        "partial": "部分信息",
                        "vague": "模糊表述",
                    }.get(record.get("surface_style"), str(record.get("surface_style")))
                ),
                html.escape(record.get("intent_group_id", "")),
                html.escape(task["query_text"]),
                "生成场景" if expected_support == "supported" else "明确拒绝该请求",
                html.escape(self.session.status(query_id)),
            )
        )
        reference_panel = w.HTML(
            "<table style='width:100%;border-collapse:collapse'>"
            "<tr><th style='text-align:left;padding:5px'>表述</th>"
            "<th style='text-align:left;padding:5px'>同一 intent 的文本（仅对照）</th></tr>{}"
            "</table><details><summary>完整结构化元数据（技术参考）</summary>{}</details>".format(
                "".join(triplet_rows), self._safe_pre(record)
            )
        )
        reference_accordion = w.Accordion(children=[reference_panel])
        reference_accordion.set_title(0, "对照材料：同 intent 三种表述与完整元数据")
        reference_accordion.selected_index = None
        inheritance_children = []
        precise_source = self.session._completed_precise_query_id(query_id)
        if (
            record.get("surface_style") in ("partial", "vague")
            and precise_source is not None
            and self.session.status(query_id) != "complete"
        ):
            inheritance_children.append(
                w.HTML(
                    "<div style='padding:8px 10px;background:#ebf8ff;border-left:4px solid #3182ce'>"
                    "<b>同 intent 的 precise 已完成：</b><code>{}</code>。"
                    "当前 surface 仍是独立、未完成的审核；请重点核对当前文本少了哪些信息，"
                    "不能因为 precise 提到某细节就继续把它设为必须满足。"
                    "</div>".format(html.escape(precise_source))
                )
            )
            if self.session.can_seed_from_precise(query_id):
                seed_button = w.Button(
                    description="从 precise 创建未完成草稿",
                    icon="copy",
                    button_style="info",
                )

                def _seed_current_from_precise(
                    _button: Any,
                    target_query_id: str = query_id,
                ) -> None:
                    if self.dirty:
                        self._notify(
                            "当前 subject 有未保存编辑；为避免覆盖，请先保存或清空后再继承。",
                            error=True,
                        )
                        return
                    try:
                        source_query_id = self.session.seed_from_completed_precise(
                            target_query_id
                        )
                        self._refresh_header()
                        self._refresh_query_options()
                        self._notify(
                            "已从 {} 创建当前 surface 的继承草稿；状态仍为未完成，"
                            "请核对差异后显式完成。".format(source_query_id)
                        )
                    except Exception as exc:
                        self._notify(str(exc), error=True)

                seed_button.on_click(_seed_current_from_precise)
                inheritance_children.append(seed_button)
        query_panel = w.VBox(
            [query_summary] + inheritance_children + [reference_accordion]
        ).add_class("review-query")

        agent = self.session.agent_review(query_id)
        findings = agent.get("semantic_findings", [])
        agent_panel = w.HTML(
            "<p><b>机器草稿总体提示：</b>{}；<b>置信度：</b>{}</p>{}".format(
                html.escape(_human_token(agent.get("overall_disposition", "unavailable"))),
                html.escape(_human_token(agent.get("confidence", "unavailable"))),
                "".join(
                    "<div style='margin:6px 0;padding:6px;border-left:3px solid #d98c00'>"
                    "<b>需关注：{} / {}</b><br>{}<br><i>{}</i></div>".format(
                        html.escape(_human_token(item.get("target_type"))),
                        html.escape(_human_token(item.get("target"))),
                        html.escape(str(item.get("reason"))),
                        html.escape(str(item.get("suggested_change"))),
                    )
                    for item in findings
                )
                or "<p>机器没有给出特别提示；仍需逐条人工检查。</p>",
            )
        )

        self._required_widgets = {}
        required_row_by_check = {}
        for check in REQUIRED_REVIEW_CHECKS:
            decision = form["required_check_decisions"][check]
            verdict = w.Dropdown(
                options=REQUIRED_VERDICT_OPTIONS,
                value=decision.get("verdict", ""),
                description="结论",
                layout=w.Layout(width="42%"),
            )
            reason = w.Textarea(
                value=decision.get("reason", ""),
                description="依据",
                placeholder="指出 query library 或 oracle source 的具体错误",
                layout=w.Layout(width="100%", height="78px"),
            )
            recommendation = agent.get("required_check_decisions", {}).get(check, {})
            info = w.HTML(
                "<b>{}</b><br><span style='color:#4a5568'>{}</span><br>"
                "<small>机器提示：{} — {}</small>".format(
                    html.escape(CHECK_LABELS[check]),
                    html.escape(CHECK_HELP[check]),
                    html.escape(
                        _human_agent_recommendation(
                            recommendation.get("recommendation", "unavailable")
                        )
                    ),
                    html.escape(str(recommendation.get("reason", ""))),
                )
            )
            mapping_info = w.HTML(
                self._check_mapping_html(
                    task, check, mapped_atom_ids_by_check[check]
                )
            )
            if check in DERIVED_REQUIRED_CHECKS:
                verdict.disabled = True
                reason.disabled = True
                reason.layout.display = "none"
                reason_status = w.HTML()
                exception_reason = [
                    decision.get("reason", "")
                    if decision.get("verdict") == "reject"
                    else ""
                ]
                exception = w.Checkbox(
                    description="推导结果或其非 atom 源字段有误，阻止完成",
                    value=decision.get("verdict") == "reject",
                    indent=False,
                )

                def _refresh_derived_check(
                    check_name: str = check,
                    verdict_widget: Any = verdict,
                    reason_widget: Any = reason,
                    status_widget: Any = reason_status,
                    exception_widget: Any = exception,
                ) -> None:
                    if exception_widget.value:
                        verdict_widget.value = "reject"
                        reason_widget.disabled = False
                        reason_widget.layout.display = ""
                        status_widget.value = (
                            "<small style='color:#9c2f00'><b>需要人工说明：</b>"
                            "指出自动投影、ego gate、surface style 或其他 source 字段"
                            "为什么有误；当前 subject 将保持阻塞。</small>"
                        )
                        return
                    changed = self._required_check_has_structural_change(check_name)
                    verdict_widget.value = "revise" if changed else "accept"
                    reason_widget.value = (
                        CHECK_REVISE_REASON if changed else CHECK_ACCEPT_REASON
                    )
                    reason_widget.disabled = True
                    reason_widget.layout.display = "none"
                    status_widget.value = (
                        "<small style='color:#2f855a'><b>自动派生：</b>{}；"
                        "完成当前 subject 时一并人工确认。若源字段本身错误，"
                        "请不要完成并修正 source。</small>"
                    ).format(
                        "相关 requirement 已修改，正式总结为 revise"
                        if changed
                        else "当前 requirement proposal 与 source projection 一致"
                    )

                def _derived_exception_changed(
                    change: Mapping[str, Any],
                    reason_widget: Any = reason,
                    saved_reason: List[str] = exception_reason,
                    refresh: Any = _refresh_derived_check,
                ) -> None:
                    if change.get("old"):
                        current = str(reason_widget.value or "").strip()
                        if current and not _is_generated_reason(current):
                            saved_reason[0] = current
                    if change.get("new"):
                        reason_widget.value = saved_reason[0]
                    refresh()
                    self._structured_changed()

                exception.observe(_derived_exception_changed, names="value")
                self._conditional_reason_refreshers.append(_refresh_derived_check)
            elif check == "support_and_response_disposition":
                reason_status = self._configure_conditional_reason(
                    verdict,
                    reason,
                    auto_reasons={"accept": CHECK_ACCEPT_REASON},
                    manual_verdicts=("revise", "reject"),
                    manual_prompt=(
                        "说明 supported/unsupported 或可接受响应的源数据错误；"
                        "这会把当前 subject 标为阻塞，不能直接定稿。"
                    ),
                )
            else:
                reason_status = self._configure_conditional_reason(
                    verdict,
                    reason,
                    auto_reasons={
                        "accept": CHECK_ACCEPT_REASON,
                        "revise": CHECK_REVISE_REASON,
                    },
                    manual_verdicts=("reject",),
                    manual_prompt="指出 query library 或 oracle source 的具体错误。",
                    conditional_auto_verdicts={
                        "revise": lambda check=check: self._required_check_has_structural_change(
                            check
                        )
                    },
                    unavailable_prompt=(
                        "尚未检测到该总结对应的 requirement 结构变化；"
                        "请先修改相关要求卡，文字理由不能替代实际修改。"
                    ),
                )
            self._required_widgets[check] = {
                "verdict": verdict,
                "reason": reason,
                "reason_status": reason_status,
                "mapping_info": mapping_info,
                "exception": exception if check in DERIVED_REQUIRED_CHECKS else None,
            }
            if check not in DERIVED_REQUIRED_CHECKS:
                self._observe_dirty(verdict)
                self._observe_dirty(reason)
                row_controls = [
                    verdict,
                    w.VBox(
                        [reason_status, reason],
                        layout=w.Layout(width="58%"),
                    ),
                ]
            else:
                self._observe_dirty(reason)
                row_controls = [
                    w.VBox([exception, verdict], layout=w.Layout(width="42%")),
                    w.VBox(
                        [reason_status, reason],
                        layout=w.Layout(width="58%"),
                    ),
                ]
            row = w.VBox(
                [
                    info,
                    mapping_info,
                    w.HBox(row_controls),
                ],
                layout=w.Layout(
                    border="1px solid #e2e8f0", padding="10px", margin="0 0 8px 0"
                ),
            )
            required_row_by_check[check] = row

        decision_by_id = {item["atom_id"]: item for item in form["atom_decisions"]}
        agent_atom_by_id = {
            item["atom_id"]: item for item in agent.get("atom_decisions", [])
        }
        self._atom_widgets = {}
        atom_children_by_step = {
            "actors_events": [],
            "road_space": [],
            "rules_risk": [],
        }
        for atom in task["oracle_draft"]["atoms"]:
            decision = decision_by_id[atom["atom_id"]]
            verdict = w.Dropdown(
                options=ATOM_VERDICT_OPTIONS,
                value=decision.get("verdict", ""),
                description="人工判断",
                layout=w.Layout(width="48%"),
            )
            reason = w.Textarea(
                value=decision.get("reason", ""),
                description="判断依据",
                placeholder="简要说明当前 query 为什么要求或不要求这件事",
                layout=w.Layout(width="100%", height="82px"),
            )
            reason_status = self._configure_conditional_reason(
                verdict,
                reason,
                auto_reasons={"accept": ATOM_ACCEPT_REASON},
                manual_verdicts=("reject", "modify", "split", "merge"),
                manual_prompt=(
                    "说明为什么当前候选不应计分，或为什么需要修改、拆分、合并。"
                ),
            )
            replacement_atoms = _read_json_text(
                decision.get("replacement_atoms_json", "[]"),
                "atom {} replacements".format(atom["atom_id"]),
                list,
            )
            replacement_editor = AtomListEditor(
                w,
                replacement_atoms,
                on_change=self._structured_changed,
            )
            template_atom = {
                key: copy.deepcopy(atom[key])
                for key in (
                    "category",
                    "predicate",
                    "arguments",
                    "layer",
                    "polarity",
                    "notes",
                )
            }
            clone_source = w.Button(
                description="复制机器候选，开始编辑最终要求",
                icon="copy",
                button_style="info",
            )
            clone_source.on_click(
                lambda _button, editor=replacement_editor, source=template_atom: editor.add_atom(
                    source
                )
            )
            recommendation = agent_atom_by_id.get(atom["atom_id"], {})
            statement = _human_atom_statement(atom)
            layer_label = LAYER_LABELS.get(atom["layer"], _human_token(atom["layer"]))
            mapping_labels = [
                CHECK_SHORT_LABELS[check]
                for check in mapped_checks_by_atom.get(atom["atom_id"], [])
            ]
            summary = w.HTML(
                "<div style='padding:8px 10px;background:#f7fafc;border-left:4px solid #4a90e2'>"
                "<div style='color:#4a5568'>{}</div>"
                "<div style='font-size:16px;margin:5px 0'><b>{}</b></div>"
                "<div><b>机器草稿的计分严格程度：</b>{}</div>"
                "<div><b>候选依据：</b>{}</div>"
                "<details><summary>关联总结与机器提示（参考）</summary>"
                "<div><b>第4步自动关联：</b>{}</div>"
                "<div><small>机器提示：{} — {}</small></div>"
                "</details></div>".format(
                    html.escape(ATOM_CATEGORY_LABELS.get(atom["category"], atom["category"])),
                    html.escape(statement),
                    html.escape(layer_label),
                    html.escape(_human_atom_evidence(atom, task["query_text"])),
                    html.escape("、".join(mapping_labels) or "无"),
                    html.escape(
                        _human_agent_recommendation(
                            recommendation.get("recommendation", "unavailable")
                        )
                    ),
                    html.escape(str(recommendation.get("reason", ""))),
                )
            )
            permit_button = w.Button(
                description=(
                    "已是允许项"
                    if atom.get("layer") == "permitted"
                    else "已有修订草稿"
                    if replacement_atoms
                    else "允许但不强制"
                ),
                icon="check",
                disabled=atom.get("layer") == "permitted" or bool(replacement_atoms),
                tooltip=(
                    "当前机器草稿已经是 permitted"
                    if atom.get("layer") == "permitted"
                    else "先处理已有 target，避免快捷操作覆盖人工草稿"
                    if replacement_atoms
                    else "创建一个 layer=permitted 的修订目标"
                ),
            )
            technical_panel = w.VBox(
                [
                    w.HTML(
                        "<b>什么时候使用这里？</b> 只有“内容或计分严格程度需要修改”、"
                        "“拆分”或“合并”才需要。<br>"
                        "<b>source</b> 是上方机器候选；<b>target</b> 是你在下面编辑的最终要求。"
                        "普通的保留或不计分不需要创建 target。"
                    ),
                    clone_source,
                    replacement_editor.widget,
                    w.HTML(
                        "<details><summary>机器 source 的原始技术字段</summary>{}</details>".format(
                            self._safe_pre(atom)
                        )
                    ),
                ]
            )
            advanced = w.Accordion(children=[technical_panel])
            advanced.set_title(0, "高级修改：编辑 target、拆分或合并")
            advanced.selected_index = (
                0
                if decision.get("verdict") in ("modify", "split", "merge")
                or replacement_atoms
                else None
            )
            decision_details = w.Accordion(children=[w.VBox([verdict, advanced])])
            verdict.layout.width = "100%"
            decision_details.set_title(0, "其他判断：修改、拆分、合并或撤回选择")
            decision_details.selected_index = advanced.selected_index

            def _sync_advanced(
                change: Mapping[str, Any], panel: Any = advanced,
                details: Any = decision_details,
            ) -> None:
                if change.get("new") in ("modify", "split", "merge"):
                    details.selected_index = 0
                    panel.selected_index = 0

            def _make_permitted(
                _button: Any,
                editor: AtomListEditor = replacement_editor,
                source: Mapping[str, Any] = template_atom,
                verdict_widget: Any = verdict,
                reason_widget: Any = reason,
                panel: Any = advanced,
                details: Any = decision_details,
            ) -> None:
                try:
                    existing_targets = editor.value()
                except Exception:
                    details.selected_index = 0
                    panel.selected_index = 0
                    self._notify(
                        "高级修改草稿尚未填写完整；快捷操作不会覆盖它。请在高级修改区继续。",
                        error=True,
                    )
                    return
                if existing_targets:
                    details.selected_index = 0
                    panel.selected_index = 0
                    self._notify(
                        "已有 target 草稿；快捷操作不会覆盖它。请直接编辑其计分严格程度，"
                        "或明确删除草稿后再使用快捷操作。",
                        error=True,
                    )
                    return
                target = copy.deepcopy(source)
                target["layer"] = "permitted"
                editor.set_value([target], notify=False)
                verdict_widget.value = "modify"
                if not reason_widget.value.strip():
                    reason_widget.value = (
                        "当前 query 未明确要求该细节；允许生成，但缺失时不应扣分"
                    )
                _button.disabled = True
                _button.description = "已创建“允许但不强制”target"
                details.selected_index = 0
                panel.selected_index = 0
                self._structured_changed()

            verdict.observe(_sync_advanced, names="value")
            permit_button.on_click(_make_permitted)
            quick_buttons = {}
            for label, value in (("正确", "accept"), ("不应计分", "reject")):
                button = w.Button(description=label, tooltip=ATOM_VERDICT_LABELS[value])
                button.on_click(
                    lambda _button, value=value, control=verdict: setattr(control, "value", value)
                )
                quick_buttons[value] = button
            status = w.HTML()
            box = w.VBox(
                [
                    summary,
                    status,
                    w.HBox(list(quick_buttons.values()) + [permit_button]).add_class("review-actions"),
                    reason_status,
                    reason,
                    decision_details,
                ],
            ).add_class("review-card")
            self._atom_widgets[atom["atom_id"]] = {
                "verdict": verdict,
                "reason": reason,
                "reason_status": reason_status,
                "replacement_editor": replacement_editor,
                "clone_source": clone_source,
                "permit_button": permit_button,
                "advanced": advanced,
                "decision_details": decision_details,
                "quick_buttons": quick_buttons,
                "status": status,
            }
            for widget in (verdict, reason):
                self._observe_dirty(widget)
            if atom["category"] in ("actor", "event", "temporal"):
                step = "actors_events"
            elif atom["category"] in ("road", "spatial"):
                step = "road_space"
            else:
                step = "rules_risk"
            atom_children_by_step[step].append(box)
            self._atom_locations[atom["atom_id"]] = {
                "step": step,
                "index": len(atom_children_by_step[step]) - 1,
                "statement": statement,
            }
        self.atom_step_panels = {
            step: w.VBox(children or [w.HTML("本步没有候选要求；如发现遗漏，可在第3步补充。")]).add_class("review-cards")
            for step, children in atom_children_by_step.items()
        }

        atom_by_id = {
            atom["atom_id"]: atom for atom in task["oracle_draft"]["atoms"]
        }
        self.merge_source_atoms = w.SelectMultiple(
            options=[
                (
                    "{} · {} · {}".format(
                        atom["category"], atom["predicate"], atom["atom_id"]
                    ),
                    atom["atom_id"],
                )
                for atom in task["oracle_draft"]["atoms"]
            ],
            description="重复要求",
            rows=6,
            layout=w.Layout(width="96%"),
        )
        # Wizard staging is intentionally not autosaved. Only Apply copies one
        # shared target into the source-bound atom decision editors.
        self.merge_target_editor = AtomListEditor(w, [], on_change=None)
        self.merge_reason = w.Textarea(
            description="合并依据",
            placeholder="说明为什么这些机器候选实际是同一条评分要求",
            layout=w.Layout(width="96%", height="72px"),
        )
        self.merge_template_button = w.Button(
            description="复制第一条，编辑合并后的最终要求", icon="copy", disabled=True
        )
        self.merge_apply_ack = w.Checkbox(
            description="我确认覆盖所选要求当前尚未完成的判断",
            indent=False,
        )
        self.merge_apply_button = w.Button(
            description="应用合并", button_style="warning", disabled=True
        )

        def _refresh_merge_buttons(_change: Any = None) -> None:
            selected_count = len(self.merge_source_atoms.value)
            self.merge_template_button.disabled = selected_count == 0
            self.merge_apply_button.disabled = not (
                selected_count >= 2 and self.merge_apply_ack.value
            )

        def _load_merge_template(_button: Any) -> None:
            selected = tuple(self.merge_source_atoms.value)
            if not selected:
                return
            source = atom_by_id[selected[0]]
            semantic = {
                key: copy.deepcopy(source[key])
                for key in (
                    "category",
                    "predicate",
                    "arguments",
                    "layer",
                    "polarity",
                    "notes",
                )
            }
            self.merge_target_editor.set_value([semantic], notify=False)

        def _apply_cross_source_merge(_button: Any) -> None:
            try:
                selected = tuple(self.merge_source_atoms.value)
                reason = self.merge_reason.value.strip()
                targets = self.merge_target_editor.value()
                if len(selected) < 2:
                    raise ValidationError("跨源 merge 至少选择两个 draft atoms")
                if len(targets) != 1:
                    raise ValidationError("跨源 merge 必须定义恰好一个共享目标 atom")
                if not reason:
                    raise ValidationError("跨源 merge 必须填写人工理由")
                target_projection = {
                    key: copy.deepcopy(targets[0].get(key))
                    for key in (
                        "category",
                        "predicate",
                        "arguments",
                        "layer",
                        "polarity",
                    )
                }
                for atom_id in selected:
                    source_projection = {
                        key: copy.deepcopy(atom_by_id[atom_id].get(key))
                        for key in target_projection
                    }
                    if source_projection == target_projection:
                        raise ValidationError(
                            "共享目标必须与每个被合并的源 atom 存在语义变化"
                        )
                for atom_id in selected:
                    widgets = self._atom_widgets[atom_id]
                    widgets["verdict"].value = "merge"
                    widgets["reason"].value = reason
                    widgets["replacement_editor"].set_value(
                        copy.deepcopy(targets), notify=False
                    )
                self.merge_apply_ack.value = False
                self._structured_changed()
                self._notify(
                    "已把 {} 条机器候选映射为一条最终要求；请继续完成最终一致性确认。".format(
                        len(selected)
                    )
                )
            except Exception as exc:
                self._notify(str(exc), error=True)

        self.merge_source_atoms.observe(_refresh_merge_buttons, names="value")
        self.merge_apply_ack.observe(_refresh_merge_buttons, names="value")
        self.merge_template_button.on_click(_load_merge_template)
        self.merge_apply_button.on_click(_apply_cross_source_merge)
        merge_panel = w.VBox(
            [
                w.HTML(
                    "<b>合并重复的机器候选</b><br>仅当两条或更多候选实际描述同一件事时使用。"
                    "先选择重复候选，再定义一条合并后的最终要求。点击“应用合并”前，"
                    "这里的临时编辑不会写入 checkpoint。"
                ),
                self.merge_source_atoms,
                self.merge_template_button,
                self.merge_target_editor.widget,
                self.merge_reason,
                w.HBox([self.merge_apply_ack, self.merge_apply_button]),
            ]
        )

        cpd = form["cpd_decision"]
        self.cpd_verdict = w.Dropdown(
            options=CPD_VERDICT_OPTIONS,
            value=cpd.get("verdict", ""),
            description="人工结论",
            layout=w.Layout(width="45%"),
        )
        self.cpd_reason = w.Textarea(
            value=cpd.get("reason", ""),
            description="判断依据",
            placeholder="说明是否存在合理变化，以及为什么能或不能跨平台判断",
            layout=w.Layout(width="100%", height="88px"),
        )
        self.cpd_reason_code = w.Dropdown(
            options=CPD_REVISION_REASON_OPTIONS,
            value=(
                _cpd_revision_reason_code(cpd.get("reason", ""))
                if cpd.get("verdict") == "revise"
                else ""
            ),
            description="修改原因",
            layout=w.Layout(width="100%"),
        )
        self.cpd_reason_status = w.HTML()
        cpd_reason_drafts = {self.cpd_verdict.value: self.cpd_reason.value}
        previous_cpd_verdict = [self.cpd_verdict.value]

        def _render_cpd_reason(selected: str, *, initial: bool) -> None:
            if selected == "accept":
                if not initial:
                    self.cpd_reason.value = _normalized_auto_reason(
                        cpd_reason_drafts.get("accept", ""), CPD_ACCEPT_REASON
                    )
                self.cpd_reason_code.layout.display = "none"
                self.cpd_reason.layout.display = "none"
                current = str(self.cpd_reason.value or "").strip()
                message = (
                    "已有人工自定义依据，将原样保留。"
                    if current and not _is_generated_reason(current)
                    else "保存时会写入标准人工确认，不需要手写。"
                )
                self.cpd_reason_status.value = (
                    "<small style='color:#2f855a'><b>自动审计依据：</b>{}</small>".format(
                        html.escape(message)
                    )
                )
                return
            if selected == "revise":
                if not initial:
                    candidate = cpd_reason_drafts.get("revise", "")
                    if not candidate:
                        orphan = cpd_reason_drafts.get("", "")
                        if orphan and not _is_generated_reason(orphan):
                            candidate = orphan
                    self.cpd_reason.value = (
                        "" if _is_generated_reason(candidate) else candidate
                    )
                    self.cpd_reason_code.value = _cpd_revision_reason_code(
                        self.cpd_reason.value
                    )
                self.cpd_reason_code.layout.display = ""
                code = self.cpd_reason_code.value
                self.cpd_reason.layout.display = "" if code == "other" else "none"
                self.cpd_reason_status.value = (
                    "<small style='color:#9c2f00'><b>条件性依据：</b>"
                    "先选择主要修改原因；只有“其他”需要手写说明。</small>"
                )
                return
            if selected == "reject":
                if not initial:
                    candidate = cpd_reason_drafts.get("reject", "")
                    if not candidate:
                        orphan = cpd_reason_drafts.get("", "")
                        if orphan and not _is_generated_reason(orphan):
                            candidate = orphan
                    self.cpd_reason.value = (
                        "" if _is_generated_reason(candidate) else candidate
                    )
                self.cpd_reason_code.layout.display = "none"
                self.cpd_reason.layout.display = ""
                self.cpd_reason_status.value = (
                    "<small style='color:#9c2f00'><b>需要人工说明：</b>"
                    "指出 CPD source 的具体错误以及为什么当前 subject 不能完成。</small>"
                )
                return
            orphan = str(self.cpd_reason.value or "").strip()
            self.cpd_reason_code.layout.display = "none"
            self.cpd_reason.layout.display = "" if orphan else "none"
            self.cpd_reason_status.value = (
                "<small style='color:#b7791f'><b>未归类 CPD 理由草稿：</b>"
                "选择修订或拒绝后会自动带入。</small>"
                if orphan
                else "<small style='color:#4a5568'>先选择 CPD 结论；普通接受不需要手写依据。</small>"
            )

        def _cpd_verdict_changed(change: Mapping[str, Any]) -> None:
            old = change.get("old", previous_cpd_verdict[0])
            cpd_reason_drafts[old] = self.cpd_reason.value
            previous_cpd_verdict[0] = change.get("new", "")
            _render_cpd_reason(previous_cpd_verdict[0], initial=False)

        def _cpd_reason_code_changed(change: Mapping[str, Any]) -> None:
            code = change.get("new", "")
            if code in CPD_REVISION_REASON_TEXT:
                self.cpd_reason.value = CPD_REVISION_REASON_TEXT[code]
                self.cpd_reason.layout.display = "none"
            elif code == "other":
                if (
                    self.cpd_reason.value in CPD_REVISION_REASON_TEXT.values()
                    or _is_generated_reason(self.cpd_reason.value)
                ):
                    self.cpd_reason.value = ""
                self.cpd_reason.layout.display = ""
            else:
                if self.cpd_reason.value in CPD_REVISION_REASON_TEXT.values():
                    self.cpd_reason.value = ""
                self.cpd_reason.layout.display = "none"

        _render_cpd_reason(self.cpd_verdict.value, initial=True)
        self.cpd_verdict.observe(_cpd_verdict_changed, names="value")
        self.cpd_reason_code.observe(_cpd_reason_code_changed, names="value")
        saved_cpd_replacement = _read_json_text(
            cpd.get("replacement_policy_json", "{}"),
            "CPD replacement policy",
            dict,
        )
        self.cpd_policy_editor = CPDPolicyEditor(
            w,
            saved_cpd_replacement or task["oracle_draft"]["cpd_policy"],
            on_change=self._structured_changed,
        )
        saved_added_atoms = _read_json_text(
            form.get("added_atoms_json", "[]"), "added atoms", list
        )
        self.added_atoms_editor = AtomListEditor(
            w,
            saved_added_atoms,
            on_change=self._structured_changed,
        )
        self.notes = w.Textarea(
            value=form.get("notes", ""),
            description="审核备注",
            placeholder="可选；如果新增 requirement，则必须说明机器草稿遗漏了什么",
            layout=w.Layout(width="96%", height="100px"),
        )
        for widget in (
            self.cpd_verdict,
            self.cpd_reason,
            self.notes,
        ):
            self._observe_dirty(widget)
        agent_cpd = agent.get("cpd_decision", {})
        draft_cpd = task["oracle_draft"]["cpd_policy"]
        dimension_names = [
            _human_token(item.get("name", item.get("dimension", "未命名维度")))
            for item in draft_cpd.get("dimensions", [])
        ]
        cpd_plain_summary = w.HTML(
            "<div style='padding:10px;background:#f7fafc;border-left:4px solid #805ad5'>"
            "<b>机器草稿当前结论</b><br>"
            "存在值得评价的合理变化：{}<br>"
            "当前判定可进入 CPD_common：{}<br>"
            "CARLA 与 MetaDrive 都能判断：{}<br>"
            "变化维度：{}"
            "{}</div>".format(
                _human_token(draft_cpd.get("candidate")),
                _human_token(draft_cpd.get("eligible")),
                _human_token(draft_cpd.get("cross_platform_judgeable")),
                html.escape("、".join(dimension_names) or "无"),
                (
                    "<br><b style='color:#b7791f'>注意：跨平台可判断性仍是“待确认”，"
                    "请不要仅凭机器草稿直接完成。</b>"
                    if (
                        draft_cpd.get("cross_platform_judgeable") is None
                        and (
                            draft_cpd.get("candidate")
                            or draft_cpd.get("eligible")
                        )
                    )
                    else ""
                ),
            )
        )
        cpd_questions = w.HTML(
            "<b>只回答三个问题：</b>"
            "<ol><li>同一 query 是否允许多个同样正确、但可区分的场景实现？</li>"
            "<li>变化维度是否不依赖某个平台的专有能力？</li>"
            "<li>目标参与者和允许值是否明确到可以自动或人工复核？</li></ol>"
            "三项都满足才应设为 eligible。精确表述通常不奖励额外多样性。"
        )
        cpd_editor_panel = w.VBox(
            [
                w.HTML(
                    "只有选择“我需要修改”时才编辑。candidate 表示存在合理变化；"
                    "eligible 表示该变化符合 CPD_common 的正式条件。"
                ),
                self.cpd_policy_editor.widget,
                w.HTML(
                    "<details><summary>机器 CPD 原始字段</summary>{}</details>".format(
                        self._safe_pre(draft_cpd)
                    )
                ),
            ]
        )
        self.cpd_advanced = w.Accordion(children=[cpd_editor_panel])
        self.cpd_advanced.set_title(0, "高级修改：CPD policy 与维度")
        self.cpd_advanced.selected_index = 0 if cpd.get("verdict") == "revise" else None

        def _sync_cpd_advanced(change: Mapping[str, Any]) -> None:
            if change.get("new") == "revise":
                self.cpd_advanced.selected_index = 0

        self.cpd_verdict.observe(_sync_cpd_advanced, names="value")

        agent_cpd_panel = w.HTML(
            "<b>机器 CPD 提示（非 gold）：</b>{}<br>{}<br>"
            "<small>内部 finding IDs: {}</small>".format(
                html.escape(
                    _human_agent_recommendation(
                        agent_cpd.get("recommendation", "unavailable")
                    )
                ),
                html.escape(str(agent_cpd.get("reason", ""))),
                html.escape(", ".join(agent_cpd.get("finding_ids", [])) or "none"),
            )
        )
        agent_cpd_accordion = w.Accordion(children=[agent_cpd_panel])
        agent_cpd_accordion.set_title(0, "机器 CPD 提示（可选参考）")
        agent_cpd_accordion.selected_index = None

        added_panel = w.VBox(
            [
                w.HTML(
                    "<b>仅用于机器草稿完全漏掉一条要求。</b> 普通修改请回到对应要求卡，"
                    "不要在这里重复新增。"
                ),
                self.added_atoms_editor.widget,
            ]
        )
        self.advanced_operations = w.Accordion(
            children=[w.VBox([merge_panel, added_panel])]
        )
        self.advanced_operations.set_title(0, "高级操作：合并重复要求或补充漏项")
        self.advanced_operations.selected_index = None

        step_intro = (
            "<div style='padding:8px 10px;background:#fffaf0'>对照当前请求，逐张判断下方要求卡。"
            "常用判断可直接点击；“已选”仍需在本条完成时校验。"
            "不要因为元数据里存在某细节，就自动把它设为必须满足。</div>"
        )
        actor_event_panel = w.VBox(
            [
                w.HTML(
                    step_intro
                    + "<p><b>本步重点：</b>参与者、外部事件及其先后关系。"
                    "公交在运行中的制动、等待、让行等反应不能成为场景生成要求。</p>"
                ),
                self.atom_step_panels["actors_events"],
            ]
        )
        road_space_panel = w.VBox(
            [
                w.HTML(
                    step_intro
                    + "<p><b>本步重点：</b>道路、车道设施、相对位置和当前文本中的数值约束。"
                    "partial/vague 没说出的细节通常只能是允许项，而不是必需项。</p>"
                ),
                self.atom_step_panels["road_space"],
            ]
        )
        rules_risk_panel = w.VBox(
            [
                w.HTML(
                    step_intro
                    + "<p><b>本步重点：</b>其他参与者/场景规则与风险标签。"
                    "确认规则作用对象不是 ego bus；风险分析标签不一定是生成要求。</p>"
                ),
                self.atom_step_panels["rules_risk"],
                self.advanced_operations,
            ]
        )
        final_checks = [
            required_row_by_check[check]
            for check in REQUIRED_REVIEW_CHECKS
            if check != "cpd_common_eligibility"
        ]
        mapping_examples = []
        for atom in task["oracle_draft"].get("atoms", []):
            is_lane_count = (
                atom.get("predicate") == "lane_configuration"
                and atom.get("arguments", {}).get("feature")
                == "motor_lanes_same_direction"
            )
            is_risk = atom.get("predicate") == "risk_level"
            if not (is_lane_count or is_risk):
                continue
            labels = [
                CHECK_SHORT_LABELS[check]
                for check in mapped_checks_by_atom.get(atom["atom_id"], [])
            ]
            mapping_examples.append(
                "<li><b>{}</b> → {}</li>".format(
                    html.escape(_human_atom_statement(atom)),
                    html.escape("、".join(labels)),
                )
            )
        mapping_guide = w.HTML(
            "<div style='padding:9px 11px;background:#fffaf0;border-left:4px solid #d69e2e'>"
            "<b>为什么同一张卡会关联多个总结？</b> 第4步从不同视角复核同一份最终评分要求，"
            "不是让你重复审核，也不是让你手工给评分指标分配字段。"
            "一张卡同时影响内容、领域结构和计分严格程度是正常的；这些关联已由正式"
            "校验规则自动计算。{}"
            "</div>".format(
                "<ul>{}</ul>".format("".join(mapping_examples))
                if mapping_examples
                else ""
            )
        )
        final_check_panel = w.VBox(
            [
                w.HTML(
                    "<div style='padding:8px 10px;background:#edf2f7'>"
                    "<b>这是逐条要求审核后的总复核，不是第二套独立答案。</b> "
                    "四项 requirement 总结由正式结构差异自动显示为 accept 或 revise；"
                    "你只需独立确认 support/response。完成本 subject 表示你同时确认"
                    "这些可见的派生结果；源 library/oracle 本身错误时不要完成。"
                    "</div>"
                ),
                mapping_guide,
            ]
            + final_checks
        )
        cpd_panel = w.VBox(
            [
                cpd_questions,
                cpd_plain_summary,
                agent_cpd_accordion,
                w.HBox(
                    [
                        self.cpd_verdict,
                        w.VBox(
                            [
                                self.cpd_reason_status,
                                self.cpd_reason_code,
                                self.cpd_reason,
                            ],
                            layout=w.Layout(width="55%"),
                        ),
                    ]
                ),
                self.cpd_advanced,
                w.HTML(
                    "<small>这一次人工结论会自动同步到正式 CPD 总结项；"
                    "不需要重复选择第二个 verdict。</small>"
                ),
                required_row_by_check["cpd_common_eligibility"],
                self.notes,
            ]
        )
        self.cpd_required_summary_row = required_row_by_check[
            "cpd_common_eligibility"
        ]
        self.cpd_required_summary_row.layout.display = "none"

        self.review_tabs = w.Tab(
            children=[
                actor_event_panel,
                road_space_panel,
                rules_risk_panel,
                final_check_panel,
                cpd_panel,
            ]
        ).add_class("review-tabs")
        for panel in self.review_tabs.children:
            panel.add_class("review-step")
        agent_accordion = w.Accordion(children=[agent_panel])
        agent_accordion.set_title(0, "机器语义提示（可选参考，不是答案）")
        agent_accordion.selected_index = None
        self.content.add_class("review-workspace")
        self.content.children = [
            query_panel,
            w.VBox([self.review_tabs, agent_accordion]).add_class("review-main"),
        ]
        # Required-check rows are constructed before the atom and CPD editors.
        # Refresh once the complete proposal is available so a restored
        # checkpoint immediately reflects its real before/after projection.
        for refresh in tuple(self._conditional_reason_refreshers):
            refresh()
        self._rendering = False
        self._refresh_review_progress()
        self._set_dirty(False)
        self.mechanical_ack.value = False
        self.reset_ack.value = False
        self._refresh_header()

    def _collect_form(self) -> Dict[str, Any]:
        required_decisions = {}
        for check, widgets in self._required_widgets.items():
            verdict = widgets["verdict"].value
            raw_reason = widgets["reason"].value
            if verdict == "accept":
                reason = _reason_for_verdict(
                    verdict, raw_reason, {"accept": CHECK_ACCEPT_REASON}
                )
            elif verdict == "revise" and (
                check != "support_and_response_disposition"
                and self._required_check_has_structural_change(check)
            ):
                reason = _normalized_auto_reason(raw_reason, CHECK_REVISE_REASON)
            else:
                reason = "" if _is_generated_reason(raw_reason) else raw_reason
            required_decisions[check] = {
                "verdict": verdict,
                "reason": reason,
            }
        # The production schema stores the CPD semantic decision twice: once
        # as the policy decision and once as the corresponding high-level
        # closure check.  The UI asks the human once and emits both matching
        # fields, avoiding a meaningless duplicate choice.
        required_decisions["cpd_common_eligibility"] = {
            "verdict": self.cpd_verdict.value,
            "reason": _reason_for_verdict(
                self.cpd_verdict.value,
                self.cpd_reason.value,
                {"accept": CPD_ACCEPT_REASON},
            ),
        }
        for check in DERIVED_REQUIRED_CHECKS:
            existing = required_decisions[check]
            if (
                self._required_widgets[check]["exception"].value
                and existing["verdict"] == "reject"
            ):
                continue
            changed = self._required_check_has_structural_change(check)
            required_decisions[check] = {
                "verdict": "revise" if changed else "accept",
                "reason": CHECK_REVISE_REASON if changed else CHECK_ACCEPT_REASON,
            }
        return {
            "required_check_decisions": required_decisions,
            "atom_decisions": [
                {
                    "atom_id": atom_id,
                    "verdict": widgets["verdict"].value,
                    "reason": _reason_for_verdict(
                        widgets["verdict"].value,
                        widgets["reason"].value,
                        {"accept": ATOM_ACCEPT_REASON},
                    ),
                    "replacement_atoms_json": _pretty(
                        widgets["replacement_editor"].value()
                    ),
                }
                for atom_id, widgets in self._atom_widgets.items()
            ],
            "cpd_decision": {
                "verdict": self.cpd_verdict.value,
                "reason": _reason_for_verdict(
                    self.cpd_verdict.value,
                    self.cpd_reason.value,
                    {"accept": CPD_ACCEPT_REASON},
                ),
                "replacement_policy_json": _pretty(
                    self.cpd_policy_editor.value()
                ),
            },
            "added_atoms_json": _pretty(self.added_atoms_editor.value()),
            "notes": self.notes.value,
        }

    def _required_check_change_sources(
        self, check: str, form: Mapping[str, Any]
    ) -> List[str]:
        """Identify requirement edits that independently affect one check."""

        task = self.session.task(self.current_query_id)
        draft = task["oracle_draft"]
        draft_atoms = {atom["atom_id"]: atom for atom in draft["atoms"]}
        sources = []
        for candidate in form["atom_decisions"]:
            if candidate["verdict"] in ("", "accept"):
                continue
            probe_decisions = []
            for atom_id in draft_atoms:
                if atom_id == candidate["atom_id"]:
                    item = copy.deepcopy(candidate)
                    item["reason"] = item["reason"] or "preview requirement change"
                else:
                    item = {
                        "atom_id": atom_id,
                        "verdict": "accept",
                        "reason": ATOM_ACCEPT_REASON,
                        "replacement_atoms_json": "[]",
                    }
                probe_decisions.append(item)
            probe = {
                "required_check_decisions": {
                    name: {"verdict": "accept", "reason": CHECK_ACCEPT_REASON}
                    for name in REQUIRED_REVIEW_CHECKS
                },
                "atom_decisions": probe_decisions,
                "cpd_decision": {
                    "verdict": "accept",
                    "reason": CPD_ACCEPT_REASON,
                    "replacement_policy_json": "{}",
                },
                "added_atoms_json": "[]",
                "notes": "",
            }
            try:
                proposed = response_from_form(task, probe)["proposed_oracle"]
            except (KeyError, TypeError, ValueError, ValidationError):
                continue
            if query_check_projection(check, draft) != query_check_projection(
                check, proposed
            ):
                sources.append(_human_atom_statement(draft_atoms[candidate["atom_id"]]))

        added_atoms = json.loads(form.get("added_atoms_json", "[]"))
        for added_atom in added_atoms:
            probe = {
                "required_check_decisions": {
                    name: {"verdict": "accept", "reason": CHECK_ACCEPT_REASON}
                    for name in REQUIRED_REVIEW_CHECKS
                },
                "atom_decisions": [
                    {
                        "atom_id": atom_id,
                        "verdict": "accept",
                        "reason": ATOM_ACCEPT_REASON,
                        "replacement_atoms_json": "[]",
                    }
                    for atom_id in draft_atoms
                ],
                "cpd_decision": {
                    "verdict": "accept",
                    "reason": CPD_ACCEPT_REASON,
                    "replacement_policy_json": "{}",
                },
                "added_atoms_json": _pretty([added_atom]),
                "notes": form.get("notes", ""),
            }
            try:
                proposed = response_from_form(task, probe)["proposed_oracle"]
            except (KeyError, TypeError, ValueError, ValidationError):
                proposed = None
            if proposed is not None and query_check_projection(
                check, draft
            ) != query_check_projection(check, proposed):
                try:
                    normalized_added = _normalize_human_atom(
                        added_atom, self.current_query_id
                    )
                except (KeyError, TypeError, ValueError, ValidationError):
                    continue
                sources.append(
                    "人工新增：{}".format(_human_atom_statement(normalized_added))
                )
        return sources

    def _required_check_completion_issues(
        self, form: Mapping[str, Any]
    ) -> List[Dict[str, Any]]:
        """Describe high-level summary states that cannot pass closure."""

        issues = []
        for check in REQUIRED_REVIEW_CHECKS:
            if check == "cpd_common_eligibility":
                continue
            if (
                check in DERIVED_REQUIRED_CHECKS
                and form["required_check_decisions"][check]["verdict"] != "reject"
            ):
                continue
            decision = form["required_check_decisions"][check]
            verdict = decision["verdict"]
            changed = self._required_check_has_structural_change(check)
            problems = []
            codes = []
            recommended_verdict = None
            if not verdict:
                problems.append("尚未确认这项最终总结")
                codes.append("missing_verdict")
            elif verdict == "accept" and changed:
                problems.append(
                    "相关 requirements 已发生变化，但当前仍选择“当前总结准确”"
                )
                codes.append("accepted_drift")
                recommended_verdict = "revise"
            elif verdict == "revise" and not changed:
                problems.append(
                    "当前选择“我已修改相关要求”，但对应范围没有实际结构变化"
                )
                codes.append("unrealized_revision")
                if check != "support_and_response_disposition":
                    recommended_verdict = "accept"
            elif verdict == "reject":
                problems.append(
                    "当前声明源数据有误；此状态只能保存为阻塞草稿，不能完成 subject"
                )
                codes.append("source_blocker")
            if (
                verdict
                and not decision["reason"].strip()
                and "unrealized_revision" not in codes
            ):
                problems.append("当前总结缺少必须的判断依据")
                codes.append("missing_reason")
            if not problems:
                continue
            issues.append(
                {
                    "check": check,
                    "label": CHECK_LABELS[check],
                    "verdict": REQUIRED_VERDICT_LABELS.get(verdict, "尚未确认"),
                    "problems": problems,
                    "codes": codes,
                    "recommended_verdict": recommended_verdict,
                    "change_sources": (
                        self._required_check_change_sources(check, form)
                        if changed
                        else []
                    ),
                }
            )
        return issues

    def _cpd_completion_issues(
        self, form: Mapping[str, Any]
    ) -> List[Dict[str, Any]]:
        """Describe the visible CPD decision without exposing its hidden duplicate."""

        decision = form["cpd_decision"]
        verdict = decision["verdict"]
        changed = self._cpd_policy_has_structural_change(form)
        problems = []
        codes = []
        recommended_verdict = None
        if not verdict:
            problems.append("尚未判断当前 CPD 结论是否正确")
            codes.append("missing_verdict")
        elif verdict == "accept" and changed:
            problems.append(
                "CPD policy 已发生变化，但当前仍选择“当前 CPD 结论正确”"
            )
            codes.append("accepted_drift")
            recommended_verdict = "revise"
        elif verdict == "revise" and not changed:
            problems.append(
                "当前选择“我需要修改 CPD 结论或维度”，但高级修改中的 "
                "CPD policy 与机器草稿没有实际结构变化；只选择修改原因不能代替修改 policy"
            )
            codes.append("unrealized_revision")
            recommended_verdict = "accept"
        elif verdict == "reject":
            problems.append(
                "当前声明 CPD 源数据有误；此状态只能保存为阻塞草稿，不能完成 subject"
            )
            codes.append("source_blocker")
        if verdict and not decision["reason"].strip():
            problems.append("当前 CPD 结论缺少必须的判断依据")
            codes.append("missing_reason")
        if not problems:
            return []
        return [
            {
                "verdict": CPD_VERDICT_LABELS.get(verdict, "尚未判断"),
                "problems": problems,
                "codes": codes,
                "recommended_verdict": recommended_verdict,
            }
        ]

    def _atom_completion_issues(
        self, form: Mapping[str, Any]
    ) -> List[Dict[str, Any]]:
        """Describe every incomplete card using the formal atom invariants."""

        decisions = {item["atom_id"]: item for item in form["atom_decisions"]}
        task_atoms = self.session.task(self.current_query_id)["oracle_draft"]["atoms"]
        atoms_by_id = {atom["atom_id"]: atom for atom in task_atoms}
        problems_by_id = {atom_id: [] for atom_id in atoms_by_id}
        codes_by_id = {atom_id: set() for atom_id in atoms_by_id}
        normalized_targets: Dict[str, List[Dict[str, Any]]] = {}

        def add(atom_id: str, code: str, message: str) -> None:
            if message not in problems_by_id[atom_id]:
                problems_by_id[atom_id].append(message)
            codes_by_id[atom_id].add(code)

        for atom in task_atoms:
            atom_id = atom["atom_id"]
            decision = decisions[atom_id]
            verdict = decision["verdict"]
            replacements = json.loads(decision["replacement_atoms_json"])
            if not verdict:
                add(atom_id, "missing_verdict", "尚未选择人工判断")
            elif not decision["reason"].strip():
                add(atom_id, "missing_reason", "当前判断缺少必须的人工依据")
            if not verdict and replacements:
                add(
                    atom_id,
                    "target_invariant",
                    "高级修改区已有 target 草稿，但尚未选择修改/拆分/合并判断",
                )
            if verdict in ("accept", "reject") and replacements:
                add(
                    atom_id,
                    "target_invariant",
                    "当前判断是“{}”，但高级修改区仍保留 {} 个 target 草稿；"
                    "请改为修改/拆分/合并，或删除这些 target".format(
                        ATOM_VERDICT_LABELS[verdict], len(replacements)
                    ),
                )
            expected_count_message = None
            if verdict == "modify" and len(replacements) != 1:
                expected_count_message = "修改必须恰好提供 1 个最终 target；当前为 {} 个".format(
                    len(replacements)
                )
            elif verdict == "split" and len(replacements) < 2:
                expected_count_message = "拆分必须至少提供 2 个最终 target；当前为 {} 个".format(
                    len(replacements)
                )
            elif verdict == "merge" and len(replacements) != 1:
                expected_count_message = "合并必须恰好提供 1 个共享 target；当前为 {} 个".format(
                    len(replacements)
                )
            if expected_count_message:
                add(atom_id, "target_invariant", expected_count_message)

            if verdict not in ("modify", "split", "merge") or not replacements:
                normalized_targets[atom_id] = []
                continue
            try:
                targets = [
                    _normalize_human_atom(raw, self.current_query_id)
                    for raw in replacements
                ]
            except (KeyError, TypeError, ValueError, ValidationError) as exc:
                add(
                    atom_id,
                    "target_invariant",
                    "target 字段尚未完成：{}".format(exc),
                )
                normalized_targets[atom_id] = []
                continue
            normalized_targets[atom_id] = targets
            target_ids = [target["atom_id"] for target in targets]
            if len(set(target_ids)) != len(target_ids):
                add(atom_id, "target_invariant", "多个 target 实际重复，请删除重复项")
            if not any(
                atom_semantic_projection(target) != atom_semantic_projection(atom)
                for target in targets
            ):
                add(
                    atom_id,
                    "target_invariant",
                    "当前 target 与机器 source 没有语义变化；请实际修改内容，或改为接受",
                )

        target_uses: Dict[str, List[str]] = {}
        for atom_id, decision in decisions.items():
            verdict = decision["verdict"]
            if verdict == "accept":
                target_ids = [atom_id]
            elif verdict in ("modify", "split", "merge"):
                target_ids = [
                    target["atom_id"] for target in normalized_targets.get(atom_id, [])
                ]
            else:
                target_ids = []
            for target_id in target_ids:
                target_uses.setdefault(target_id, []).append(atom_id)

        rejected_source_ids = {
            atom_id
            for atom_id, decision in decisions.items()
            if decision["verdict"] == "reject"
        }
        for source_id, targets in normalized_targets.items():
            for target in targets:
                rejected_id = target["atom_id"]
                if rejected_id == source_id or rejected_id not in rejected_source_ids:
                    continue
                add(
                    source_id,
                    "target_invariant",
                    "这个 target 与另一张已排除的 source 卡“{}”是同一个最终要求；"
                    "请修改当前 target，或重新审查那张 source 卡的排除判断".format(
                        _human_atom_statement(atoms_by_id[rejected_id])
                    ),
                )

        for atom_id, decision in decisions.items():
            if decision["verdict"] != "merge":
                continue
            targets = normalized_targets.get(atom_id, [])
            if len(targets) != 1:
                continue
            target_id = targets[0]["atom_id"]
            merge_members = [
                source_id
                for source_id in target_uses.get(target_id, [])
                if decisions[source_id]["verdict"] == "merge"
            ]
            if len(merge_members) < 2:
                add(
                    atom_id,
                    "target_invariant",
                    "合并至少需要 2 张 source 卡共同指向同一个 target；当前只有 1 张",
                )

        for target_id, source_ids in target_uses.items():
            if len(source_ids) <= 1:
                continue
            valid_merge = len(source_ids) >= 2 and all(
                decisions[source_id]["verdict"] == "merge"
                for source_id in source_ids
            )
            if valid_merge:
                continue
            for atom_id in source_ids:
                add(
                    atom_id,
                    "target_invariant",
                    "这个最终 target 也被其他要求卡使用；只有至少两张卡共同选择合并时才允许复用",
                )

        issues = []
        for atom in task_atoms:
            atom_id = atom["atom_id"]
            decision = decisions[atom_id]
            verdict = decision["verdict"]
            problems = problems_by_id[atom_id]
            if not problems:
                continue
            location = self._atom_locations[atom_id]
            issues.append(
                {
                    "atom_id": atom_id,
                    "step": location["step"],
                    "index": location["index"],
                    "statement": location["statement"],
                    "verdict": ATOM_VERDICT_LABELS.get(verdict, "尚未判断"),
                    "problems": problems,
                    "codes": sorted(codes_by_id[atom_id]),
                    "open_advanced": "target_invariant" in codes_by_id[atom_id],
                }
            )
        return issues

    def _added_atom_completion_issues(
        self, form: Mapping[str, Any]
    ) -> List[Dict[str, Any]]:
        """Reject added requirements which duplicate a source or derived target."""

        task_atoms = self.session.task(self.current_query_id)["oracle_draft"]["atoms"]
        draft_by_id = {atom["atom_id"]: atom for atom in task_atoms}
        decisions = {item["atom_id"]: item for item in form["atom_decisions"]}
        target_sources: Dict[str, List[str]] = {}
        for decision in form["atom_decisions"]:
            if decision.get("verdict") not in ("modify", "split", "merge"):
                continue
            try:
                replacements = _read_json_text(
                    str(decision.get("replacement_atoms_json", "[]")),
                    "replacement atoms",
                    list,
                )
                normalized = [
                    _normalize_human_atom(raw, self.current_query_id)
                    for raw in replacements
                ]
            except (KeyError, TypeError, ValueError, ValidationError):
                continue
            for target in normalized:
                target_sources.setdefault(target["atom_id"], []).append(
                    decision["atom_id"]
                )

        try:
            added_raw = _read_json_text(
                str(form.get("added_atoms_json", "[]")),
                "added atoms",
                list,
            )
        except (TypeError, ValueError, ValidationError) as exc:
            return [
                {
                    "index": 0,
                    "statement": "人工新增要求",
                    "problems": ["新增区内容无法读取：{}".format(exc)],
                    "codes": ["invalid_added_atom"],
                }
            ]

        normalized_added: List[Optional[Dict[str, Any]]] = []
        issues_by_index: Dict[int, Dict[str, Any]] = {}

        def add_issue(index: int, code: str, message: str, statement: str) -> None:
            issue = issues_by_index.setdefault(
                index,
                {
                    "index": index,
                    "statement": statement,
                    "problems": [],
                    "codes": [],
                },
            )
            if message not in issue["problems"]:
                issue["problems"].append(message)
            if code not in issue["codes"]:
                issue["codes"].append(code)

        for index, raw in enumerate(added_raw):
            try:
                atom = _normalize_human_atom(raw, self.current_query_id)
            except (KeyError, TypeError, ValueError, ValidationError) as exc:
                normalized_added.append(None)
                add_issue(
                    index,
                    "invalid_added_atom",
                    "字段尚未完成：{}".format(exc),
                    "人工新增第 {} 条".format(index + 1),
                )
                continue
            normalized_added.append(atom)
            statement = _human_atom_statement(atom)
            source = draft_by_id.get(atom["atom_id"])
            if source is not None:
                source_decision = decisions.get(atom["atom_id"], {})
                add_issue(
                    index,
                    "duplicates_source",
                    "与现有 source 卡“{}”是同一 requirement 身份（原卡判断：{}）。"
                    "请删除这条人工新增项，并回到原卡完成接受、排除或修改".format(
                        _human_atom_statement(source),
                        ATOM_VERDICT_LABELS.get(
                            source_decision.get("verdict", ""), "尚未判断"
                        ),
                    ),
                    statement,
                )
            if atom["atom_id"] in target_sources:
                source_statements = [
                    _human_atom_statement(draft_by_id[source_id])
                    for source_id in target_sources[atom["atom_id"]]
                ]
                add_issue(
                    index,
                    "duplicates_target",
                    "与高级修改 target 重复；该 target 已由 source 卡“{}”产生。"
                    "请删除人工新增项".format("；".join(source_statements)),
                    statement,
                )

        indices_by_atom_id: Dict[str, List[int]] = {}
        for index, atom in enumerate(normalized_added):
            if atom is not None:
                indices_by_atom_id.setdefault(atom["atom_id"], []).append(index)
        for indices in indices_by_atom_id.values():
            if len(indices) < 2:
                continue
            for index in indices:
                atom = normalized_added[index]
                if atom is None:  # pragma: no cover - filtered above
                    continue
                add_issue(
                    index,
                    "duplicates_added",
                    "与人工新增区的另一条 requirement 身份重复；请只保留一条",
                    _human_atom_statement(atom),
                )
        return [issues_by_index[index] for index in sorted(issues_by_index)]

    def _focus_atom_issue(self, issue: Mapping[str, Any]) -> None:
        step = issue["step"]
        self.review_tabs.selected_index = ATOM_STEP_TABS[step]
        controls = self._atom_widgets[issue["atom_id"]]
        controls["decision_details"].selected_index = 0
        if issue.get("open_advanced"):
            controls["advanced"].selected_index = 0
        # Native form controls remain focusable in widget frontends that do not
        # apply DOMWidget.tabbable to generic containers.
        controls["verdict"].focus()

    def _focus_added_atom_issue(self, _issue: Mapping[str, Any]) -> None:
        self.review_tabs.selected_index = 2
        self.advanced_operations.selected_index = 0

    def _focus_required_check_issue(self, _issue: Mapping[str, Any]) -> None:
        self.review_tabs.selected_index = 3

    def _focus_cpd_issue(self, issue: Mapping[str, Any]) -> None:
        self.review_tabs.selected_index = 4
        if "unrealized_revision" in issue.get("codes", ()):
            self.cpd_advanced.selected_index = 0

    def _show_completion_issues(
        self, form: Mapping[str, Any], gaps: Sequence[str]
    ) -> None:
        """Render a persistent, navigable completion report above the query."""

        self._clear_completion_issues()
        atom_issues = self._atom_completion_issues(form)
        added_atom_issues = self._added_atom_completion_issues(form)
        required_issues = self._required_check_completion_issues(form)
        cpd_issues = self._cpd_completion_issues(form)
        atom_gap_fragments = (
            "前3步还有 ",
            "张要求卡缺少判断依据",
            "张要求卡的判断与高级修改内容不一致",
        )
        required_gap_fragments = (
            "项总结未确认",
            "项最终总结缺少判断依据",
            "项‘我已修改相关要求’尚未反映为对应结构变化",
            "项最终总结仍选择‘当前总结准确’",
            "项最终总结指出源数据错误",
        )
        added_gap_fragments = ("人工新增要求有 ",)
        cpd_gap_fragments = ("第5步的 CPD",)
        other_gaps = [
            gap
            for gap in gaps
            if not any(
                fragment in gap
                for fragment in (
                    atom_gap_fragments
                    + required_gap_fragments
                    + added_gap_fragments
                    + cpd_gap_fragments
                )
            )
        ]
        summary_parts = []
        if atom_issues:
            summary_parts.append("{} 张问题要求卡".format(len(atom_issues)))
        if added_atom_issues:
            summary_parts.append(
                "{} 条冲突的人工新增要求".format(len(added_atom_issues))
            )
        if required_issues:
            summary_parts.append(
                "{} 项最终一致性总结".format(len(required_issues))
            )
        if cpd_issues:
            summary_parts.append("{} 项 CPD 结论".format(len(cpd_issues)))
        if other_gaps:
            summary_parts.append("{} 类其他待处理项".format(len(other_gaps)))
        issue_summary = "、".join(summary_parts)
        if atom_issues:
            issue_guidance = (
                "问题要求卡已逐张列出；第一张已自动展开，也可点击按钮定位。"
            )
        elif added_atom_issues:
            issue_guidance = (
                "第3步的人工新增区已自动打开；请删除重复项，或回到已有 source 卡修改。"
            )
        elif required_issues:
            issue_guidance = (
                "第4步已自动打开；请补全 support/response 判断，"
                "源数据有误时保持 subject 未完成并修正 source。"
            )
        elif cpd_issues:
            issue_guidance = (
                "第5步已自动打开；请按说明修改 CPD policy，或使用显式同步按钮"
                "更正当前结论。"
            )
        else:
            issue_guidance = "请按下方待处理项修正 CPD 或备注。"
        children = [
            self.widgets.HTML(
                "<div style='padding:10px 12px;border-left:4px solid #b00020;"
                "background:#fff5f5'><b>完成检查发现：{}。</b> "
                "{}</div>".format(
                    issue_summary, issue_guidance
                )
            )
        ]
        if atom_issues:
            for issue in atom_issues:
                detail = self.widgets.HTML(
                    "<div><b>{}</b> · 当前判断：{}<br>"
                    "<span style='font-size:15px'>{}</span><br>"
                    "<span style='color:#9c2f00'><b>需要处理：</b>{}</span></div>".format(
                        html.escape(ATOM_STEP_LABELS[issue["step"]]),
                        html.escape(issue["verdict"]),
                        html.escape(issue["statement"]),
                        html.escape("；".join(issue["problems"])),
                    )
                )
                locate = self.widgets.Button(
                    description="定位这张要求卡",
                    icon="location-arrow",
                    button_style="warning",
                )
                locate.on_click(
                    lambda _button, issue=issue: self._focus_atom_issue(issue)
                )
                children.append(
                    self.widgets.VBox(
                        [detail, locate],
                        layout=self.widgets.Layout(
                            border="1px solid #feb2b2",
                            padding="9px",
                            margin="0 0 7px 0",
                        ),
                    )
                )
        if added_atom_issues:
            for issue in added_atom_issues:
                detail = self.widgets.HTML(
                    "<div><b>第3步：规则与风险 → 人工新增要求</b><br>"
                    "<span style='font-size:15px'>{}</span><br>"
                    "<span style='color:#9c2f00'><b>需要处理：</b>{}</span></div>".format(
                        html.escape(issue["statement"]),
                        html.escape("；".join(issue["problems"])),
                    )
                )
                locate = self.widgets.Button(
                    description="定位到人工新增区",
                    icon="location-arrow",
                    button_style="warning",
                )
                locate.on_click(
                    lambda _button, issue=issue: self._focus_added_atom_issue(issue)
                )
                children.append(
                    self.widgets.VBox(
                        [detail, locate],
                        layout=self.widgets.Layout(
                            border="1px solid #feb2b2",
                            padding="9px",
                            margin="0 0 7px 0",
                        ),
                    )
                )
        if cpd_issues:
            for issue in cpd_issues:
                detail = self.widgets.HTML(
                    "<div><b>第5步：CPD 与备注</b><br>"
                    "<span style='font-size:15px'><b>CPD 结论与 policy 是否一致</b></span><br>"
                    "当前选择：{}<br>"
                    "<span style='color:#9c2f00'><b>需要处理：</b>{}</span></div>".format(
                        html.escape(issue["verdict"]),
                        html.escape("；".join(issue["problems"])),
                    )
                )
                locate = self.widgets.Button(
                    description="定位到第5步",
                    icon="location-arrow",
                    button_style="warning",
                )
                locate.on_click(
                    lambda _button, issue=issue: self._focus_cpd_issue(issue)
                )
                actions = [locate]
                if issue["recommended_verdict"]:
                    recommended = issue["recommended_verdict"]
                    apply_suggestion = self.widgets.Button(
                        description="改为“{}”".format(
                            CPD_VERDICT_LABELS[recommended]
                        ),
                        icon="check",
                        button_style="info",
                    )

                    def _apply_cpd_suggestion(
                        _button: Any,
                        issue: Mapping[str, Any] = issue,
                        recommended: str = recommended,
                        button: Any = apply_suggestion,
                    ) -> None:
                        self.cpd_verdict.value = recommended
                        self._focus_cpd_issue(issue)
                        button.disabled = True
                        button.description = "已同步为“{}”".format(
                            CPD_VERDICT_LABELS[recommended]
                        )

                    apply_suggestion.on_click(_apply_cpd_suggestion)
                    actions.append(apply_suggestion)
                children.append(
                    self.widgets.VBox(
                        [detail, self.widgets.HBox(actions)],
                        layout=self.widgets.Layout(
                            border="1px solid #f6ad55",
                            padding="9px",
                            margin="0 0 7px 0",
                        ),
                    )
                )
        if required_issues:
            for issue in required_issues:
                sources = issue["change_sources"]
                source_text = ""
                if sources:
                    shown = sources[:3]
                    source_text = "<br><b>触发变化的 requirement：</b>{}".format(
                        html.escape("；".join(shown))
                    )
                    if len(sources) > len(shown):
                        source_text += " 等 {} 项".format(len(sources))
                detail = self.widgets.HTML(
                    "<div><b>第4步：最终一致性确认</b><br>"
                    "<span style='font-size:15px'><b>{}</b></span><br>"
                    "当前选择：{}<br>"
                    "<span style='color:#9c2f00'><b>需要处理：</b>{}</span>{}</div>".format(
                        html.escape(issue["label"]),
                        html.escape(issue["verdict"]),
                        html.escape("；".join(issue["problems"])),
                        source_text,
                    )
                )
                locate = self.widgets.Button(
                    description="定位到第4步",
                    icon="location-arrow",
                    button_style="warning",
                )
                locate.on_click(
                    lambda _button, issue=issue: self._focus_required_check_issue(
                        issue
                    )
                )
                actions = [locate]
                if issue["recommended_verdict"]:
                    recommended = issue["recommended_verdict"]
                    apply_suggestion = self.widgets.Button(
                        description="改为“{}”".format(
                            REQUIRED_VERDICT_LABELS[recommended]
                        ),
                        icon="check",
                        button_style="info",
                    )

                    def _apply_suggestion(
                        _button: Any,
                        issue: Mapping[str, Any] = issue,
                        recommended: str = recommended,
                        button: Any = apply_suggestion,
                    ) -> None:
                        self._required_widgets[issue["check"]]["verdict"].value = (
                            recommended
                        )
                        self._focus_required_check_issue(issue)
                        button.disabled = True
                        button.description = "已同步为“{}”".format(
                            REQUIRED_VERDICT_LABELS[recommended]
                        )

                    apply_suggestion.on_click(_apply_suggestion)
                    actions.append(apply_suggestion)
                children.append(
                    self.widgets.VBox(
                        [detail, self.widgets.HBox(actions)],
                        layout=self.widgets.Layout(
                            border="1px solid #f6ad55",
                            padding="9px",
                            margin="0 0 7px 0",
                        ),
                    )
                )
        if other_gaps:
            children.append(
                self.widgets.HTML(
                    "<div style='padding:8px 10px;background:#fffaf0'>"
                    "<b>其他待处理项：</b><ul>{}</ul></div>".format(
                        "".join(
                            "<li>{}</li>".format(html.escape(gap))
                            for gap in other_gaps
                        )
                    )
                )
            )
        self.completion_issues.children = tuple(children)
        self.completion_issues.layout.display = ""
        if atom_issues:
            self._focus_atom_issue(atom_issues[0])
        elif added_atom_issues:
            self._focus_added_atom_issue(added_atom_issues[0])
        elif required_issues:
            self._focus_required_check_issue(required_issues[0])
        elif cpd_issues:
            self._focus_cpd_issue(cpd_issues[0])

    def _completion_gaps(self, form: Mapping[str, Any]) -> List[str]:
        """Return reviewer-facing omissions before the authoritative validator."""

        gaps = []
        required_issues = self._required_check_completion_issues(form)
        missing_check_count = sum(
            "missing_verdict" in issue["codes"] for issue in required_issues
        )
        if missing_check_count:
            gaps.append(
                "第4步还有 {} 项最终总结未确认".format(missing_check_count)
            )
        check_reason_count = sum(
            "missing_reason" in issue["codes"] for issue in required_issues
        )
        if check_reason_count:
            gaps.append("{} 项最终总结缺少判断依据".format(check_reason_count))
        accepted_drift_count = sum(
            "accepted_drift" in issue["codes"] for issue in required_issues
        )
        if accepted_drift_count:
            gaps.append(
                "{} 项最终总结仍选择‘当前总结准确’，但相关 requirements 已修改".format(
                    accepted_drift_count
                )
            )
        unrealized_revision_count = sum(
            "unrealized_revision" in issue["codes"] for issue in required_issues
        )
        if unrealized_revision_count:
            gaps.append(
                "{} 项‘我已修改相关要求’尚未反映为对应结构变化".format(
                    unrealized_revision_count
                )
            )
        source_blocker_count = sum(
            "source_blocker" in issue["codes"] for issue in required_issues
        )
        if source_blocker_count:
            gaps.append(
                "{} 项最终总结指出源数据错误，当前 subject 只能保持阻塞".format(
                    source_blocker_count
                )
            )

        atom_issues = self._atom_completion_issues(form)
        missing_atom_count = sum(
            "missing_verdict" in issue["codes"] for issue in atom_issues
        )
        if missing_atom_count:
            gaps.append(
                "前3步还有 {} 张要求卡未判断".format(missing_atom_count)
            )
        atom_reason_count = sum(
            "missing_reason" in issue["codes"] for issue in atom_issues
        )
        if atom_reason_count:
            gaps.append("{} 张要求卡缺少判断依据".format(atom_reason_count))
        target_mismatch_count = sum(
            "target_invariant" in issue["codes"] for issue in atom_issues
        )
        if target_mismatch_count:
            gaps.append(
                "{} 张要求卡的判断与高级修改内容不一致".format(
                    target_mismatch_count
                )
            )

        added_atom_issues = self._added_atom_completion_issues(form)
        if added_atom_issues:
            gaps.append(
                "人工新增要求有 {} 项与现有 source/target 重复或字段不完整".format(
                    len(added_atom_issues)
                )
            )

        cpd_issues = self._cpd_completion_issues(form)
        if cpd_issues:
            issue = cpd_issues[0]
            if "missing_verdict" in issue["codes"]:
                gaps.append("第5步的 CPD 结论尚未判断")
            if "missing_reason" in issue["codes"]:
                gaps.append("第5步的 CPD 结论缺少判断依据")
            if "accepted_drift" in issue["codes"]:
                gaps.append("第5步的 CPD policy 已修改，但结论仍选择当前正确")
            if "unrealized_revision" in issue["codes"]:
                gaps.append("第5步的 CPD 结论声称需要修改，但 policy 尚未实际变化")
            if "source_blocker" in issue["codes"]:
                gaps.append("第5步的 CPD 结论指出源数据错误，当前 subject 只能保持阻塞")
        try:
            added_atoms = _read_json_text(
                str(form.get("added_atoms_json", "[]")), "added atoms", list
            )
        except (TypeError, ValueError, ValidationError):
            added_atoms = []
        if added_atoms and not str(form.get("notes", "")).strip():
            gaps.append("新增 requirement 必须在审核备注中说明机器草稿遗漏了什么")
        return gaps

    def _complete_and_continue(self, _: Any = None) -> bool:
        """Advance from the completed subject, even if saving changes the visible queue."""

        return self._save_current(True, advance=True)

    def _next_unfinished_query_id(self, original: str) -> Optional[str]:
        ordered = self.session.ordered_query_ids
        start = ordered.index(original)
        following = ordered[start + 1:] + ordered[:start]
        visible = set(self.session.filtered_query_ids(self.filter.value, self.search.value))
        return next(
            (query_id for query_id in following
             if query_id in visible and self.session.status(query_id) != "complete"),
            None,
        )

    def _save_current(self, complete: bool, *, advance: bool = False) -> bool:
        if not self.current_query_id:
            return False
        try:
            form = self._collect_form()
            if complete:
                gaps = self._completion_gaps(form)
                if gaps:
                    self._show_completion_issues(form, gaps)
                    self._notify(
                        "还不能完成本条审核：已在当前 query 上方列出可定位的问题清单；"
                        "已定位第一个相关审核位置。请修正后重试，当前内容仍可保存为草稿。",
                        error=True,
                    )
                    return False
                seeded = self.session.mark_complete_and_seed_siblings(
                    self.current_query_id, form
                )
                if seeded:
                    self._notify(
                        "已通过正式逐题校验并标记 complete；同时为 {} 创建了"
                        "未完成的 precise 继承草稿：{}。尚未生成 gold。".format(
                            len(seeded), "、".join(seeded)
                        )
                    )
                else:
                    self._notify("已通过正式逐题校验并标记 complete；尚未生成 gold。")
            else:
                self.session.save_form(self.current_query_id, form)
                self._notify("checkpoint 已原子保存；它不是 submission 或 gold。")
            self._cancel_autosave()
            self._set_dirty(False)
            self._refresh_header()
            target = None
            if complete and advance:
                target = self._next_unfinished_query_id(self.current_query_id)
                if target is not None:
                    # Select the destination before rendering. Rebuilding the
                    # completed item first would create and immediately dispose
                    # an intermediate widget tree in the asynchronous frontend.
                    self.current_query_id = target
            self._refresh_query_options()
            if complete and advance:
                if target is not None:
                    # The native select stays mounted while cards are replaced.
                    self.query_select.focus()
                else:
                    self._notify(
                        "本条已通过校验并完成；当前筛选和搜索范围内没有其他待处理请求。"
                        "可切换筛选继续；整个 split 全部完成后再定稿。"
                    )
            return True
        except Exception as exc:
            self._notify(str(exc), error=True)
            return False

    def _navigate(self, offset: int) -> None:
        if not self.current_query_id:
            return
        original_query_id = self.current_query_id
        if self.dirty and not self._save_current(False):
            return
        # Saving can remove the current subject from a dynamic filter (for
        # example ``pending``) and ``_refresh_query_options`` will already move
        # the selection to the next visible subject.  Do not apply the offset a
        # second time in that case.
        if self.current_query_id != original_query_id:
            return
        visible = [value for _, value in self.query_select.options]
        if not visible or original_query_id not in visible:
            return
        index = visible.index(original_query_id)
        self.query_select.value = visible[(index + offset) % len(visible)]

    def _load_mechanical(self, _: Any) -> None:
        if self.current_query_id and quick_confirmation_issues(self.session.task(self.current_query_id)["oracle_draft"]["atoms"], self.session.task(self.current_query_id)["query_text"]):
            self._notify("存在未登记或未明确字段，不能整题预填确认；请逐条审阅或暂存疑问。", error=True)
            return
        if not self.current_query_id or not self.mechanical_ack.value:
            return
        self.session.save_form(
            self.current_query_id, self.session.mechanical_form(self.current_query_id)
        )
        self._notify("仅载入当前 subject 的机械保留草稿；请独立检查 Agent findings。")
        self._render_current()

    def _reset_current(self, _: Any) -> None:
        if not self.current_query_id or not self.reset_ack.value:
            return
        self.session.reset_form(self.current_query_id)
        self._notify("当前 subject 已恢复为空白 checkpoint。")
        self._render_current()

    def _finalize(self, _: Any) -> None:
        if self.finalize_confirmation.value != "FINALIZE":
            self._notify("请输入 FINALIZE 后再定稿。", error=True)
            return
        if not self._save_dirty_checkpoint():
            return
        progress = self.session.progress()
        if progress["subjects_complete"] != progress["subjects_total"]:
            self._notify(
                "当前修订已保存为 checkpoint；请重新校验受影响的 subject，"
                "全部 complete 后才能定稿。",
                error=True,
            )
            return
        try:
            result = self.session.finalize(Path(self.finalize_output.value))
            self._notify(
                "已通过 canonical finalizer 生成 {} 条 human gold：{}".format(
                    result["record_count"], result["output_dir"]
                )
            )
        except Exception as exc:
            self._notify(str(exc), error=True)


def launch_workbench(
    default_assignment_dir: Optional[Path] = None,
) -> Any:
    """Return an ipywidgets launcher; no assignment or gold is created yet."""

    try:
        import ipywidgets as widgets
        from IPython.display import display
    except ImportError as exc:  # pragma: no cover - environment-specific
        raise RuntimeError("launch_workbench requires ipywidgets and IPython") from exc

    assignment = widgets.Text(
        description="工作目录",
        value=str(
            Path(default_assignment_dir)
            if default_assignment_dir is not None
            else DEFAULT_ASSIGNMENT_DIR
        ),
        layout=widgets.Layout(width="85%"),
    )
    reviewer = widgets.Text(description="Reviewer ID", placeholder="真实 reviewer 的分配标签")
    split = widgets.Dropdown(
        description="Split",
        options=[
            ("Development（先完成）", "development"),
            ("Test（建议 development 完成后）", "test"),
        ],
        value="development",
    )
    start = widgets.Button(description="创建 / 恢复 workbench", button_style="primary")
    output = widgets.Output(layout=widgets.Layout(border="1px solid #ddd"))
    container = widgets.VBox()
    state: Dict[str, Optional[QueryReviewWorkbench]] = {"workbench": None}

    def _start(_: Any) -> None:
        with output:
            output.clear_output()
            try:
                current = state["workbench"]
                if current is not None and not current._save_dirty_checkpoint():
                    raise ValidationError("当前 workbench 草稿保存失败，已取消切换")
                ensure_review_assignment(reviewer.value, Path(assignment.value))
                session = QueryReviewSession.from_assignment(
                    Path(assignment.value), split.value
                )
                workbench = QueryReviewWorkbench(session)
                if current is not None:
                    current.close()
                container.children = [workbench.widget]
                state["workbench"] = workbench
                display(
                    widgets.HTML(
                        "<b>已加载 source-bound assignment。</b> Reviewer ID 只是分配标签；"
                        "真实人类身份由外部 custody 保证。"
                    )
                )
            except Exception as exc:
                display(
                    widgets.HTML(
                        "<span style='color:#b00020'>{}</span>".format(
                            html.escape(str(exc))
                        )
                    )
                )

    start.on_click(_start)
    launcher = widgets.VBox(
        [
            widgets.HTML(
                "<h2>ChatScene P0 Query Human Review Workbench</h2>"
                "<p>无需 API key、GPU、CARLA 或网络。先完成 development，再审核 test。"
                "机器建议和 checkpoint 都不是 human gold。</p>"
                "<p><b>审核顺序：</b>参与者与外部事件 → 道路与空间 → 规则与风险 → "
                "最终一致性确认 → CPD 与备注。默认界面使用自然语言；内部 atom 字段只在"
                "高级修改区显示。</p>"
            ),
            reviewer,
            assignment,
            split,
            start,
            output,
            container,
        ]
    )
    # Private diagnostic handle; it carries no answers and is not serialized
    # into the notebook.
    launcher._query_review_state = state
    return launcher


__all__ = [
    "QueryReviewSession",
    "QueryReviewWorkbench",
    "ensure_review_assignment",
    "launch_workbench",
    "response_from_form",
]
