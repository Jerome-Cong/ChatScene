"""Maintainer commands for offline review packets."""
import json
from pathlib import Path
from .jsonio import read_json, write_json
from .errors import ValidationError
from .paths import review_state_path
from .review_packet import export_packet, import_packet, finalize_packet, validate_browser_submission


def _run(args):
    if args.review_action == "export":
        result = export_packet(args.library, args.oracle, args.reviewer, args.output, with_suggestions=args.suggestions)
    elif args.review_action == "propose":
        from .human_workflow import export_query_review_bundle
        from .review_proposals import build_revision_proposals, proposal_coverage
        tasks = export_query_review_bundle(args.library, args.oracle, reviewer_id="machine-only-proposal-preparation")["reviewer_packet"]["tasks"]
        agent = read_json(args.agent_review) if args.agent_review else None
        records = {r["query_id"]: r for r in agent["reviews"]} if agent else {}
        if agent and (agent.get("human_gold") is not False or len(records) != len(agent["reviews"]) or any(t["subject_id"] not in records for t in tasks)):
            raise ValidationError("legacy Agent draft must be non-gold and cover every source subject")
        reports = [build_revision_proposals(t, records.get(t["subject_id"])) for t in tasks]
        output = review_state_path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        try:
            output.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise ValidationError("proposal output exists; choose a new directory") from exc
        write_json(output / "proposals.json", {"human_gold": False, "reports": reports})
        result = proposal_coverage(reports)
        write_json(output / "coverage.json", result)
    elif args.review_action == "validate":
        value = validate_browser_submission(read_json(args.assignment), read_json(args.submission), args.library, args.oracle)
        result = {k: value[k] for k in ("subjects_total", "subjects_submitted", "human_gold")}
    else:
        function = import_packet if args.review_action == "import" else finalize_packet
        result = function(args.assignment, args.submission, args.library, args.oracle, args.output)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


def add_review_parser(subparsers):
    parser = subparsers.add_parser("review", help="export, import and validate offline human review")
    actions = parser.add_subparsers(dest="review_action", required=True)
    for name in ("export", "propose", "validate", "import", "finalize"):
        command = actions.add_parser(name)
        command.add_argument("--library", type=Path, required=True)
        command.add_argument("--oracle", type=Path, required=True)
        if name == "export":
            command.add_argument("--reviewer", required=True)
            command.add_argument("--suggestions", action="store_true", help="include offline literal proposals and unresolved issues")
        elif name == "propose":
            command.add_argument("--agent-review", type=Path)
        else:
            command.add_argument("--assignment", type=Path, required=True)
            command.add_argument("--submission", type=Path, required=True)
        if name != "validate":
            command.add_argument("--output", type=Path, required=True)
        command.set_defaults(function=_run)
