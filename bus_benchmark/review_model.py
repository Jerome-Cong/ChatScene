"""Proposal/edit/explicit-confirm protocol; no operation here creates gold."""

import copy
from typing import Mapping

from .errors import ValidationError
from .human_workflow import validate_query_review_task_response
from .jsonio import canonical_json_bytes, sha256_bytes, sha256_file
from .paths import asset_path
from .review_forms import _form_from_response, _json_copy, response_from_form
from .review_presentation import project_atom, quick_confirmation_issues
from .review_registry import REGISTRY_VERSION, FIELD_DEFINITIONS, PREDICATE_DEFINITIONS, TOKEN_TRANSLATIONS
from .review_vocabulary import ATOM_ACCEPT_REASON, CHECK_ACCEPT_REASON, CPD_ACCEPT_REASON, MECHANICAL_REASON_PREFIX

MODEL_VERSION = "1"
GUIDE_VERSION = "1"
COMPILER_VERSION = "1"
_DRAFT_KEYS = {"model_version", "task_binding", "proposal_id", "reviewer_id", "form", "revision", "edit_sources", "receipts", "issues", "status", "human_gold"}


def content_hash(value):
    return sha256_bytes(canonical_json_bytes(value))


def task_binding(task):
    return {
        "task_id": task["task_id"], "subject_id": task["subject_id"],
        "subject_sha256": task["subject_sha256"], "task_sha256": content_hash(task),
        "query_sha256": content_hash(task["query_text"]),
        "model_version": MODEL_VERSION, "registry_version": REGISTRY_VERSION,
        "guide_version": GUIDE_VERSION, "compiler_version": COMPILER_VERSION,
        "guide_sha256": sha256_file(asset_path("review", "guide_zh.md")),
        "registry_sha256": content_hash([FIELD_DEFINITIONS, PREDICATE_DEFINITIONS, TOKEN_TRANSLATIONS]),
    }


def make_proposal(task, form=None, provenance=None):
    """Separate machine artifact; its contents carry zero human attestations."""
    core = {
        "artifact_type": "review_machine_proposal", "model_version": MODEL_VERSION,
        "task_binding": task_binding(task), "human_gold": False,
        "form": _json_copy(form if form is not None else _form_from_response(task["machine_recommendation"]["recommended_response"]), "proposal form"),
        "provenance": _json_copy(provenance or {"source": "legacy_machine_recommendation"}, "proposal provenance"),
    }
    return {**core, "proposal_id": content_hash(core)}


def validate_proposal(task, proposal):
    if not isinstance(proposal, Mapping) or set(proposal) != {"artifact_type", "model_version", "task_binding", "human_gold", "form", "provenance", "proposal_id"}:
        raise ValidationError("malformed machine proposal")
    if proposal["artifact_type"] != "review_machine_proposal" or proposal["model_version"] != MODEL_VERSION or proposal["human_gold"] is not False:
        raise ValidationError("machine proposal must remain non-gold")
    if not isinstance(proposal["form"], Mapping) or not isinstance(proposal["provenance"], Mapping):
        raise ValidationError("proposal form and provenance must be objects")
    if proposal["task_binding"] != task_binding(task) or proposal["proposal_id"] != content_hash({k: v for k, v in proposal.items() if k != "proposal_id"}):
        raise ValidationError("proposal source/version/content binding differs")
    _json_copy(proposal, "proposal")


def new_draft(task, proposal, reviewer_id):
    validate_proposal(task, proposal)
    if not isinstance(reviewer_id, str) or not reviewer_id.strip():
        raise ValidationError("reviewer ID must be nonempty")
    return {
        "model_version": MODEL_VERSION, "task_binding": task_binding(task),
        "proposal_id": proposal["proposal_id"], "reviewer_id": reviewer_id,
        "form": copy.deepcopy(proposal["form"]), "revision": 0,
        "edit_sources": [], "receipts": [], "issues": [],
        "status": "draft", "human_gold": False,
    }


