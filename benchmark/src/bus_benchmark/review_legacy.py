"""Compatibility adapter for the historical checkout assignment manifest."""
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Mapping, Optional
from .agent_query_review import (
    DEFAULT_AGENT_REVIEW_FRAGMENT_PATHS,
    validate_agent_query_review_draft,
)
from .errors import ValidationError
from .human_workflow import export_query_review_bundle, validate_human_document
from .jsonio import canonical_json_bytes, read_json, sha256_file
from .paths import review_state_path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


HUMAN_REVIEW_ROOT = REPOSITORY_ROOT / "benchmark_artifacts" / "human_review"


LEGACY_ASSIGNMENT_DIR = HUMAN_REVIEW_ROOT / "workbench_assignment"


DEFAULT_ASSIGNMENT_DIR = HUMAN_REVIEW_ROOT / "workbench_assignment_v0_2"


AGENT_REVIEW_PATH = (
    REPOSITORY_ROOT
    / "benchmark_artifacts"
    / "drafts"
    / "query_agent_semantic_review_v0_2.json"
)


ASSIGNMENT_EXPORTER = REPOSITORY_ROOT / "scripts" / "export_human_review_drafts.py"


SPLIT_SOURCES = {
    "development": {
        "packet_name": "dev_query_review",
        "library": REPOSITORY_ROOT
        / "query_lib"
        / "bus_ego_topdown_2d_dev_query_library_v0_2.jsonl",
        "oracle": REPOSITORY_ROOT
        / "benchmark_artifacts"
        / "drafts"
        / "dev_oracle_draft.jsonl",
        "expected_subjects": 48,
    },
    "test": {
        "packet_name": "test_query_review",
        "library": REPOSITORY_ROOT
        / "query_lib"
        / "bus_ego_topdown_2d_query_library_v0_2.jsonl",
        "oracle": REPOSITORY_ROOT
        / "benchmark_artifacts"
        / "drafts"
        / "test_oracle_draft.jsonl",
        "expected_subjects": 252,
    },
}


def _artifact_matches(binding: Mapping[str, Any], expected_parent: Optional[Path] = None) -> Path:
    path = Path(binding.get("path", "")).resolve()
    if expected_parent is not None and path.parent != expected_parent.resolve():
        raise ValidationError("assignment artifact escapes its workspace: {}".format(path))
    if not path.is_file():
        raise ValidationError("assignment artifact is missing: {}".format(path))
    if path.stat().st_size != binding.get("bytes") or sha256_file(path) != binding.get(
        "sha256"
    ):
        raise ValidationError("assignment artifact differs from its registry: {}".format(path))
    return path


def _live_agent_review() -> Dict[str, Any]:
    value = read_json(AGENT_REVIEW_PATH)
    query_sources = (
        (
            "development",
            SPLIT_SOURCES["development"]["library"],
            SPLIT_SOURCES["development"]["oracle"],
        ),
        (
            "test",
            SPLIT_SOURCES["test"]["library"],
            SPLIT_SOURCES["test"]["oracle"],
        ),
    )
    validate_agent_query_review_draft(
        value,
        query_sources=query_sources,
        fragment_paths=DEFAULT_AGENT_REVIEW_FRAGMENT_PATHS,
    )
    return value


