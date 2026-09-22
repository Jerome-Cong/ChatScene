"""Explicit source and workspace context for any query-review suite."""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .errors import ValidationError
from .human_workflow import export_query_review_bundle
from .paths import review_workspace
from .review_session import QueryReviewSession


@dataclass(frozen=True)
class ReviewContext:
    library_source: Path
    oracle_source: Path
    workspace: Path
    reviewer_id: str
    split: str = "development"
    expected_subjects: Optional[int] = None

    def open_session(self, checkpoint_name: str = "review_checkpoint.json") -> QueryReviewSession:
        workspace = review_workspace(self.workspace)
        if not isinstance(self.reviewer_id, str) or not self.reviewer_id.strip():
            raise ValidationError("reviewer ID must be nonempty")
        if not isinstance(checkpoint_name, str) or not checkpoint_name or Path(checkpoint_name).name != checkpoint_name or checkpoint_name in (".", ".."):
            raise ValidationError("checkpoint_name must be a filename inside the workspace")
        checkpoint = workspace / checkpoint_name
        if checkpoint.resolve().parent != workspace or checkpoint.is_symlink():
            raise ValidationError("checkpoint must not escape the review workspace")
        sources = []
        for label, raw in (("library", self.library_source), ("oracle", self.oracle_source)):
            if raw is None or not str(raw).strip():
                raise ValidationError("{} source must be explicit".format(label))
            path = Path(raw).expanduser().resolve()
            if not path.is_file():
                raise ValidationError("{} source is missing or is not a file: {}".format(label, path))
            sources.append(path)
        bundle = export_query_review_bundle(*sources, reviewer_id=self.reviewer_id)
        if self.expected_subjects is not None:
            if isinstance(self.expected_subjects, bool) or not isinstance(self.expected_subjects, int) or self.expected_subjects < 1:
                raise ValidationError("expected_subjects must be a positive manifest count")
            if len(bundle["reviewer_packet"]["tasks"]) != self.expected_subjects:
                raise ValidationError("review suite subject count differs from the caller manifest")
        return QueryReviewSession(bundle, library_source=sources[0], oracle_source=sources[1], split=self.split, checkpoint_path=checkpoint)


def main(argv=None):
    """Prepare/resume a draft checkpoint from explicit sources, never gold."""
    import argparse
    import json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--oracle", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--reviewer", required=True)
    parser.add_argument("--split", default="development")
    parser.add_argument("--expected-subjects", type=int)
    args = parser.parse_args(argv)
    try:
        session = ReviewContext(args.library, args.oracle, args.workspace, args.reviewer, args.split, args.expected_subjects).open_session()
    except (ValidationError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps({"subjects": len(session.ordered_query_ids), "checkpoint": str(session.checkpoint_path), "human_gold": False}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