def validate_draft(task, proposal, draft):
    validate_proposal(task, proposal)
    if not isinstance(draft, Mapping) or set(draft) != _DRAFT_KEYS:
        raise ValidationError("malformed review draft")
    if draft["human_gold"] is not False or draft["model_version"] != MODEL_VERSION:
        raise ValidationError("review draft cannot claim gold or another version")
    if draft["task_binding"] != task_binding(task) or draft["proposal_id"] != proposal["proposal_id"]:
        raise ValidationError("draft source/proposal/version binding differs")
    if not isinstance(draft["reviewer_id"], str) or not draft["reviewer_id"].strip():
        raise ValidationError("draft reviewer is missing")
    if type(draft["revision"]) is not int or draft["revision"] < 0 or draft["status"] not in ("draft", "submitted", "deferred", "requires_source_fix"):
        raise ValidationError("invalid draft revision/status")
    if any(not isinstance(draft[k], list) for k in ("receipts", "issues", "edit_sources")) or not isinstance(draft["form"], Mapping):
        raise ValidationError("invalid draft form/audit collections")
    _json_copy(draft, "draft")


def edit_draft(task, proposal, draft, form, origin="human"):
    validate_draft(task, proposal, draft)
    if origin not in ("human", "inherited", "machine_proposal"):
        raise ValidationError("unknown edit origin")
    if not isinstance(form, Mapping):
        raise ValidationError("edited form must be an object")
    result = copy.deepcopy(draft)
    result["form"] = _json_copy(form, "edited form")
    result["revision"] += 1
    result["edit_sources"].append({"revision": result["revision"], "origin": origin, "form_sha256": content_hash(result["form"])})
    result["receipts"] = []
    result["status"] = "draft"
    return result


def confirmation_projection(task, proposal, draft):
    """The frontend must display this snapshot before sending its digest back."""
    validate_draft(task, proposal, draft)
    response = response_from_form(task, draft["form"])
    source_atoms = {a["atom_id"]: a for a in task["oracle_draft"]["atoms"]}
    units = []
    for decision in response["atom_decisions"]:
        source = source_atoms[decision["atom_id"]]
        units.append({
            "id": "atom:" + decision["atom_id"],
            "source": project_atom(source, task["query_text"]),
            "decision": copy.deepcopy(decision),
            "replacement_views": [project_atom(a, task["query_text"]) for a in decision["replacement_atoms"]],
        })
    mapped = {a["atom_id"] for d in response["atom_decisions"] for a in d["replacement_atoms"]}
    mapped.update(d["atom_id"] for d in response["atom_decisions"] if d["verdict"] == "accept")
    units.extend([
        {"id": "support", "expected_support": task["oracle_draft"]["expected_support"], "acceptable_response": task["oracle_draft"]["acceptable_response"], "decision": response["required_check_decisions"]["support_and_response_disposition"]},
        {"id": "cpd", "policy": response["proposed_oracle"]["cpd_policy"], "decision": response["cpd_decision"]},
        {"id": "additions", "atoms": [project_atom(a, task["query_text"]) for a in response["proposed_oracle"]["atoms"] if a["atom_id"] not in mapped]},
        {"id": "notes", "text": response["notes"]},
    ])
    core = {"task_binding": task_binding(task), "proposal_id": proposal["proposal_id"], "reviewer_id": draft["reviewer_id"], "revision": draft["revision"], "form": draft["form"], "edit_sources": draft["edit_sources"], "query_text": task["query_text"], "units": units}
    return {**_json_copy(core, "confirmation projection"), "content_sha256": content_hash(core)}


