"""Offline review packets and authoritative source-rebuilding imports."""

import copy
import json
from pathlib import Path

from .errors import ValidationError
from .human_workflow import export_query_review_bundle, finalize_query_review_bundle, validate_query_review_submission
from .jsonio import read_json, canonical_json_bytes, write_json, write_jsonl, sha256_file
from .paths import asset_path, review_state_path
from .review_model import make_proposal, new_draft, validate_draft, confirmation_projection, confirm_scope, compile_confirmed
from .review_registry import FIELD_DEFINITIONS, PREDICATE_DEFINITIONS, TOKEN_TRANSLATIONS
from .review_vocabulary import TOKEN_LABELS
from .review_wire import wire_hash

PACKET_VERSION = "1"
UI_VERSION = "1"


def build_packet(library_source, oracle_source, reviewer_id):
    bundle = export_query_review_bundle(library_source, oracle_source, reviewer_id=reviewer_id)
    items = []
    for task in bundle["reviewer_packet"]["tasks"]:
        proposal = make_proposal(task)
        items.append({"task": task, "proposal": proposal, "initial_draft": new_draft(task, proposal, reviewer_id)})
    core = {
        "packet_version": PACKET_VERSION, "ui_version": UI_VERSION,
        "reviewer_id": reviewer_id, "human_gold": False,
        "source_binding": bundle["reviewer_packet"]["source_binding"],
        "bundle_sha256": wire_hash(bundle), "items": items,
        "dictionary": {"fields": FIELD_DEFINITIONS, "predicates": PREDICATE_DEFINITIONS, "tokens": {**TOKEN_LABELS, **TOKEN_TRANSLATIONS}},
        "guide": asset_path("review", "guide_zh.md").read_text(encoding="utf-8"),
        "practice": read_json(asset_path("review", "practice.json")),
        "ui_sha256": {name: sha256_file(asset_path("review", name)) for name in ("app.js", "app.css", "template.html")},
    }
    # JSON makes tuples transport lists before the typed wire hash.
    core = json.loads(canonical_json_bytes(core))
    return {**core, "packet_id": wire_hash(core)}


def browser_snapshot(packet_id, task, proposal, draft):
    return {"packet_id": packet_id, "task": task, "proposal": proposal, "draft_content": {k: v for k, v in draft.items() if k not in ("receipts", "status", "human_gold")}}


