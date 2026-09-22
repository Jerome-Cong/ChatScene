"""Source-bound review sessions, without notebook dependencies."""
import copy
import os
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence
from .errors import ValidationError
from .human_workflow import (
    atom_semantic_projection,
    export_query_review_bundle,
    finalize_query_review_bundle,
    validate_query_review_submission,
    validate_query_review_task_response,
)
from .jsonio import canonical_json_bytes, read_json, write_json, write_jsonl
from .review_forms import (
    _atom_batch_id,
    _atom_form_decision_has_content,
    _blank_form,
    _cpd_batch_id,
    _cpd_form_decision_has_content,
    _cpd_review_projection,
    _form_from_response,
    _inherited_surface_form,
    _json_copy,
    _json_sha256,
    _merge_inherited_surface_form,
    _normalize_human_atom,
    _pretty,
    _read_json_text,
    response_from_form,
)
from .review_store import _checkpoint_guard
from .paths import review_state_path
from .review_vocabulary import (
    ATOM_ACCEPT_REASON,
    BATCH_PERMITTED_REASON,
    CHECKPOINT_KEYS,
    CPD_ACCEPT_REASON,
    SURFACE_ORDER,
    WORKBENCH_CHECKPOINT_SCHEMA_VERSION,
)


class QueryReviewSession:
    """Crash-safe, source-bound state for one development or test split."""

    def __init__(
        self,
        bundle: Mapping[str, Any],
        *,
        library_source: Any,
        oracle_source: Any,
        split: str,
        checkpoint_path: Path,
        agent_reviews: Optional[Mapping[str, Mapping[str, Any]]] = None,
        agent_artifact_id: str = "none",
    ) -> None:
        if not isinstance(split, str) or not split.strip():
            raise ValidationError("split must be a nonempty suite label")
        self.bundle = _json_copy(bundle, "query bundle")
        self.packet = self.bundle["reviewer_packet"]
        self.reviewer_id = self.packet["reviewer_id"]
        self.library_source = library_source
        self.oracle_source = oracle_source
        self.split = split
        self.checkpoint_path = review_state_path(checkpoint_path)
        self.agent_reviews = dict(agent_reviews or {})
        self.agent_artifact_id = agent_artifact_id

        expected = export_query_review_bundle(
            library_source, oracle_source, reviewer_id=self.reviewer_id
        )
        if canonical_json_bytes(self.bundle) != canonical_json_bytes(expected):
            raise ValidationError("query bundle differs from canonical current sources")
        self.tasks_by_id = {
            task["subject_id"]: task for task in self.packet["tasks"]
        }
        self.ordered_query_ids = sorted(
            self.tasks_by_id,
            key=lambda query_id: (
                self.tasks_by_id[query_id]["query_record"]["intent_group_id"],
                SURFACE_ORDER[
                    self.tasks_by_id[query_id]["query_record"]["surface_style"]
                ],
                query_id,
            ),
        )
        self._state = self._load_or_create_checkpoint()
        self._atom_batch_specs = self._build_atom_batch_specs()
        self._cpd_batch_specs = self._build_cpd_batch_specs()

    @classmethod
    def from_assignment(cls, assignment_dir: Path, split: str) -> "QueryReviewSession":
        from .review_legacy import session_from_assignment
        return session_from_assignment(cls, assignment_dir, split)

    def _checkpoint_core(self, entries: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        return {
            "schema_version": WORKBENCH_CHECKPOINT_SCHEMA_VERSION,
            "artifact_type": "query_review_workbench_checkpoint",
            "human_gold": False,
            "formal_submission": False,
            "split": self.split,
            "reviewer_id": self.reviewer_id,
            "packet_id": self.packet["packet_id"],
            "source_binding": copy.deepcopy(self.packet["source_binding"]),
            "agent_review_artifact_id": self.agent_artifact_id,
            "entries": copy.deepcopy(list(entries)),
        }

    def _new_checkpoint(self) -> Dict[str, Any]:
        entries = [
            {
                "task_id": self.tasks_by_id[query_id]["task_id"],
                "subject_id": query_id,
                "subject_sha256": self.tasks_by_id[query_id]["subject_sha256"],
                "human_confirmed": False,
                "form": _blank_form(self.tasks_by_id[query_id]),
            }
            for query_id in self.ordered_query_ids
        ]
        core = self._checkpoint_core(entries)
        core["checkpoint_id"] = _json_sha256(core)
        return core

    def _validate_checkpoint(self, state: Mapping[str, Any]) -> Dict[str, Any]:
        if set(state) != CHECKPOINT_KEYS:
            raise ValidationError("workbench checkpoint fields are malformed")
        core = {key: value for key, value in state.items() if key != "checkpoint_id"}
        if state.get("checkpoint_id") != _json_sha256(core):
            raise ValidationError("workbench checkpoint hash does not match its content")
        for field, expected in (
            ("schema_version", WORKBENCH_CHECKPOINT_SCHEMA_VERSION),
            ("artifact_type", "query_review_workbench_checkpoint"),
            ("human_gold", False),
            ("formal_submission", False),
            ("split", self.split),
            ("reviewer_id", self.reviewer_id),
            ("packet_id", self.packet["packet_id"]),
            ("source_binding", self.packet["source_binding"]),
            ("agent_review_artifact_id", self.agent_artifact_id),
        ):
            if state.get(field) != expected:
                raise ValidationError("workbench checkpoint {} binding is stale".format(field))
        entries = state.get("entries")
        if not isinstance(entries, list) or len(entries) != len(self.tasks_by_id):
            raise ValidationError("workbench checkpoint subject coverage is incomplete")
        by_subject = {}
        for entry in entries:
            if not isinstance(entry, Mapping) or set(entry) != {
                "task_id",
                "subject_id",
                "subject_sha256",
                "human_confirmed",
                "form",
            }:
                raise ValidationError("workbench checkpoint entry is malformed")
            query_id = entry["subject_id"]
            task = self.tasks_by_id.get(query_id)
            if (
                task is None
                or query_id in by_subject
                or entry["task_id"] != task["task_id"]
                or entry["subject_sha256"] != task["subject_sha256"]
                or type(entry["human_confirmed"]) is not bool
                or not isinstance(entry["form"], Mapping)
            ):
                raise ValidationError("workbench checkpoint task binding is stale")
            by_subject[query_id] = entry
            if entry["human_confirmed"]:
                response = response_from_form(task, entry["form"])
                validate_query_review_task_response(
                    task,
                    response,
                    reviewer_id=self.reviewer_id,
                    require_confirmation=True,
                )
        if set(by_subject) != set(self.tasks_by_id):
            raise ValidationError("workbench checkpoint does not cover the packet")
        return _json_copy(state, "workbench checkpoint")

    def _load_or_create_checkpoint(self) -> Dict[str, Any]:
        with _checkpoint_guard(self.checkpoint_path):
            if self.checkpoint_path.exists():
                return self._validate_checkpoint(read_json(self.checkpoint_path))
            state = self._new_checkpoint()
            write_json(self.checkpoint_path, state)
            return state

    def _entry(self, query_id: str) -> Dict[str, Any]:
        for entry in self._state["entries"]:
            if entry["subject_id"] == query_id:
                return entry
        raise ValidationError("unknown query subject {!r}".format(query_id))

    def _persist(self, entries: Sequence[Mapping[str, Any]]) -> None:
        core = self._checkpoint_core(entries)
        core["checkpoint_id"] = _json_sha256(core)
        with _checkpoint_guard(self.checkpoint_path):
            if not self.checkpoint_path.is_file():
                raise ValidationError("workbench checkpoint disappeared before save")
            live = read_json(self.checkpoint_path)
            if live.get("checkpoint_id") != self._state.get("checkpoint_id"):
                raise ValidationError(
                    "checkpoint 已被另一会话修改；本次未覆盖，请关闭其他标签页并重新加载"
                )
            write_json(self.checkpoint_path, core)
        self._state = core

    def task(self, query_id: str) -> Mapping[str, Any]:
        try:
            return self.tasks_by_id[query_id]
        except KeyError as exc:
            raise ValidationError("unknown query subject {!r}".format(query_id)) from exc

    def agent_review(self, query_id: str) -> Mapping[str, Any]:
        return self.agent_reviews.get(query_id, {})

    def form(self, query_id: str) -> Dict[str, Any]:
        return copy.deepcopy(self._entry(query_id)["form"])

    def mechanical_form(self, query_id: str) -> Dict[str, Any]:
        response = self.task(query_id)["machine_recommendation"]["recommended_response"]
        return _form_from_response(response)

    def _build_atom_batch_specs(self) -> Dict[str, Dict[str, Any]]:
        groups: Dict[str, Dict[str, Any]] = {}
        for query_id in self.ordered_query_ids:
            task = self.task(query_id)
            record = task["query_record"]
            for atom in task["oracle_draft"]["atoms"]:
                batch_id = _atom_batch_id(atom)
                group = groups.setdefault(
                    batch_id,
                    {
                        "batch_id": batch_id,
                        "atom": atom_semantic_projection(atom),
                        "instances": [],
                    },
                )
                group["instances"].append(
                    {
                        "subject_id": query_id,
                        "atom_id": atom["atom_id"],
                        "intent_group_id": record["intent_group_id"],
                        "surface_style": record["surface_style"],
                        "query_text": task["query_text"],
                    }
                )
        return groups

    def _build_cpd_batch_specs(self) -> Dict[str, Dict[str, Any]]:
        groups: Dict[str, Dict[str, Any]] = {}
        for query_id in self.ordered_query_ids:
            task = self.task(query_id)
            record = task["query_record"]
            policy = task["oracle_draft"]["cpd_policy"]
            batch_id = _cpd_batch_id(record["surface_style"], policy)
            group = groups.setdefault(
                batch_id,
                {
                    "batch_id": batch_id,
                    "surface_style": record["surface_style"],
                    "policy": _cpd_review_projection(policy),
                    "instances": [],
                },
            )
            group["instances"].append(
                {
                    "subject_id": query_id,
                    "intent_group_id": record["intent_group_id"],
                    "surface_style": record["surface_style"],
                    "query_text": task["query_text"],
                }
            )
        return groups

    @staticmethod
    def _atom_decision_has_content(decision: Mapping[str, Any]) -> bool:
        return _atom_form_decision_has_content(decision)

    @staticmethod
    def _cpd_decision_has_content(decision: Mapping[str, Any]) -> bool:
        return _cpd_form_decision_has_content(decision)

    def atom_review_batches(self) -> List[Dict[str, Any]]:
        """Return split-local semantic batches with live per-instance state."""

        result = []
        for spec in self._atom_batch_specs.values():
            batch = copy.deepcopy(spec)
            for instance in batch["instances"]:
                entry = self._entry(instance["subject_id"])
                decision = next(
                    item
                    for item in entry["form"]["atom_decisions"]
                    if item["atom_id"] == instance["atom_id"]
                )
                instance["human_confirmed"] = entry["human_confirmed"]
                instance["verdict"] = str(decision.get("verdict", ""))
                instance["has_content"] = self._atom_decision_has_content(decision)
                agent = self.agent_review(instance["subject_id"])
                recommendation = next(
                    (
                        item
                        for item in agent.get("atom_decisions", [])
                        if item.get("atom_id") == instance["atom_id"]
                    ),
                    {},
                )
                instance["agent_recommendation"] = recommendation.get(
                    "recommendation", "unavailable"
                )
                instance["agent_reason"] = str(recommendation.get("reason", ""))
            result.append(batch)
        return sorted(
            result,
            key=lambda item: (
                str(item["atom"].get("category")),
                str(item["atom"].get("predicate")),
                canonical_json_bytes(item["atom"]),
            ),
        )

    def cpd_review_batches(self) -> List[Dict[str, Any]]:
        """Return split-local, surface-scoped CPD batches with live state."""

        result = []
        for spec in self._cpd_batch_specs.values():
            batch = copy.deepcopy(spec)
            for instance in batch["instances"]:
                entry = self._entry(instance["subject_id"])
                decision = entry["form"]["cpd_decision"]
                instance["human_confirmed"] = entry["human_confirmed"]
                instance["verdict"] = str(decision.get("verdict", ""))
                instance["has_content"] = self._cpd_decision_has_content(decision)
                recommendation = self.agent_review(instance["subject_id"]).get(
                    "cpd_decision", {}
                )
                instance["agent_recommendation"] = recommendation.get(
                    "recommendation", "unavailable"
                )
                instance["agent_reason"] = str(recommendation.get("reason", ""))
            result.append(batch)
        return sorted(
            result,
            key=lambda item: (
                SURFACE_ORDER[item["surface_style"]],
                canonical_json_bytes(item["policy"]),
            ),
        )

    def apply_atom_batch_decision(
        self, batch_id: str, action: str, *, reason: str = ""
    ) -> Dict[str, int]:
        """Fill blank instances in one semantic atom batch without confirming them."""

        if batch_id not in self._atom_batch_specs:
            raise ValidationError("unknown atom batch")
        if action not in ("accept", "reject", "permitted"):
            raise ValidationError("atom batch action must be accept, reject, or permitted")
        reason = str(reason).strip()
        if action == "reject" and not reason:
            raise ValidationError("批量不计分必须说明为什么该要求不应计分")

        entries = copy.deepcopy(self._state["entries"])
        entries_by_id = {entry["subject_id"]: entry for entry in entries}
        applied = skipped_complete = skipped_existing = 0
        for instance in self._atom_batch_specs[batch_id]["instances"]:
            entry = entries_by_id[instance["subject_id"]]
            if entry["human_confirmed"]:
                skipped_complete += 1
                continue
            decision = next(
                item
                for item in entry["form"]["atom_decisions"]
                if item["atom_id"] == instance["atom_id"]
            )
            if self._atom_decision_has_content(decision):
                skipped_existing += 1
                continue
            atom = next(
                item
                for item in self.task(instance["subject_id"])["oracle_draft"]["atoms"]
                if item["atom_id"] == instance["atom_id"]
            )
            if action == "accept" or (
                action == "permitted" and atom.get("layer") == "permitted"
            ):
                decision.update(
                    {
                        "verdict": "accept",
                        "reason": ATOM_ACCEPT_REASON,
                        "replacement_atoms_json": "[]",
                    }
                )
            elif action == "reject":
                decision.update(
                    {
                        "verdict": "reject",
                        "reason": reason,
                        "replacement_atoms_json": "[]",
                    }
                )
            else:
                if atom.get("polarity") != "present":
                    raise ValidationError(
                        "禁止出现的要求不能批量改为允许；请转到逐题高级编辑"
                    )
                target = {
                    field: copy.deepcopy(atom[field])
                    for field in (
                        "category",
                        "predicate",
                        "arguments",
                        "polarity",
                        "weight",
                    )
                }
                if atom.get("notes") is not None:
                    target["notes"] = str(atom["notes"])
                target["layer"] = "permitted"
                _normalize_human_atom(target, instance["subject_id"])
                decision.update(
                    {
                        "verdict": "modify",
                        "reason": reason or BATCH_PERMITTED_REASON,
                        "replacement_atoms_json": _pretty([target]),
                    }
                )
            applied += 1
        if applied:
            self._persist(entries)
        return {
            "instances_total": len(self._atom_batch_specs[batch_id]["instances"]),
            "applied": applied,
            "skipped_complete": skipped_complete,
            "skipped_existing": skipped_existing,
        }

    def apply_cpd_batch_decision(
        self,
        batch_id: str,
        verdict: str,
        *,
        reason: str = "",
        replacement_policy_json: str = "{}",
    ) -> Dict[str, int]:
        """Fill blank CPD instances in one surface-scoped policy batch."""

        if batch_id not in self._cpd_batch_specs:
            raise ValidationError("unknown CPD batch")
        if verdict not in ("accept", "revise", "reject"):
            raise ValidationError("CPD batch verdict must be accept, revise, or reject")
        reason = str(reason).strip()
        replacement_text = "{}"
        if verdict == "accept":
            reason = CPD_ACCEPT_REASON
        elif not reason:
            raise ValidationError("批量修改或拒绝 CPD 必须说明理由")
        if verdict == "revise":
            replacement = _read_json_text(
                str(replacement_policy_json), "批量 CPD replacement policy", dict
            )
            if _cpd_review_projection(replacement) == self._cpd_batch_specs[batch_id][
                "policy"
            ]:
                raise ValidationError("批量 CPD 修改必须产生实际 policy 变化")
            replacement_text = _pretty(replacement)

        entries = copy.deepcopy(self._state["entries"])
        entries_by_id = {entry["subject_id"]: entry for entry in entries}
        applied = skipped_complete = skipped_existing = 0
        for instance in self._cpd_batch_specs[batch_id]["instances"]:
            entry = entries_by_id[instance["subject_id"]]
            if entry["human_confirmed"]:
                skipped_complete += 1
                continue
            decision = entry["form"]["cpd_decision"]
            if self._cpd_decision_has_content(decision):
                skipped_existing += 1
                continue
            decision.update(
                {
                    "verdict": verdict,
                    "reason": reason,
                    "replacement_policy_json": replacement_text,
                }
            )
            entry["form"]["required_check_decisions"][
                "cpd_common_eligibility"
            ] = {"verdict": verdict, "reason": reason}
            applied += 1
        if applied:
            self._persist(entries)
        return {
            "instances_total": len(self._cpd_batch_specs[batch_id]["instances"]),
            "applied": applied,
            "skipped_complete": skipped_complete,
            "skipped_existing": skipped_existing,
        }

    def save_form(
        self, query_id: str, form: Mapping[str, Any], *, human_confirmed: bool = False
    ) -> None:
        normalized_form = _json_copy(form, "workbench form")
        if human_confirmed:
            response = response_from_form(self.task(query_id), normalized_form)
            validate_query_review_task_response(
                self.task(query_id),
                response,
                reviewer_id=self.reviewer_id,
                require_confirmation=True,
            )
        entries = copy.deepcopy(self._state["entries"])
        entry = next(item for item in entries if item["subject_id"] == query_id)
        entry["form"] = normalized_form
        entry["human_confirmed"] = bool(human_confirmed)
        self._persist(entries)

    def mark_complete(self, query_id: str, form: Mapping[str, Any]) -> None:
        self.save_form(query_id, form, human_confirmed=True)

    def _completed_precise_query_id(self, query_id: str) -> Optional[str]:
        task = self.task(query_id)
        intent = task["query_record"]["intent_group_id"]
        for candidate in self.ordered_query_ids:
            candidate_task = self.task(candidate)
            record = candidate_task["query_record"]
            if (
                record["intent_group_id"] == intent
                and record["surface_style"] == "precise"
                and self._entry(candidate)["human_confirmed"]
            ):
                return candidate
        return None

    def can_seed_from_precise(self, query_id: str) -> bool:
        """Return whether precise inheritance can fill any untouched target slot."""

        entry = self._entry(query_id)
        style = self.task(query_id)["query_record"]["surface_style"]
        precise_query_id = self._completed_precise_query_id(query_id)
        if (
            style not in ("partial", "vague")
            or entry["human_confirmed"]
            or precise_query_id is None
        ):
            return False
        inherited = self._seeded_form_from_precise(precise_query_id, query_id)
        merged = _merge_inherited_surface_form(entry["form"], inherited)
        return canonical_json_bytes(merged) != canonical_json_bytes(entry["form"])

    def _seeded_form_from_precise(
        self, precise_query_id: str, target_query_id: str
    ) -> Dict[str, Any]:
        return _inherited_surface_form(
            self.task(precise_query_id),
            self.form(precise_query_id),
            self.task(target_query_id),
            self.mechanical_form(target_query_id),
        )

    def seed_from_completed_precise(self, query_id: str) -> str:
        """Fill untouched partial/vague slots and keep the subject incomplete."""

        if not self.can_seed_from_precise(query_id):
            raise ValidationError(
                "当前 partial/vague 没有可从已完成 precise 安全补入的空白项"
            )
        precise_query_id = self._completed_precise_query_id(query_id)
        if precise_query_id is None:  # pragma: no cover - guarded above
            raise ValidationError("同 intent 的 precise 尚未完成")
        entries = copy.deepcopy(self._state["entries"])
        target_entry = next(
            item for item in entries if item["subject_id"] == query_id
        )
        target_entry["form"] = _merge_inherited_surface_form(
            target_entry["form"],
            self._seeded_form_from_precise(precise_query_id, query_id),
        )
        target_entry["human_confirmed"] = False
        self._persist(entries)
        return precise_query_id

    def mark_complete_and_seed_siblings(
        self, query_id: str, form: Mapping[str, Any]
    ) -> List[str]:
        """Complete one subject and atomically seed blank sibling drafts."""

        normalized_form = _json_copy(form, "workbench form")
        response = response_from_form(self.task(query_id), normalized_form)
        validate_query_review_task_response(
            self.task(query_id),
            response,
            reviewer_id=self.reviewer_id,
            require_confirmation=True,
        )
        entries = copy.deepcopy(self._state["entries"])
        source_entry = next(
            item for item in entries if item["subject_id"] == query_id
        )
        source_entry["form"] = normalized_form
        source_entry["human_confirmed"] = True

        seeded = []
        record = self.task(query_id)["query_record"]
        if record["surface_style"] == "precise":
            intent = record["intent_group_id"]
            for target_query_id in self.ordered_query_ids:
                target_record = self.task(target_query_id)["query_record"]
                if (
                    target_record["intent_group_id"] != intent
                    or target_record["surface_style"] not in ("partial", "vague")
                ):
                    continue
                target_entry = next(
                    item
                    for item in entries
                    if item["subject_id"] == target_query_id
                )
                if target_entry["human_confirmed"]:
                    continue
                inherited = _inherited_surface_form(
                    self.task(query_id),
                    normalized_form,
                    self.task(target_query_id),
                    self.mechanical_form(target_query_id),
                )
                merged = _merge_inherited_surface_form(target_entry["form"], inherited)
                if canonical_json_bytes(merged) != canonical_json_bytes(
                    target_entry["form"]
                ):
                    target_entry["form"] = merged
                    target_entry["human_confirmed"] = False
                    seeded.append(target_query_id)
        self._persist(entries)
        return seeded

    def reset_form(self, query_id: str) -> None:
        self.save_form(query_id, _blank_form(self.task(query_id)))

    @staticmethod
    def _form_has_content(form: Mapping[str, Any]) -> bool:
        checks = form.get("required_check_decisions", {})
        if any(item.get("verdict") or item.get("reason") for item in checks.values()):
            return True
        if any(
            item.get("verdict")
            or item.get("reason")
            or str(item.get("replacement_atoms_json", "[]")).strip() not in ("", "[]")
            for item in form.get("atom_decisions", [])
        ):
            return True
        cpd = form.get("cpd_decision", {})
        return bool(
            cpd.get("verdict")
            or cpd.get("reason")
            or str(cpd.get("replacement_policy_json", "{}")).strip() not in ("", "{}")
            or str(form.get("added_atoms_json", "[]")).strip() not in ("", "[]")
            or form.get("notes")
        )

    @staticmethod
    def _blocked_by_source_or_reject(form: Mapping[str, Any]) -> bool:
        checks = form.get("required_check_decisions", {})
        support = checks.get("support_and_response_disposition", {}).get("verdict")
        if support in ("revise", "reject"):
            return True
        if any(item.get("verdict") == "reject" for item in checks.values()):
            return True
        return form.get("cpd_decision", {}).get("verdict") == "reject"

    def status(self, query_id: str) -> str:
        entry = self._entry(query_id)
        form = entry["form"]
        if entry["human_confirmed"]:
            return "complete"
        if not self._form_has_content(form):
            return "pending"
        if self._blocked_by_source_or_reject(form):
            return "blocked"
        try:
            response = response_from_form(self.task(query_id), form)
            validate_query_review_task_response(
                self.task(query_id),
                response,
                reviewer_id=self.reviewer_id,
                require_confirmation=True,
            )
        except (ValidationError, KeyError, TypeError, ValueError):
            return "draft"
        return "locally_valid"

    def progress(self) -> Dict[str, Any]:
        statuses = {query_id: self.status(query_id) for query_id in self.ordered_query_ids}
        total_slots = 0
        completed_slots = 0
        for query_id in self.ordered_query_ids:
            form = self.form(query_id)
            slots = list(form["required_check_decisions"].values()) + list(
                form["atom_decisions"]
            ) + [form["cpd_decision"]]
            total_slots += len(slots)
            completed_slots += sum(
                1 for item in slots if item.get("verdict") and str(item.get("reason", "")).strip()
            )
        return {
            "subjects_total": len(statuses),
            "subjects_complete": sum(status == "complete" for status in statuses.values()),
            "decision_slots_total": total_slots,
            "decision_slots_completed": completed_slots,
            "status_counts": {
                status: sum(value == status for value in statuses.values())
                for status in ("pending", "draft", "locally_valid", "blocked", "complete")
            },
            "human_gold_records": 0,
        }

    def has_attention(self, query_id: str, *, human_only: bool = False) -> bool:
        review = self.agent_review(query_id)
        decisions = list(review.get("required_check_decisions", {}).values())
        decisions.extend(review.get("atom_decisions", []))
        if review.get("cpd_decision"):
            decisions.append(review["cpd_decision"])
        target = (
            {"human_judgment_required"}
            if human_only
            else {"revise_current", "reject_current", "human_judgment_required"}
        )
        return any(item.get("recommendation") in target for item in decisions)

    def filtered_query_ids(self, status_filter: str = "all", search: str = "") -> List[str]:
        needle = search.strip().lower()
        result = []
        for query_id in self.ordered_query_ids:
            task = self.task(query_id)
            if status_filter in {
                "pending",
                "draft",
                "locally_valid",
                "blocked",
                "complete",
            } and self.status(query_id) != status_filter:
                continue
            if status_filter == "attention" and not self.has_attention(query_id):
                continue
            if status_filter == "human_judgment" and not self.has_attention(
                query_id, human_only=True
            ):
                continue
            if status_filter in ("attention", "human_judgment") and self.status(
                query_id
            ) == "complete":
                continue
            haystack = " ".join(
                (
                    query_id,
                    str(task["query_record"].get("intent_group_id", "")),
                    str(task.get("query_text", "")),
                )
            ).lower()
            if needle and needle not in haystack:
                continue
            result.append(query_id)
        return result

    def intent_query_ids(self, query_id: str) -> List[str]:
        intent = self.task(query_id)["query_record"]["intent_group_id"]
        return [
            candidate
            for candidate in self.ordered_query_ids
            if self.task(candidate)["query_record"]["intent_group_id"] == intent
        ]

    def build_submission(self) -> Dict[str, Any]:
        responses = []
        for task in self.packet["tasks"]:
            query_id = task["subject_id"]
            entry = self._entry(query_id)
            if not entry["human_confirmed"]:
                raise ValidationError("{} 尚未明确标记 complete".format(query_id))
            response = response_from_form(task, entry["form"])
            validate_query_review_task_response(
                task,
                response,
                reviewer_id=self.reviewer_id,
                require_confirmation=True,
            )
            responses.append(
                {
                    "task_id": task["task_id"],
                    "subject_id": query_id,
                    "subject_sha256": task["subject_sha256"],
                    "response": response,
                }
            )
        submission = copy.deepcopy(self.packet["submission_template"])
        submission["submission_status"] = "complete"
        submission["responses"] = responses
        validate_query_review_submission(
            self.packet,
            submission,
            library_source=self.library_source,
            oracle_source=self.oracle_source,
        )
        return submission

    def finalize(self, output_dir: Path) -> Dict[str, Any]:
        """Finalize one complete split into a new, non-overwritten directory."""

        with _checkpoint_guard(self.checkpoint_path):
            if not self.checkpoint_path.is_file():
                raise ValidationError("workbench checkpoint is missing at finalization")
            live = self._validate_checkpoint(read_json(self.checkpoint_path))
            if live["checkpoint_id"] != self._state["checkpoint_id"]:
                raise ValidationError(
                    "checkpoint 已被另一会话更新；陈旧 workbench 不得生成 human gold"
                )
            return self._finalize_locked(output_dir)

    def _finalize_locked(self, output_dir: Path) -> Dict[str, Any]:
        """Build and publish while the checkpoint's OS lock is held."""

        output_dir = review_state_path(output_dir)
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        submission = self.build_submission()
        result = finalize_query_review_bundle(
            self.bundle,
            submission,
            library_source=self.library_source,
            oracle_source=self.oracle_source,
        )
        try:
            output_dir.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise ValidationError("finalization output 已存在；请选择新目录") from exc
        try:
            write_json(output_dir / "submission.json", submission)
            write_jsonl(
                output_dir / "confirmed_oracle.jsonl", result["confirmed_oracles"]
            )
            write_jsonl(
                output_dir / "human_query_gold.jsonl", result["human_gold_records"]
            )
            summary = {
                key: value
                for key, value in result.items()
                if key not in ("confirmed_oracles", "human_gold_records")
            }
            summary["record_count"] = len(result["confirmed_oracles"])
            summary["checkpoint_id"] = self._state["checkpoint_id"]
            summary["human_custody_required"] = True
            # This completion receipt is written last. A failed run retains its
            # exclusively claimed directory for inspection instead of deleting
            # or silently reusing it.
            write_json(output_dir / "finalization_summary.json", summary)
            directory_fd = os.open(
                str(output_dir), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except Exception as exc:
            raise ValidationError(
                "finalization 写入失败；已保留未完成目录 {} 供人工检查".format(
                    output_dir
                )
            ) from exc
        return {
            "output_dir": str(output_dir),
            "record_count": len(result["confirmed_oracles"]),
            "submission": str(output_dir / "submission.json"),
            "confirmed_oracle": str(output_dir / "confirmed_oracle.jsonl"),
            "human_query_gold": str(output_dir / "human_query_gold.jsonl"),
        }
