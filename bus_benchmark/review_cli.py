"""Maintainer commands for offline review packets."""
import json
from pathlib import Path
from .jsonio import read_json
from .review_packet import export_packet, import_packet, finalize_packet, validate_browser_submission


def _run(args):
    if args.review_action == "export":
        result = export_packet(args.library, args.oracle, args.reviewer, args.output)
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
    for name in ("export", "validate", "import", "finalize"):
        command = actions.add_parser(name)
        command.add_argument("--library", type=Path, required=True)
        command.add_argument("--oracle", type=Path, required=True)
        if name == "export":
            command.add_argument("--reviewer", required=True)
        else:
            command.add_argument("--assignment", type=Path, required=True)
            command.add_argument("--submission", type=Path, required=True)
        if name != "validate":
            command.add_argument("--output", type=Path, required=True)
        command.set_defaults(function=_run)