def export_packet(library_source, oracle_source, reviewer_id, output_dir):
    packet = build_packet(library_source, oracle_source, reviewer_id)
    output = review_state_path(output_dir)
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        output.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise ValidationError("packet output already exists; choose a new directory") from exc
    write_json(output / "assignment.json", packet)
    # Never interpolate raw query text into executable markup.
    embedded = json.dumps(packet, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    html = asset_path("review", "template.html").read_text(encoding="utf-8")
    html = html.replace("@@STYLE@@", asset_path("review", "app.css").read_text(encoding="utf-8"))
    html = html.replace("@@SCRIPT@@", asset_path("review", "app.js").read_text(encoding="utf-8"))
    html = html.replace("@@PACKET@@", embedded)
    (output / "review.html").write_text(html, encoding="utf-8")
    write_json(output / "export_receipt.json", {"packet_id": packet["packet_id"], "human_gold": False, "html_sha256": sha256_file(output / "review.html")})
    return {"packet_id": packet["packet_id"], "html": str(output / "review.html"), "assignment": str(output / "assignment.json"), "human_gold": False}


def validate_browser_submission(assignment, submission, library_source, oracle_source):
    """Rebuild trusted task sources; browser hashes alone are never trusted."""
    if not isinstance(assignment, dict) or not isinstance(assignment.get("reviewer_id"), str):
        raise ValidationError("trusted assignment is malformed")
    expected = build_packet(library_source, oracle_source, assignment["reviewer_id"])
    if canonical_json_bytes(assignment) != canonical_json_bytes(expected):
        raise ValidationError("assignment differs from trusted current sources, guide or UI")
    keys = {"artifact_type", "packet_version", "packet_id", "reviewer_id", "generation", "entries", "human_gold", "backup_sha256"}
    if not isinstance(submission, dict) or set(submission) != keys or submission["artifact_type"] != "browser_review_backup" or submission["human_gold"] is not False:
        raise ValidationError("malformed browser backup; it must not claim gold")
    if submission["packet_version"] != PACKET_VERSION or submission["packet_id"] != expected["packet_id"] or submission["reviewer_id"] != expected["reviewer_id"]:
        raise ValidationError("backup belongs to another packet, version or reviewer")
    if type(submission["generation"]) is not int or submission["generation"] < 0:
        raise ValidationError("invalid backup generation")
    if submission["backup_sha256"] != wire_hash({k: v for k, v in submission.items() if k != "backup_sha256"}):
        raise ValidationError("backup integrity check failed")
    entries = submission["entries"]
    if not isinstance(entries, list) or len(entries) != len(expected["items"]):
        raise ValidationError("backup must retain every assigned subject, including deferred ones")
    drafts, responses = [], []
    for item, incoming in zip(expected["items"], entries):
        task, proposal = item["task"], item["proposal"]
        validate_draft(task, proposal, incoming)
        if incoming["reviewer_id"] != expected["reviewer_id"]:
            raise ValidationError("subject reviewer differs from the assignment")
        draft = copy.deepcopy(incoming)
        draft["receipts"] = []
        draft["status"] = incoming["status"] if incoming["status"] in ("deferred", "requires_source_fix") else "draft"
        if incoming["receipts"]:
            expected_digest = wire_hash(browser_snapshot(expected["packet_id"], task, proposal, incoming))
            for receipt in incoming["receipts"]:
                receipt_keys = {"action", "content_sha256", "covered_units", "reviewer_id", "revision"}
                if not isinstance(receipt, dict) or set(receipt) != receipt_keys or receipt["action"] != "explicit_confirm" or receipt["content_sha256"] != expected_digest or receipt["reviewer_id"] != expected["reviewer_id"] or type(receipt["revision"]) is not int or receipt["revision"] != draft["revision"]:
                    raise ValidationError("browser receipt source/content/reviewer differs")
                projection = confirmation_projection(task, proposal, draft)
                draft = confirm_scope(task, proposal, draft, receipt["covered_units"], projection["content_sha256"], explicit=True)
        if incoming["status"] == "submitted":
            response = compile_confirmed(task, proposal, draft, reviewer_id=expected["reviewer_id"])
            responses.append({"task_id": task["task_id"], "subject_id": task["subject_id"], "subject_sha256": task["subject_sha256"], "response": response})
        elif draft["status"] == "submitted":
            raise ValidationError("backup status contradicts complete confirmation coverage")
        drafts.append(draft)
    return {"drafts": drafts, "responses": responses, "subjects_total": len(entries), "subjects_submitted": len(responses), "human_gold": False}


def import_packet(assignment_path, submission_path, library_source, oracle_source, output_dir):
    assignment, submission = read_json(assignment_path), read_json(submission_path)
    result = validate_browser_submission(assignment, submission, library_source, oracle_source)
    output = review_state_path(output_dir)
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        output.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise ValidationError("import output already exists; original files were retained") from exc
    write_json(output / "browser_backup.json", submission)
    write_json(output / "validated_review.json", result)
    return {"output": str(output), "subjects_total": result["subjects_total"], "subjects_submitted": result["subjects_submitted"], "human_gold": False}


def finalize_packet(assignment_path, submission_path, library_source, oracle_source, output_dir):
    assignment = read_json(assignment_path)
    imported = validate_browser_submission(assignment, read_json(submission_path), library_source, oracle_source)
    if imported["subjects_submitted"] != imported["subjects_total"]:
        raise ValidationError("all subjects must be explicitly submitted; deferred subjects cannot become gold")
    bundle = export_query_review_bundle(library_source, oracle_source, reviewer_id=assignment["reviewer_id"])
    submission = copy.deepcopy(bundle["reviewer_packet"]["submission_template"])
    submission["responses"] = imported["responses"]
    submission["submission_status"] = "complete"
    validate_query_review_submission(bundle["reviewer_packet"], submission, library_source=library_source, oracle_source=oracle_source)
    result = finalize_query_review_bundle(bundle, submission, library_source=library_source, oracle_source=oracle_source)
    output = review_state_path(output_dir)
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        output.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise ValidationError("finalization output already exists; no overwrite") from exc
    write_json(output / "submission.json", submission)
    write_jsonl(output / "confirmed_oracle.jsonl", result["confirmed_oracles"])
    write_jsonl(output / "human_query_gold.jsonl", result["human_gold_records"])
    receipt = {"packet_id": assignment["packet_id"], "record_count": len(result["human_gold_records"]), "external_human_custody_required": True}
    write_json(output / "finalization_receipt.json", receipt)
    return receipt