def _attested_form(form):
    """Compiler-generated receipts are not claims that a human typed a reason."""
    result = copy.deepcopy(form)
    decisions = [(d, ATOM_ACCEPT_REASON) for d in result["atom_decisions"]]
    decisions += [(d, CHECK_ACCEPT_REASON) for d in result["required_check_decisions"].values()]
    decisions.append((result["cpd_decision"], CPD_ACCEPT_REASON))
    for decision, standard in decisions:
        if str(decision.get("reason", "")).startswith(MECHANICAL_REASON_PREFIX):
            decision["reason"] = standard if decision.get("verdict") == "accept" else "Human attestation: explicitly accepted the displayed machine revision. Machine rationale: " + decision["reason"]
    return result


def _assert_confirmable(task, proposal, draft):
    validate_draft(task, proposal, draft)
    if draft["issues"] or draft["status"] in ("deferred", "requires_source_fix"):
        raise ValidationError("unresolved/deferred/source-fix issues prevent confirmation")
    response = response_from_form(task, _attested_form(draft["form"]))
    if quick_confirmation_issues(response["proposed_oracle"]["atoms"], task["query_text"]):
        raise ValidationError("unknown fields or unresolved evidence prevent quick confirmation")
    validate_query_review_task_response(task, response, reviewer_id=draft["reviewer_id"], require_confirmation=True)
    return response


def confirm_scope(task, proposal, draft, visible_unit_ids, displayed_content_sha256, *, explicit=False):
    """An explicit UI action only; load/prefill/save never invokes this operation."""
    if explicit is not True:
        raise ValidationError("explicit human confirmation action is required")
    _assert_confirmable(task, proposal, draft)
    projection = confirmation_projection(task, proposal, draft)
    if displayed_content_sha256 != projection["content_sha256"]:
        raise ValidationError("displayed content changed; inspect and confirm again")
    all_ids = {u["id"] for u in projection["units"]}
    if not isinstance(visible_unit_ids, list) or not visible_unit_ids or any(not isinstance(x, str) for x in visible_unit_ids) or len(set(visible_unit_ids)) != len(visible_unit_ids) or not set(visible_unit_ids) <= all_ids:
        raise ValidationError("confirmation coverage must be exact visible units")
    result = copy.deepcopy(draft)
    receipt = {"action": "explicit_confirm", "content_sha256": displayed_content_sha256, "covered_units": sorted(visible_unit_ids), "reviewer_id": draft["reviewer_id"], "revision": draft["revision"]}
    if receipt not in result["receipts"]:
        result["receipts"].append(receipt)
    covered = _receipt_coverage(result, projection)
    result["status"] = "submitted" if covered == all_ids else "draft"
    return result


def _receipt_coverage(draft, projection):
    covered = set()
    known = {u["id"] for u in projection["units"]}
    for receipt in draft["receipts"]:
        if not isinstance(receipt, Mapping) or set(receipt) != {"action", "content_sha256", "covered_units", "reviewer_id", "revision"}:
            raise ValidationError("malformed confirmation receipt")
        if receipt["action"] != "explicit_confirm" or receipt["content_sha256"] != projection["content_sha256"] or receipt["reviewer_id"] != draft["reviewer_id"] or type(receipt["revision"]) is not int or receipt["revision"] != draft["revision"]:
            raise ValidationError("stale or foreign confirmation receipt")
        units = receipt["covered_units"]
        if not isinstance(units, list) or not units or any(not isinstance(x, str) for x in units) or len(set(units)) != len(units) or not set(units) <= known:
            raise ValidationError("invalid confirmation receipt coverage")
        covered.update(units)
    return covered


def compile_confirmed(task, proposal, draft, *, reviewer_id):
    if draft.get("reviewer_id") != reviewer_id:
        raise ValidationError("submission reviewer differs from the trusted assignment")
    response = _assert_confirmable(task, proposal, draft)
    projection = confirmation_projection(task, proposal, draft)
    if draft["status"] != "submitted" or _receipt_coverage(draft, projection) != {u["id"] for u in projection["units"]}:
        raise ValidationError("all visible semantic units require explicit confirmation")
    return response
