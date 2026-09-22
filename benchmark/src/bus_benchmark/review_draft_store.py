"""Atomic compare-and-swap persistence for versioned non-gold review drafts."""

from pathlib import Path
from .errors import ValidationError
from .jsonio import read_json, write_json
from .paths import review_state_path
from .review_model import compile_confirmed, content_hash, validate_draft, validate_proposal
from .review_store import _checkpoint_guard


def publish_proposal(path, task, proposal):
    """Publish a machine proposal independently; never overwrite another one."""
    validate_proposal(task, proposal)
    path = review_state_path(path)
    with _checkpoint_guard(path):
        if path.exists():
            if content_hash(read_json(path)) != content_hash(proposal):
                raise ValidationError("proposal already exists with different contents")
        else:
            write_json(path, proposal)
    return proposal["proposal_id"]


class ReviewDraftStore:
    def __init__(self, path):
        self.path = review_state_path(Path(path))

    def load(self, task, proposal, reviewer_id):
        value = read_json(self.path)
        validate_draft(task, proposal, value)
        if value["reviewer_id"] != reviewer_id:
            raise ValidationError("draft belongs to another reviewer")
        if value["status"] == "submitted":
            compile_confirmed(task, proposal, value, reviewer_id=reviewer_id)
        return value, content_hash(value)

    def save(self, task, proposal, draft, expected_sha256=None):
        validate_draft(task, proposal, draft)
        if draft["status"] == "submitted":
            compile_confirmed(task, proposal, draft, reviewer_id=draft["reviewer_id"])
        with _checkpoint_guard(self.path):
            if self.path.exists():
                current, digest = self.load(task, proposal, draft["reviewer_id"])
                if expected_sha256 != digest:
                    raise ValidationError("draft changed in another session; reload before saving")
            elif expected_sha256 is not None:
                raise ValidationError("draft disappeared; refusing blind replacement")
            write_json(self.path, draft)
        return content_hash(draft)