def ensure_review_assignment(
    reviewer_id: str,
    assignment_dir: Path,
    *,
    python_executable: Optional[str] = None,
) -> Dict[str, Any]:
    """Create or verify the canonical 300-query reviewer assignment."""

    reviewer_id = str(reviewer_id).strip()
    if not reviewer_id:
        raise ValidationError("reviewer ID 不能为空")
    assignment_dir = review_state_path(assignment_dir)
    if assignment_dir == REPOSITORY_ROOT:
        raise ValidationError("review assignment 不能直接写到仓库根目录")
    if assignment_dir == LEGACY_ASSIGNMENT_DIR.resolve():
        raise ValidationError(
            "workbench_assignment 是只读的 v0.1 历史目录；"
            "请使用 workbench_assignment_v0_2 或另一个全新目录"
        )
    registry_path = assignment_dir / "machine_draft_registry.json"
    if not registry_path.exists():
        if not ASSIGNMENT_EXPORTER.is_file():
            raise ValidationError("legacy assignment exporter is unavailable; use ReviewContext with explicit sources and workspace")
        if assignment_dir.exists() and any(assignment_dir.iterdir()):
            raise ValidationError(
                "目标目录非空且没有 machine_draft_registry.json；请使用新目录"
            )
        assignment_dir.mkdir(parents=True, exist_ok=True)
        command = [
            python_executable or sys.executable,
            str(ASSIGNMENT_EXPORTER),
            "--reviewer",
            reviewer_id,
            "--output-dir",
            str(assignment_dir),
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=str(REPOSITORY_ROOT),
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValidationError("无法创建 canonical review assignment") from exc
        if not registry_path.is_file():
            raise ValidationError(
                "assignment exporter 未生成 registry: {}".format(completed.stderr)
            )

    registry = read_json(registry_path)
    validate_human_document(registry, "machine_draft_registry")
    assignment = registry.get("reviewer_assignment", {})
    if assignment.get("reviewer_id") != reviewer_id:
        raise ValidationError("workspace 已绑定另一个 reviewer ID")
    if assignment.get("identity_proof_provided") is not False:
        raise ValidationError("workbench 不得声称 reviewer identity 已认证")
    if registry.get("review_policy") != "one_complete_human_gold_per_subject":
        raise ValidationError("assignment 不符合 single-gold policy")
    if registry.get("human_gold", {}).get("admissible_records_created_by_export") != 0:
        raise ValidationError("assignment exporter 不得创建 human gold")

    agent_binding = registry.get("query_agent_semantic_review", {}).get("artifact", {})
    agent_path = _artifact_matches(agent_binding)
    if agent_path != AGENT_REVIEW_PATH.resolve():
        raise ValidationError("assignment 绑定了非 canonical Agent semantic draft")
    _live_agent_review()

    packet_by_name = {item["packet_name"]: item for item in registry["packets"]}
    for split, spec in SPLIT_SOURCES.items():
        packet_entry = packet_by_name.get(spec["packet_name"])
        if not packet_entry or packet_entry.get("status") != "packet_ready":
            raise ValidationError("{} query packet is not ready".format(split))
        bundle_path = _artifact_matches(packet_entry["bundle"], assignment_dir)
        _artifact_matches(packet_entry["submission_template"], assignment_dir)
        bundle = read_json(bundle_path)
        expected = export_query_review_bundle(
            spec["library"], spec["oracle"], reviewer_id=reviewer_id
        )
        if canonical_json_bytes(bundle) != canonical_json_bytes(expected):
            raise ValidationError("{} bundle differs from current sources".format(split))
        if len(bundle["reviewer_packet"]["tasks"]) != spec["expected_subjects"]:
            raise ValidationError("{} bundle has an unexpected subject count".format(split))
    return registry


def session_from_assignment(cls, assignment_dir: Path, split: str):
    assignment_dir = Path(assignment_dir).resolve()
    registry = read_json(assignment_dir / "machine_draft_registry.json")
    validate_human_document(registry, "machine_draft_registry")
    reviewer_id = registry["reviewer_assignment"]["reviewer_id"]
    ensure_review_assignment(reviewer_id, assignment_dir)
    spec = SPLIT_SOURCES[split]
    packet_entry = next(
        item
        for item in registry["packets"]
        if item["packet_name"] == spec["packet_name"]
    )
    bundle = read_json(Path(packet_entry["bundle"]["path"]))
    agent_value = _live_agent_review()
    agent_reviews = {item["query_id"]: item for item in agent_value["reviews"]}
    return cls(
        bundle,
        library_source=spec["library"],
        oracle_source=spec["oracle"],
        split=split,
        checkpoint_path=assignment_dir
        / "{}_query_review_workbench_checkpoint.json".format(
            "dev" if split == "development" else "test"
        ),
        agent_reviews=agent_reviews,
        agent_artifact_id=agent_value["artifact_id"],
    )
