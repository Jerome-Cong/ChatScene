"""Command-line interface for reproducible benchmark preparation and scoring."""

import argparse
import datetime as _datetime
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence

from .audit import collect_draft_decisions, select_stratified_audit_sample
from .cpd import (
    compute_coverage,
    recompute_cpd_aggregate,
    validate_cpd_policy_routing,
)
from .constants import SCHEMA_VERSION
from .errors import BenchmarkError
from .freeze import create_freeze_manifest, verify_freeze_manifest
from .generation import (
    GenerationRunner,
    ensure_immutable_jsonl,
    validate_generation_response_chain,
)
from .generation_preflight import generation_preflight
from .jsonio import (
    canonical_json_bytes,
    read_json,
    read_jsonl,
    sha256_file,
    write_json,
    write_jsonl,
)
from .judge import parse_raw_judge_response
from .judge_pipeline import (
    build_judge_calibration_context_manifest,
    compute_judge_calibration_run_diagnostics,
    create_judge_calibration_request_bundle,
    create_judge_request_bundle,
    run_judge_calibration_request_bundle,
    run_judge_request_bundle,
    validate_judge_calibration_run_bundle,
    validate_judge_run_bundle,
)
from .library import audit_dev_test_overlap, find_internal_duplicate_candidates, validate_library
from .metrics import (
    aggregate_semantic_metrics,
    compute_rqs,
    compute_uqh,
    score_semantic_output,
    validate_repetition_grid,
)
from .method_result import (
    MethodResultPaths,
    publish_method_result,
    validate_method_result,
)
from .oracle import (
    build_review_tasks,
    cpd_dimension_names,
    draft_oracle,
    restrict_cpd_eligibility,
)
from .provenance import (
    frozen_asset_hash,
    metric_source_hash,
    record_sha256,
    validate_aggregate_response_chain,
    validate_evidence_response_chain,
    validate_frozen_judge_response_chain,
    validate_scores_against_evidence,
    validate_score_provenance,
    validate_score_judge_run_provenance,
)
from .roster import (
    build_roster,
    roster_entries,
    validate_records_against_roster,
)
from .runtime import (
    aggregate_runtime,
    execute_runtime_grid,
    validate_platform_runtime_config,
)
from .schema import validate_schema_instance, validate_schema_records
from .paths import asset_path, PACKAGE_ROOT
from .semantic_pipeline import build_frozen_semantic_evidence
from .stats import clustered_bootstrap_mean
from .uqh import create_signed_uqh_assessments, create_uqh_assessor_requests


def _emit(value: Any, output: str = None) -> None:
    if output:
        write_json(Path(output), value)
    else:
        print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def _utc_now() -> str:
    return _datetime.datetime.now(_datetime.timezone.utc).isoformat()


def _authorize_frozen_assets(
    args: argparse.Namespace, required_assets: Iterable[tuple]
) -> Any:
    """Require formal scoring inputs to be members of one verified freeze manifest."""

    if getattr(args, "development", False):
        return None
    manifest_path = getattr(args, "freeze_manifest", None)
    if not manifest_path:
        raise BenchmarkError("formal command requires --freeze-manifest")
    manifest = read_json(Path(manifest_path))
    verify_freeze_manifest(manifest, require_frozen=True)
    registered = {
        (asset.get("role"), str(Path(asset.get("path", "")).resolve()))
        for asset in manifest.get("assets", [])
    }
    missing = []
    for role, path in required_assets:
        key = (role, str(Path(path).resolve()))
        if key not in registered:
            missing.append({"role": role, "path": key[1]})
    if missing:
        raise BenchmarkError(
            "formal scoring input is not registered in the verified freeze: {}".format(
                missing
            )
        )
    return manifest


def _frozen_roster_method_config_sha256(
    manifest: Mapping[str, Any], roster: Mapping[str, Any]
) -> str:
    matches = []
    for asset in manifest.get("assets", []):
        if asset.get("role") != "method_config":
            continue
        method_config = read_json(Path(asset["path"]))
        if (
            method_config.get("method_id") == roster.get("method_id")
            and method_config.get("platform") == roster.get("platform")
        ):
            matches.append(asset)
    if len(matches) != 1:
        raise BenchmarkError(
            "formal UQH requires one frozen method config for the roster"
        )
    return matches[0]["sha256"]


_UQH_GENERATION_ARGUMENTS = (
    "generation_library",
    "generation_method_config",
    "generation_output_root",
    "generation_evidence",
    "generation_run_manifest",
)


def _uqh_generation_assets_and_validate_args(args: argparse.Namespace) -> List[tuple]:
    values = {name: getattr(args, name, None) for name in _UQH_GENERATION_ARGUMENTS}
    present = {name for name, value in values.items() if value}
    if present and len(present) != len(values):
        missing = sorted(set(values) - present)
        raise BenchmarkError(
            "UQH generation-chain arguments are incomplete; missing {}".format(missing)
        )
    if not getattr(args, "development", False) and len(present) != len(values):
        raise BenchmarkError(
            "formal UQH requires --generation-library, --generation-method-config, "
            "--generation-output-root, --generation-evidence, and "
            "--generation-run-manifest"
        )
    if not present:
        return []
    output_root = Path(values["generation_output_root"]).resolve()
    expected_paths = {
        "responses": output_root / "response.jsonl",
        "generation_evidence": output_root / "evidence.jsonl",
        "generation_run_manifest": output_root / "generation_run_manifest.json",
    }
    supplied_paths = {
        "responses": Path(args.responses).resolve(),
        "generation_evidence": Path(values["generation_evidence"]).resolve(),
        "generation_run_manifest": Path(values["generation_run_manifest"]).resolve(),
    }
    if any(supplied_paths[name] != path for name, path in expected_paths.items()):
        raise BenchmarkError(
            "formal UQH generation indexes must be the committed files under "
            "--generation-output-root"
        )
    return [
        ("query_library_test", values["generation_library"]),
        ("method_config", values["generation_method_config"]),
    ]


def _validate_uqh_generation_chain(
    args: argparse.Namespace,
    responses: Sequence[Mapping[str, Any]],
    roster: Mapping[str, Any],
) -> Any:
    if not getattr(args, "generation_output_root", None):
        if getattr(args, "development", False):
            return None
        raise BenchmarkError("formal UQH generation chain was not supplied")
    return validate_generation_response_chain(
        Path(args.generation_library),
        roster,
        Path(args.generation_method_config),
        Path(args.generation_output_root),
        responses=list(responses),
        evidence=read_jsonl(Path(args.generation_evidence)),
        manifest=read_json(Path(args.generation_run_manifest)),
        development=bool(args.development),
    )


_SCORING_GENERATION_ARGUMENTS = (
    "library",
    "method_config",
    "generation_dir",
)


def _scoring_generation_assets_and_validate_args(
    args: argparse.Namespace,
) -> List[tuple]:
    """Gate score/CPD on one committed generation chain."""

    label = str(getattr(args, "command", "scoring")).upper()
    values = {
        name: getattr(args, name, None) for name in _SCORING_GENERATION_ARGUMENTS
    }
    present = {name for name, value in values.items() if value}
    allow_unverified = bool(
        getattr(args, "allow_unverified_generation_chain", False)
    )
    development = bool(getattr(args, "development", False))
    if allow_unverified and not development:
        raise BenchmarkError(
            "--allow-unverified-generation-chain is development-only"
        )
    if present and len(present) != len(values):
        missing = sorted(set(values) - present)
        raise BenchmarkError(
            "{} generation-chain arguments are incomplete; missing {}".format(
                label, missing
            )
        )
    if not present:
        if not development:
            raise BenchmarkError(
                "formal {} requires --library, --method-config, and "
                "--generation-dir".format(label)
            )
        if not allow_unverified:
            raise BenchmarkError(
                "development {} without a committed generation chain requires "
                "--allow-unverified-generation-chain".format(label)
            )
        return []
    if allow_unverified:
        raise BenchmarkError(
            "--allow-unverified-generation-chain conflicts with supplied "
            "generation-chain inputs"
        )
    if not getattr(args, "responses", None):
        raise BenchmarkError(
            "{} generation-chain validation requires --responses".format(label)
        )
    if not development:
        committed_responses = Path(values["generation_dir"]).resolve() / "response.jsonl"
        if Path(args.responses).resolve() != committed_responses:
            raise BenchmarkError(
                "formal {} --responses must be the committed response.jsonl "
                "under --generation-dir".format(label)
            )
    return [
        ("query_library_test", values["library"]),
        ("method_config", values["method_config"]),
    ]


def _validate_scoring_generation_chain(
    args: argparse.Namespace,
    responses: Sequence[Mapping[str, Any]],
    roster: Mapping[str, Any],
) -> Dict[str, Any]:
    """Validate supplied score/CPD responses against committed generation bytes."""

    if not getattr(args, "generation_dir", None):
        print(
            "warning: development {} generation_chain_verified=false".format(
                str(getattr(args, "command", "scoring")).upper()
            ),
            file=sys.stderr,
        )
        return {
            "method_id": roster["method_id"],
            "platform": roster["platform"],
            "record_count": len(responses),
            "manifest_sha256": None,
            "implementation_bundle_sha256": None,
            "verified": False,
        }
    output_root = Path(args.generation_dir).resolve()
    result = validate_generation_response_chain(
        Path(args.library),
        roster,
        Path(args.method_config),
        output_root,
        responses=list(responses),
        evidence=read_jsonl(output_root / "evidence.jsonl"),
        manifest=read_json(output_root / "generation_run_manifest.json"),
        development=bool(getattr(args, "development", False)),
    )
    return dict(result, verified=True)


def _cmd_validate_library(args: argparse.Namespace) -> None:
    _emit(
        validate_library(
            Path(args.input),
            expected_rows=args.expected_rows,
            expected_groups=args.expected_groups,
            expected_supported_rows=args.expected_supported_rows,
        ),
        args.output,
    )


def _cmd_draft_oracle(args: argparse.Namespace) -> None:
    records = read_jsonl(Path(args.input))
    oracle = draft_oracle(records)
    if args.cpd_registry_oracle:
        registry_oracle = read_jsonl(Path(args.cpd_registry_oracle))
        oracle = restrict_cpd_eligibility(
            oracle, cpd_dimension_names(registry_oracle)
        )
    write_jsonl(Path(args.output), oracle)
    if args.review_tasks:
        write_jsonl(Path(args.review_tasks), build_review_tasks(oracle))
    if args.coverage:
        write_json(
            Path(args.coverage),
            compute_coverage(oracle, allow_draft=True, expected_per_style=None),
        )
    if args.decisions:
        write_json(
            Path(args.decisions),
            {
                "decision_status": "draft",
                "source": str(Path(args.input).resolve()),
                "draft_decision_count": len(collect_draft_decisions(oracle)),
                "decisions": collect_draft_decisions(oracle),
            },
        )
    _emit({"records": len(oracle), "output": str(Path(args.output).resolve())})


def _cmd_overlap(args: argparse.Namespace) -> None:
    report = audit_dev_test_overlap(Path(args.dev), Path(args.test))
    report["test_internal_duplicate_candidates"] = find_internal_duplicate_candidates(
        Path(args.test), threshold=args.duplicate_threshold
    )
    _emit(report, args.output)


def _cmd_build_roster(args: argparse.Namespace) -> None:
    records = read_jsonl(Path(args.library))
    roster = build_roster(
        records,
        Path(args.library),
        args.method_id,
        args.platform,
        read_json(Path(args.statistical_clusters)),
        allow_draft=args.allow_draft,
    )
    _emit(roster, args.output)


def _cmd_generate(args: argparse.Namespace) -> None:
    """Run one complete query-by-five method grid and freeze its JSONL indexes."""

    output_directory = Path(args.output_dir).resolve()
    response_path = output_directory / "response.jsonl"
    evidence_path = output_directory / "evidence.jsonl"
    if response_path == evidence_path:
        raise BenchmarkError("generation response and evidence paths must differ")
    if not args.resume and (response_path.exists() or evidence_path.exists()):
        raise BenchmarkError(
            "generation indexes already exist; use --resume to verify without overwriting"
        )

    _authorize_frozen_assets(
        args,
        (
            ("query_library_test", args.library),
            ("query_roster", args.roster),
            ("method_config", args.method_config),
        ),
    )

    roster = read_json(Path(args.roster))
    validate_schema_instance(roster, "query_roster", context="generation roster")
    runner = GenerationRunner(
        Path(args.library),
        roster,
        Path(args.method_config),
        output_directory,
        development=args.development,
        resume=args.resume,
    )
    responses = runner.run()
    validate_schema_records(
        responses, "response_record", context="generation response"
    )
    validate_records_against_roster(
        responses, roster, require_confirmed=not args.development
    )
    evidence = runner.evidence_records(responses)
    validate_records_against_roster(
        evidence, roster, require_confirmed=not args.development
    )

    response_created = ensure_immutable_jsonl(response_path, responses)
    evidence_created = ensure_immutable_jsonl(evidence_path, evidence)
    _emit(
        {
            "query_count": roster["query_count"],
            "record_count": len(responses),
            "development": bool(args.development),
            "resume": bool(args.resume),
            "response_index_created": response_created,
            "evidence_index_created": evidence_created,
            "responses": str(response_path),
            "evidence": str(evidence_path),
        },
        args.summary,
    )


def _cmd_generation_preflight(args: argparse.Namespace) -> None:
    """Report readiness without invoking a method or consuming repetitions."""

    roster = read_json(Path(args.roster))
    _emit(
        generation_preflight(
            Path(args.library), roster, Path(args.method_config)
        ),
        args.output,
    )


def _runtime_inputs(args: argparse.Namespace):
    roster = read_json(Path(args.roster))
    validate_schema_instance(roster, "query_roster", context="runtime roster")
    responses = read_jsonl(Path(args.responses))
    validate_schema_records(
        responses, "response_record", context="runtime generation response"
    )
    validate_records_against_roster(
        responses, roster, require_confirmed=not args.development
    )
    platform = roster["platform"]
    platform_config = read_json(Path(args.platform_config))
    worker_config = validate_platform_runtime_config(
        platform_config, platform, allow_draft=args.development
    )
    controller_config = read_json(Path(args.controller_config))
    validate_schema_instance(
        controller_config, "controller_config", context="runtime controller config"
    )
    if (
        Path(controller_config["implementation"]["source_path"]).resolve()
        != Path(worker_config["worker"]).resolve()
        or controller_config["implementation"]["source_sha256"]
        != platform_config["worker"]["worker_sha256"]
    ):
        raise BenchmarkError("runtime controller and platform worker sources differ")
    chain_inputs = (
        getattr(args, "library", None),
        getattr(args, "method_config", None),
        getattr(args, "generation_dir", None),
    )
    if any(chain_inputs) and not all(chain_inputs):
        raise BenchmarkError(
            "runtime generation binding requires --library, --method-config, and --generation-dir together"
        )
    if all(chain_inputs):
        generation_chain = dict(
            validate_generation_response_chain(
                Path(args.library),
                roster,
                Path(args.method_config),
                Path(args.generation_dir),
                responses=responses,
                development=args.development,
            ),
            verified=True,
        )
    elif args.development:
        implementation_hashes = {
            response.get("implementation_bundle_sha256") for response in responses
        }
        if len(implementation_hashes) != 1 or None in implementation_hashes:
            raise BenchmarkError(
                "development runtime responses mix implementation bundles"
            )
        generation_chain = {
            "method_id": roster["method_id"],
            "platform": platform,
            "record_count": len(responses),
            "manifest_sha256": sha256_file(Path(args.responses)),
            "implementation_bundle_sha256": next(iter(implementation_hashes)),
            "verified": False,
        }
    else:
        raise BenchmarkError(
            "formal runtime requires the committed generation chain inputs"
        )
    frozen_assets = [
        ("query_roster", args.roster),
        ("platform_config_{}".format(platform), args.platform_config),
        ("controller_config_{}".format(platform), args.controller_config),
    ]
    if all(chain_inputs):
        frozen_assets.extend(
            [
                ("query_library_test", args.library),
                ("method_config", args.method_config),
            ]
        )
    _authorize_frozen_assets(args, frozen_assets)
    return (
        roster,
        responses,
        platform_config,
        worker_config,
        controller_config,
        generation_chain,
    )


def _cmd_runtime_execute(args: argparse.Namespace) -> None:
    roster, responses, platform_config, worker_config, controller_config, generation_chain = (
        _runtime_inputs(args)
    )
    records = execute_runtime_grid(
        responses,
        Path(args.output_dir),
        {roster["platform"]: worker_config},
        {roster["platform"]: controller_config},
        generation_chain,
        resume=args.resume,
        allow_draft_controller=args.development,
        allow_draft_runtime=args.development,
    )
    # Re-probe every formal dependency after the complete grid so source/package/
    # simulator drift during a long run cannot leave an admissible terminal index.
    final_platform_config = read_json(Path(args.platform_config))
    if canonical_json_bytes(final_platform_config) != canonical_json_bytes(platform_config):
        raise BenchmarkError("platform runtime config changed during execution")
    validate_platform_runtime_config(
        final_platform_config,
        roster["platform"],
        allow_draft=args.development,
    )
    validate_schema_records(records, "runtime_record", context="runtime record")
    created = ensure_immutable_jsonl(Path(args.output), records)
    _emit(
        {
            "record_count": len(records),
            "platform": roster["platform"],
            "resume": bool(args.resume),
            "runtime_index_created": created,
            "runtime_records": str(Path(args.output).resolve()),
        },
        args.summary,
    )


def _cmd_runtime_aggregate(args: argparse.Namespace) -> None:
    (
        roster,
        responses,
        platform_config,
        worker_config,
        controller_config,
        generation_chain,
    ) = _runtime_inputs(args)
    if args.allow_partial and not args.development:
        raise BenchmarkError("--allow-partial is development-only")
    records = read_jsonl(Path(args.records))
    validate_schema_records(records, "runtime_record", context="runtime record")
    expected_worker_hash = record_sha256(worker_config)
    expected_controller_hash = record_sha256(controller_config)
    for record in records:
        if (
            record.get("provenance", {}).get("worker_config_sha256")
            != expected_worker_hash
            or record.get("provenance", {}).get("worker_sha256")
            != platform_config["worker"]["worker_sha256"]
            or record.get("provenance", {}).get("interpreter_sha256")
            != platform_config["worker"]["interpreter_sha256"]
            or record.get("ne", {}).get("controller_config_sha256")
            != expected_controller_hash
        ):
            raise BenchmarkError(
                "runtime record differs from the authorized platform/controller freeze"
            )
    result = aggregate_runtime(
        records,
        roster=roster,
        responses=responses,
        generation_chain=generation_chain,
        allow_partial=args.allow_partial,
        require_confirmed=not args.development,
    )
    _emit(result, args.output)


def _cmd_method_result(args: argparse.Namespace) -> None:
    result = publish_method_result(
        MethodResultPaths(
            freeze_manifest=Path(args.freeze_manifest),
            library=Path(args.library),
            oracle=Path(args.oracle),
            roster=Path(args.roster),
            method_config=Path(args.method_config),
            generation_dir=Path(args.generation_dir),
            responses=Path(args.responses),
            semantic_evidence=Path(args.semantic_evidence),
            semantic_scores=Path(args.semantic_scores),
            judge_run_dir=Path(args.judge_run_dir),
            semantic_aggregate=Path(args.semantic_aggregate),
            uqh_assessments=Path(args.uqh_assessments),
            uqh_assessor_registry=Path(args.uqh_assessor_registry),
            cpd_aggregate=Path(args.cpd_aggregate),
            cpd_coverage=Path(args.cpd_coverage),
            runtime_records=Path(args.runtime_records),
            runtime_aggregate=Path(args.runtime_aggregate),
            platform_config=Path(args.platform_config),
            controller_config=Path(args.controller_config),
        ),
        Path(args.output),
    )
    _emit(
        {
            "cell_id": result["cell_id"],
            "method_id": result["method_id"],
            "platform": result["platform"],
            "result_sha256": result["result_sha256"],
            "output": str(Path(args.output).resolve()),
        }
    )


def _cmd_method_result_verify(args: argparse.Namespace) -> None:
    result = read_json(Path(args.input))
    validate_method_result(result, verify_live_inputs=True)
    _emit(
        {
            "verified": True,
            "cell_id": result["cell_id"],
            "method_id": result["method_id"],
            "platform": result["platform"],
            "result_sha256": result["result_sha256"],
        }
    )


def _cmd_extract_evidence(args: argparse.Namespace) -> None:
    """Materialize formal semantic evidence from one committed generation chain."""

    required_assets = [("query_roster", args.roster)]
    required_assets.extend(_scoring_generation_assets_and_validate_args(args))
    manifest = _authorize_frozen_assets(args, required_assets)
    roster = read_json(Path(args.roster))
    validate_schema_instance(roster, "query_roster", context="query roster")
    responses = read_jsonl(Path(args.responses))
    validate_schema_records(
        responses, "response_record", context="generation response"
    )
    validate_records_against_roster(responses, roster, require_confirmed=True)
    generation_chain = _validate_scoring_generation_chain(args, responses, roster)

    evidence = build_frozen_semantic_evidence(responses, manifest)
    validate_records_against_roster(evidence, roster, require_confirmed=True)
    validate_evidence_response_chain(
        evidence, responses, manifest, roster["method_id"]
    )
    created = ensure_immutable_jsonl(Path(args.output), evidence)
    _emit(
        {
            "record_count": len(evidence),
            "output": str(Path(args.output).resolve()),
            "output_created": created,
            "generation_chain_verified": generation_chain["verified"],
            "generation_run_manifest_sha256": generation_chain["manifest_sha256"],
            "extractor_manifest_sha256": frozen_asset_hash(
                manifest, "semantic_extractor_manifest"
            ),
        }
    )


def _load_formal_judge_sources(args: argparse.Namespace):
    required_assets = [("query_roster", args.roster)]
    required_assets.extend(_scoring_generation_assets_and_validate_args(args))
    manifest = _authorize_frozen_assets(args, required_assets)
    roster = read_json(Path(args.roster))
    validate_schema_instance(roster, "query_roster", context="query roster")
    responses = read_jsonl(Path(args.responses))
    evidence = read_jsonl(Path(args.evidence))
    validate_schema_records(
        responses, "response_record", context="generation response"
    )
    validate_schema_records(evidence, "semantic_evidence", context="semantic evidence")
    validate_records_against_roster(responses, roster, require_confirmed=True)
    validate_records_against_roster(evidence, roster, require_confirmed=True)
    generation_chain = _validate_scoring_generation_chain(args, responses, roster)
    validate_evidence_response_chain(
        evidence, responses, manifest, roster["method_id"]
    )
    return manifest, responses, evidence, generation_chain


def _cmd_judge_request(args: argparse.Namespace) -> None:
    manifest, responses, evidence, generation_chain = _load_formal_judge_sources(args)
    request_manifest = create_judge_request_bundle(
        responses, evidence, manifest, Path(args.output_dir)
    )
    _emit(
        {
            "request_count": request_manifest["request_count"],
            "output_directory": str(Path(args.output_dir).resolve()),
            "request_manifest_sha256": request_manifest["manifest_sha256"],
            "generation_run_manifest_sha256": generation_chain["manifest_sha256"],
        }
    )


def _cmd_judge_run(args: argparse.Namespace) -> None:
    manifest, responses, evidence, generation_chain = _load_formal_judge_sources(args)
    run_manifest = run_judge_request_bundle(
        Path(args.request_dir),
        responses,
        evidence,
        manifest,
        Path(args.output_dir),
        resume=args.resume,
    )
    _emit(
        {
            "response_count": run_manifest["response_count"],
            "output_directory": str(Path(args.output_dir).resolve()),
            "judge_run_manifest_sha256": run_manifest["manifest_sha256"],
            "generation_run_manifest_sha256": generation_chain["manifest_sha256"],
        }
    )


def _cmd_judge_calibration_request(args: argparse.Namespace) -> None:
    request_manifest = create_judge_calibration_request_bundle(
        Path(args.context), Path(args.output_dir)
    )
    _emit(
        {
            "request_count": request_manifest["request_count"],
            "output_directory": str(Path(args.output_dir).resolve()),
            "request_manifest_sha256": request_manifest["manifest_sha256"],
            "calibration_context_sha256": request_manifest[
                "calibration_context_sha256"
            ],
        }
    )


def _cmd_judge_calibration_context(args: argparse.Namespace) -> None:
    output_path = Path(args.output).resolve()
    if output_path.exists() or output_path.is_symlink():
        raise BenchmarkError("immutable Judge calibration context already exists")
    sources = [
        {
            "platform": "carla",
            "source_config": Path(args.carla_source_config),
            "query_roster": Path(args.carla_roster),
            "response_records": Path(args.carla_responses),
            "evidence_records": Path(args.carla_evidence),
        }
    ]
    metadrive_values = (
        args.metadrive_source_config,
        args.metadrive_roster,
        args.metadrive_responses,
        args.metadrive_evidence,
    )
    if any(metadrive_values) and not all(metadrive_values):
        raise BenchmarkError("MetaDrive calibration source arguments are all-or-none")
    if all(metadrive_values):
        sources.append(
            {
                "platform": "metadrive",
                "source_config": Path(args.metadrive_source_config),
                "query_roster": Path(args.metadrive_roster),
                "response_records": Path(args.metadrive_responses),
                "evidence_records": Path(args.metadrive_evidence),
            }
        )
    if args.profile == "cross_platform_final" and len(sources) != 2:
        raise BenchmarkError("cross-platform final calibration requires both platforms")
    if args.profile == "carla_stage" and len(sources) != 1:
        raise BenchmarkError("CARLA-stage calibration cannot include MetaDrive")
    context = build_judge_calibration_context_manifest(
        query_library_path=Path(args.query_library),
        requirement_oracle_path=Path(args.oracle),
        calibration_roster_path=Path(args.roster),
        calibration_human_gold_path=Path(args.human_gold),
        prompt_path=Path(args.prompt),
        output_schema_path=Path(args.output_schema),
        runner_manifest_path=Path(args.runner_manifest),
        development_sources=sources,
        model_id=args.model_id,
        inference_config=read_json(Path(args.inference_config)),
        calibration_profile=args.profile,
    )
    write_json(output_path, context)
    _emit(
        {
            "calibration_profile": context["calibration_profile"],
            "formal_freeze_eligible": context["formal_freeze_eligible"],
            "calibration_context_sha256": context["manifest_sha256"],
            "output": str(output_path),
        }
    )


def _cmd_judge_calibration_run(args: argparse.Namespace) -> None:
    run_manifest = run_judge_calibration_request_bundle(
        Path(args.request_dir),
        Path(args.context),
        Path(args.output_dir),
        resume=args.resume,
    )
    validated_manifest, responses = validate_judge_calibration_run_bundle(
        Path(args.output_dir),
        Path(args.context),
        request_directory=Path(args.request_dir),
    )
    if validated_manifest["manifest_sha256"] != run_manifest["manifest_sha256"]:
        raise BenchmarkError("calibration run changed during CLI validation")
    diagnostics = compute_judge_calibration_run_diagnostics(
        Path(args.output_dir),
        Path(args.context),
        request_directory=Path(args.request_dir),
    )
    _emit(
        {
            "response_count": len(responses),
            "output_directory": str(Path(args.output_dir).resolve()),
            "judge_run_manifest_sha256": run_manifest["manifest_sha256"],
            "judge_responses_sha256": run_manifest["judge_responses_sha256"],
            "calibration_profile": diagnostics["calibration_profile"],
            "formal_freeze_eligible": diagnostics["formal_freeze_eligible"],
            "diagnostics": diagnostics,
        }
    )


def _development_judge_bindings(raw_records):
    validate_schema_records(raw_records, "judge_response_record", context="judge response")
    bindings = {}
    seen = set()
    for record in raw_records:
        pair = (record["run_id"], record["atom_id"])
        if pair in seen:
            raise BenchmarkError("development judge responses contain a duplicate")
        seen.add(pair)
        parsed = parse_raw_judge_response(record["raw_response"])
        bindings.setdefault(pair[0], {})[pair[1]] = {
            "verdict": parsed["verdict"],
            "judge_response_id": record["judge_response_id"],
            "judge_response_record_sha256": record_sha256(record),
        }
    return bindings


def _load_judge_bindings(args, response_records, evidence_records, manifest):
    if manifest is not None:
        if args.judge_responses:
            raise BenchmarkError(
                "formal evaluation accepts --judge-run-dir, not bare --judge-responses"
            )
        if not args.judge_run_dir:
            raise BenchmarkError("formal evaluation requires --judge-run-dir")
        run_manifest, raw_records = validate_judge_run_bundle(
            Path(args.judge_run_dir),
            response_records,
            evidence_records,
            manifest,
        )
        return (
            validate_frozen_judge_response_chain(
                raw_records, response_records, evidence_records, manifest
            ),
            {
                "judge_run_manifest_sha256": run_manifest["manifest_sha256"],
                "judge_responses_sha256": run_manifest["judge_responses_sha256"],
            },
        )
    if args.judge_run_dir:
        raise BenchmarkError("development evaluation does not accept a formal Judge run")
    if args.judge_responses:
        return (
            _development_judge_bindings(read_jsonl(Path(args.judge_responses))),
            {
                "judge_run_manifest_sha256": None,
                "judge_responses_sha256": sha256_file(Path(args.judge_responses)),
            },
        )
    return {}, {
        "judge_run_manifest_sha256": None,
        "judge_responses_sha256": None,
    }


def _cmd_score(args: argparse.Namespace) -> None:
    if args.allow_draft_oracle and not args.development:
        raise BenchmarkError("--allow-draft-oracle is development-only")
    required_assets = [
        ("requirement_oracle_test", args.oracle),
        ("query_roster", args.roster),
    ]
    required_assets.extend(_scoring_generation_assets_and_validate_args(args))
    manifest = _authorize_frozen_assets(
        args,
        required_assets,
    )
    roster = read_json(Path(args.roster))
    validate_schema_instance(roster, "query_roster", context="query roster")
    entries = roster_entries(roster)
    response_records = []
    responses_by_run = {}
    if args.responses:
        response_records = read_jsonl(Path(args.responses))
        validate_schema_records(
            response_records, "response_record", context="generation response"
        )
        validate_records_against_roster(
            response_records, roster, require_confirmed=not args.development
        )
        responses_by_run = {record["run_id"]: record for record in response_records}
    elif not args.development:
        raise BenchmarkError("formal scoring requires --responses for provenance")
    generation_chain = _validate_scoring_generation_chain(
        args, response_records, roster
    )
    oracle_records = {
        record["query_id"]: record for record in read_jsonl(Path(args.oracle))
    }
    evidence_records = read_jsonl(Path(args.evidence))
    validate_schema_records(
        evidence_records, "semantic_evidence", context="semantic evidence"
    )
    validate_records_against_roster(
        evidence_records, roster, require_confirmed=not args.development
    )
    if manifest is not None:
        validate_evidence_response_chain(
            evidence_records,
            response_records,
            manifest,
            roster["method_id"],
        )
    judge_bindings_by_run, judge_run_provenance = _load_judge_bindings(
        args, response_records, evidence_records, manifest
    )

    roster_hash = sha256_file(Path(args.roster))
    oracle_hash = sha256_file(Path(args.oracle))
    if manifest is not None:
        extractor_hash = frozen_asset_hash(manifest, "semantic_extractor_manifest")
        evaluator_hash = metric_source_hash(manifest)
        freeze_hash = manifest["manifest_sha256"]
    else:
        extractor_hash = None
        evaluator_hash = sha256_file(Path(__file__).with_name("metrics.py"))
        freeze_hash = None
    results = []
    for evidence in evidence_records:
        query_id = evidence.get("query_id")
        if query_id not in oracle_records:
            raise BenchmarkError("evidence references unknown query_id {}".format(query_id))
        frozen_query_oracle = oracle_records[query_id]
        query_oracle = dict(frozen_query_oracle)
        query_oracle["statistical_intent_cluster_id"] = entries[query_id][
            "statistical_intent_cluster_id"
        ]
        result = score_semantic_output(
            query_oracle,
            evidence,
            allow_draft_oracle=args.allow_draft_oracle,
            judge_verdicts=judge_bindings_by_run.get(evidence.get("run_id"), {}),
        )
        for field in ("run_id", "repetition", "method_id", "platform"):
            if field in evidence:
                result[field] = evidence[field]
        response = responses_by_run.get(evidence.get("run_id"))
        result["schema_version"] = SCHEMA_VERSION
        result["provenance"] = {
                "scorer_id": "bus_benchmark.metrics",
                "scorer_version": SCHEMA_VERSION,
                "created_at_utc": _utc_now(),
                "response_record_sha256": record_sha256(response)
                if response is not None
                else evidence.get("provenance", {}).get("source_response_sha256"),
                "evidence_record_sha256": record_sha256(evidence),
                "oracle_record_sha256": record_sha256(frozen_query_oracle),
                "roster_sha256": roster_hash,
                "oracle_sha256": oracle_hash,
                "extractor_manifest_sha256": extractor_hash
                or evidence.get("provenance", {}).get("extractor_config_sha256"),
                "evaluator_source_sha256": evaluator_hash,
                "freeze_manifest_sha256": freeze_hash,
                "judge_run_manifest_sha256": judge_run_provenance[
                    "judge_run_manifest_sha256"
                ],
                "judge_responses_sha256": judge_run_provenance[
                    "judge_responses_sha256"
                ],
            }
        results.append(result)
    validate_schema_records(results, "semantic_score", context="semantic score")
    if manifest is not None:
        output_created = ensure_immutable_jsonl(Path(args.output), results)
    else:
        write_jsonl(Path(args.output), results)
        output_created = True
    _emit(
        {
            "records": len(results),
            "output": str(Path(args.output).resolve()),
            "output_created": output_created,
            "generation_chain_verified": generation_chain["verified"],
            "generation_run_manifest_sha256": generation_chain["manifest_sha256"],
        }
    )


def _cmd_aggregate(args: argparse.Namespace) -> None:
    required_assets = [("query_roster", args.roster)]
    if args.oracle:
        required_assets.append(("requirement_oracle_test", args.oracle))
    elif not args.development:
        raise BenchmarkError("formal aggregation requires --oracle")
    if args.responses:
        if not args.uqh_assessments or not args.uqh_assessor_registry:
            raise BenchmarkError(
                "UQH aggregation requires --uqh-assessments and --uqh-assessor-registry"
            )
        required_assets.append(("uqh_assessor_registry", args.uqh_assessor_registry))
        required_assets.extend(_uqh_generation_assets_and_validate_args(args))
    if not args.development and (
        not args.evidence or not args.responses or not args.judge_run_dir
    ):
        raise BenchmarkError(
            "formal aggregation requires --evidence, --responses, and --judge-run-dir"
        )
    manifest = _authorize_frozen_assets(args, required_assets)
    scores = read_jsonl(Path(args.scores))
    validate_schema_records(scores, "semantic_score", context="semantic score")
    roster = read_json(Path(args.roster))
    validate_schema_instance(roster, "query_roster", context="query roster")
    grid = validate_records_against_roster(
        scores, roster, require_confirmed=not args.development
    )
    if manifest is not None:
        validate_score_provenance(scores, manifest, Path(args.roster), Path(args.oracle))
    evidence_records = []
    response_records = []
    generation_chain = None
    if args.evidence:
        evidence_records = read_jsonl(Path(args.evidence))
        validate_schema_records(
            evidence_records, "semantic_evidence", context="semantic evidence"
        )
        validate_records_against_roster(
            evidence_records, roster, require_confirmed=not args.development
        )
    if args.responses:
        response_records = read_jsonl(Path(args.responses))
        validate_schema_records(
            response_records, "response_record", context="generation response"
        )
        validate_records_against_roster(
            response_records, roster, require_confirmed=not args.development
        )
        generation_chain = _validate_uqh_generation_chain(
            args, response_records, roster
        )
    if manifest is not None:
        validate_evidence_response_chain(
            evidence_records,
            response_records,
            manifest,
            roster["method_id"],
        )
        judge_bindings_by_run, judge_run_provenance = _load_judge_bindings(
            args, response_records, evidence_records, manifest
        )
        validate_score_judge_run_provenance(scores, judge_run_provenance)
        validate_scores_against_evidence(
            scores,
            evidence_records,
            read_jsonl(Path(args.oracle)),
            roster,
            judge_verdicts_by_run=judge_bindings_by_run,
        )
        validate_aggregate_response_chain(response_records, scores)
    result = {
        "repetition_grid": grid,
        "semantic": aggregate_semantic_metrics(scores),
        "rqs": compute_rqs(scores),
    }
    if response_records:
        if not args.oracle:
            raise BenchmarkError("UQH aggregation requires --oracle")
        uqh_oracles = read_jsonl(Path(args.oracle))
        validate_schema_records(uqh_oracles, "oracle_record", context="UQH oracle")
        uqh_assessments = read_jsonl(Path(args.uqh_assessments))
        uqh_assessor_registry = read_json(Path(args.uqh_assessor_registry))
        expected_config_sha256 = (
            _frozen_roster_method_config_sha256(manifest, roster)
            if manifest is not None
            else None
        )
        result["uqh"] = compute_uqh(
            response_records,
            uqh_oracles,
            uqh_assessments,
            roster,
            uqh_assessor_registry,
            expected_config_sha256=expected_config_sha256,
        )
        result["uqh"]["provenance"]["freeze_manifest_sha256"] = (
            manifest["manifest_sha256"] if manifest is not None else None
        )
        result["uqh"]["provenance"]["generation_run_manifest_sha256"] = (
            generation_chain["manifest_sha256"]
            if generation_chain is not None
            else None
        )
    _emit(result, args.output)


def _cmd_uqh_request(args: argparse.Namespace) -> None:
    required_assets = [
        ("query_roster", args.roster),
        ("requirement_oracle_test", args.oracle),
        ("uqh_assessor_registry", args.uqh_assessor_registry),
    ]
    required_assets.extend(_uqh_generation_assets_and_validate_args(args))
    manifest = _authorize_frozen_assets(
        args,
        required_assets,
    )
    roster = read_json(Path(args.roster))
    responses = read_jsonl(Path(args.responses))
    oracles = read_jsonl(Path(args.oracle))
    registry = read_json(Path(args.uqh_assessor_registry))
    generation_chain = _validate_uqh_generation_chain(args, responses, roster)
    expected_config_sha256 = (
        _frozen_roster_method_config_sha256(manifest, roster)
        if manifest is not None
        else None
    )
    requests = create_uqh_assessor_requests(
        responses,
        oracles,
        roster,
        registry,
        args.assessor_id,
        Path(args.output_dir),
        expected_config_sha256=expected_config_sha256,
    )
    _emit(
        {
            "records": len(requests),
            "output_directory": str(Path(args.output_dir).resolve()),
            "request_index": str(
                (Path(args.output_dir).resolve() / "request_index.jsonl")
            ),
            "generation_run_manifest_sha256": (
                generation_chain["manifest_sha256"]
                if generation_chain is not None
                else None
            ),
        }
    )


def _cmd_uqh_attest(args: argparse.Namespace) -> None:
    required_assets = [
        ("query_roster", args.roster),
        ("requirement_oracle_test", args.oracle),
        ("uqh_assessor_registry", args.uqh_assessor_registry),
    ]
    required_assets.extend(_uqh_generation_assets_and_validate_args(args))
    manifest = _authorize_frozen_assets(
        args,
        required_assets,
    )
    roster = read_json(Path(args.roster))
    responses = read_jsonl(Path(args.responses))
    oracles = read_jsonl(Path(args.oracle))
    executions = read_jsonl(Path(args.assessor_executions))
    registry = read_json(Path(args.uqh_assessor_registry))
    generation_chain = _validate_uqh_generation_chain(args, responses, roster)
    expected_config_sha256 = (
        _frozen_roster_method_config_sha256(manifest, roster)
        if manifest is not None
        else None
    )
    assessments = create_signed_uqh_assessments(
        responses,
        oracles,
        executions,
        roster,
        registry,
        args.assessor_id,
        Path(args.private_key),
        Path(args.output_dir),
        expected_config_sha256=expected_config_sha256,
    )
    _emit(
        {
            "records": len(assessments),
            "output_directory": str(Path(args.output_dir).resolve()),
            "assessment_index": str(
                (Path(args.output_dir).resolve() / "assessment_index.jsonl")
            ),
            "private_key_persisted": False,
            "generation_run_manifest_sha256": (
                generation_chain["manifest_sha256"]
                if generation_chain is not None
                else None
            ),
        }
    )


def _cmd_cpd(args: argparse.Namespace) -> None:
    if args.allow_draft_policy and not args.development:
        raise BenchmarkError("--allow-draft-policy is development-only")
    required_assets = [
        ("requirement_oracle_test", args.oracle),
        ("query_roster", args.roster),
    ]
    required_assets.extend(_scoring_generation_assets_and_validate_args(args))
    if not args.development and not args.coverage:
        raise BenchmarkError("formal CPD requires --coverage")
    if args.coverage:
        required_assets.append(("cpd_policy", args.coverage))
    manifest = _authorize_frozen_assets(
        args,
        required_assets,
    )
    if not args.development and (not args.responses or not args.judge_run_dir):
        raise BenchmarkError(
            "formal CPD requires --responses and --judge-run-dir for provenance"
        )
    roster = read_json(Path(args.roster))
    validate_schema_instance(roster, "query_roster", context="query roster")
    response_records = []
    if args.responses:
        response_records = read_jsonl(Path(args.responses))
        validate_schema_records(
            response_records, "response_record", context="generation response"
        )
        validate_records_against_roster(
            response_records,
            roster,
            require_confirmed=not args.development,
        )
    generation_chain = _validate_scoring_generation_chain(
        args, response_records, roster
    )
    evidence_records = read_jsonl(Path(args.input))
    validate_schema_records(
        evidence_records, "semantic_evidence", context="semantic evidence"
    )
    policy_records = read_jsonl(Path(args.oracle))
    policies = {record["query_id"]: record for record in policy_records}
    if len(policies) != len(policy_records):
        raise BenchmarkError("CPD oracle query IDs must be unique")
    validate_cpd_policy_routing(policy_records)
    computed_coverage = compute_coverage(
        policy_records,
        allow_draft=args.allow_draft_policy,
        expected_per_style=None if args.development else 76,
    )
    if args.coverage:
        coverage = read_json(Path(args.coverage))
        validate_schema_instance(coverage, "cpd_coverage", context="CPD coverage")
        if canonical_json_bytes(coverage) != canonical_json_bytes(computed_coverage):
            raise BenchmarkError("CPD coverage asset differs from the oracle policies")
        coverage_sha256 = sha256_file(Path(args.coverage))
    else:
        coverage = computed_coverage
        validate_schema_instance(coverage, "cpd_coverage", context="CPD coverage")
        coverage_sha256 = record_sha256(coverage)
    validate_records_against_roster(
        evidence_records, roster, require_confirmed=not args.development
    )
    scores = read_jsonl(Path(args.scores))
    validate_schema_records(scores, "semantic_score", context="semantic score")
    validate_records_against_roster(scores, roster, require_confirmed=not args.development)
    if manifest is not None:
        validate_score_provenance(scores, manifest, Path(args.roster), Path(args.oracle))
        validate_evidence_response_chain(
            evidence_records,
            response_records,
            manifest,
            roster["method_id"],
        )
        judge_bindings_by_run, judge_run_provenance = _load_judge_bindings(
            args, response_records, evidence_records, manifest
        )
        validate_score_judge_run_provenance(scores, judge_run_provenance)
        validate_scores_against_evidence(
            scores,
            evidence_records,
            policies.values(),
            roster,
            judge_verdicts_by_run=judge_bindings_by_run,
        )
        validate_aggregate_response_chain(response_records, scores)
    if manifest is not None:
        evaluator_source_sha256 = metric_source_hash(manifest)
        freeze_manifest_sha256 = manifest["manifest_sha256"]
    else:
        evaluator_source_sha256 = record_sha256(
            {
                "cpd": sha256_file(Path(__file__).with_name("cpd.py")),
                "schema": sha256_file(asset_path("schemas", "cpd_aggregate.schema.json")),
            }
        )
        freeze_manifest_sha256 = None
    aggregate = recompute_cpd_aggregate(
        evidence_records,
        scores,
        policy_records,
        coverage,
        roster,
        provenance={
            "roster_sha256": sha256_file(Path(args.roster)),
            "oracle_sha256": sha256_file(Path(args.oracle)),
            "coverage_sha256": coverage_sha256,
            "semantic_evidence_sha256": sha256_file(Path(args.input)),
            "semantic_scores_sha256": sha256_file(Path(args.scores)),
            "responses_sha256": (
                sha256_file(Path(args.responses)) if args.responses else None
            ),
            "judge_responses_sha256": (
                sha256_file(Path(args.judge_run_dir) / "judge_responses.jsonl")
                if args.judge_run_dir
                else sha256_file(Path(args.judge_responses))
                if args.judge_responses
                else None
            ),
            "evaluator_source_sha256": evaluator_source_sha256,
            "freeze_manifest_sha256": freeze_manifest_sha256,
            "generation_chain_verified": generation_chain["verified"],
            "generation_run_manifest_sha256": generation_chain["manifest_sha256"],
            "implementation_bundle_sha256": generation_chain[
                "implementation_bundle_sha256"
            ],
            "created_at_utc": _utc_now(),
        },
        allow_draft_policy=args.allow_draft_policy,
        expected_per_style=None if args.development else 76,
    )
    validate_schema_instance(aggregate, "cpd_aggregate", context="CPD aggregate")
    _emit(aggregate, args.output)


def _cmd_bootstrap(args: argparse.Namespace) -> None:
    _emit(
        clustered_bootstrap_mean(
            read_jsonl(Path(args.input)),
            value_field=args.value_field,
            cluster_field=args.cluster_field,
            iterations=args.iterations,
            seed=args.seed,
        ),
        args.output,
    )


def _cmd_audit_sample(args: argparse.Namespace) -> None:
    _emit(
        select_stratified_audit_sample(
            read_jsonl(Path(args.input)), rate=args.rate, seed=args.seed
        ),
        args.output,
    )


def _cmd_freeze_create(args: argparse.Namespace) -> None:
    specification = read_json(Path(args.spec))
    manifest = create_freeze_manifest(
        specification.get("assets", []),
        specification.get("protocol", {}),
        allow_draft=args.allow_draft,
    )
    _emit(manifest, args.output)


def _cmd_freeze_verify(args: argparse.Namespace) -> None:
    _emit(
        verify_freeze_manifest(read_json(Path(args.manifest)), require_frozen=not args.allow_draft),
        args.output,
    )


def _cmd_paths(args: argparse.Namespace) -> None:
    """Expose installed immutable inputs without relying on checkout layout."""

    _emit(
        {
            "package_root": str(PACKAGE_ROOT),
            "schema_directory": str(asset_path("schemas")),
            "query_library_directory": str(asset_path("query_lib")),
            "dev_query_library": str(
                asset_path("query_lib", "bus_ego_topdown_2d_dev_query_library_v0_2.jsonl")
            ),
            "test_query_library": str(
                asset_path("query_lib", "bus_ego_topdown_2d_query_library_v0_2.jsonl")
            ),
        },
        args.output,
    )


def _add_uqh_generation_chain_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument("--generation-library")
    command.add_argument("--generation-method-config")
    command.add_argument("--generation-output-root")
    command.add_argument("--generation-evidence")
    command.add_argument("--generation-run-manifest")


def _add_scoring_generation_chain_arguments(
    command: argparse.ArgumentParser,
) -> None:
    command.add_argument("--library")
    command.add_argument("--method-config")
    command.add_argument("--generation-dir")
    command.add_argument(
        "--allow-unverified-generation-chain",
        action="store_true",
        help=(
            "development-only acknowledgement that score/CPD output is not "
            "bound to a committed generation chain"
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Unified bus-scene benchmark tooling")
    subparsers = parser.add_subparsers(dest="command", required=True)
    from .review_cli import add_review_parser
    add_review_parser(subparsers)

    command = subparsers.add_parser(
        "paths", help="print installed benchmark asset paths for scripts and CI"
    )
    command.add_argument("--output")
    command.set_defaults(function=_cmd_paths)

    command = subparsers.add_parser("validate-library", help="validate JSONL schema and triplets")
    command.add_argument("--input", required=True)
    command.add_argument("--expected-rows", type=int)
    command.add_argument("--expected-groups", type=int)
    command.add_argument("--expected-supported-rows", type=int)
    command.add_argument("--output")
    command.set_defaults(function=_cmd_validate_library)

    command = subparsers.add_parser("draft-oracle", help="create explicitly unconfirmed oracle drafts")
    command.add_argument("--input", required=True)
    command.add_argument("--output", required=True)
    command.add_argument("--review-tasks")
    command.add_argument("--coverage")
    command.add_argument("--decisions")
    command.add_argument(
        "--cpd-registry-oracle",
        help="mark dimensions absent from this development oracle as CPD-ineligible",
    )
    command.set_defaults(function=_cmd_draft_oracle)

    command = subparsers.add_parser("audit-overlap", help="audit dev/test and internal duplicates")
    command.add_argument("--dev", required=True)
    command.add_argument("--test", required=True)
    command.add_argument("--duplicate-threshold", type=float, default=0.75)
    command.add_argument("--output")
    command.set_defaults(function=_cmd_overlap)

    command = subparsers.add_parser("build-roster", help="build an exact query/method/platform roster")
    command.add_argument("--library", required=True)
    command.add_argument("--method-id", required=True)
    command.add_argument("--platform", choices=("carla", "metadrive"), required=True)
    command.add_argument("--statistical-clusters", required=True)
    command.add_argument("--allow-draft", action="store_true")
    command.add_argument("--output", required=True)
    command.set_defaults(function=_cmd_build_roster)

    command = subparsers.add_parser(
        "generate", help="execute an immutable query-by-five generation grid"
    )
    command.add_argument("--library", required=True)
    command.add_argument("--roster", required=True)
    command.add_argument("--method-config", required=True)
    command.add_argument("--output-dir", required=True)
    command.add_argument(
        "--freeze-manifest",
        help="verified common freeze authorizing library, roster, and method config",
    )
    command.add_argument(
        "--development",
        action="store_true",
        help="allow draft method config and roster inputs",
    )
    command.add_argument(
        "--resume",
        action="store_true",
        help="verify and reuse immutable completed runs; never overwrite them",
    )
    command.add_argument("--summary")
    command.set_defaults(function=_cmd_generate)

    command = subparsers.add_parser(
        "generation-preflight",
        help="read-only generation readiness checks without consuming repetitions",
    )
    command.add_argument("--library", required=True)
    command.add_argument("--roster", required=True)
    command.add_argument("--method-config", required=True)
    command.add_argument(
        "--output",
        help="write the canonical readiness report instead of printing it",
    )
    command.set_defaults(function=_cmd_generation_preflight)

    command = subparsers.add_parser(
        "runtime-execute", help="run immutable platform-native Compile, SV, and NE stages"
    )
    command.add_argument("--responses", required=True)
    command.add_argument("--roster", required=True)
    command.add_argument("--library")
    command.add_argument("--method-config")
    command.add_argument("--generation-dir")
    command.add_argument("--platform-config", required=True)
    command.add_argument("--controller-config", required=True)
    command.add_argument("--output-dir", required=True)
    command.add_argument("--output", required=True)
    command.add_argument("--freeze-manifest")
    command.add_argument("--development", action="store_true")
    command.add_argument("--resume", action="store_true")
    command.add_argument("--summary")
    command.set_defaults(function=_cmd_runtime_execute)

    command = subparsers.add_parser(
        "runtime-aggregate", help="aggregate platform-labeled SV and NE diagnostics"
    )
    command.add_argument("--records", required=True)
    command.add_argument("--responses", required=True)
    command.add_argument("--roster", required=True)
    command.add_argument("--library")
    command.add_argument("--method-config")
    command.add_argument("--generation-dir")
    command.add_argument("--platform-config", required=True)
    command.add_argument("--controller-config", required=True)
    command.add_argument("--freeze-manifest")
    command.add_argument("--development", action="store_true")
    command.add_argument("--allow-partial", action="store_true")
    command.add_argument("--output")
    command.set_defaults(function=_cmd_runtime_aggregate)

    command = subparsers.add_parser(
        "method-result",
        help="publish one tamper-evident exclusive-create method/platform result",
    )
    command.add_argument("--freeze-manifest", required=True)
    command.add_argument("--library", required=True)
    command.add_argument("--oracle", required=True)
    command.add_argument("--roster", required=True)
    command.add_argument("--method-config", required=True)
    command.add_argument("--generation-dir", required=True)
    command.add_argument("--responses", required=True)
    command.add_argument("--semantic-evidence", required=True)
    command.add_argument("--semantic-scores", required=True)
    command.add_argument("--judge-run-dir", required=True)
    command.add_argument("--semantic-aggregate", required=True)
    command.add_argument("--uqh-assessments", required=True)
    command.add_argument("--uqh-assessor-registry", required=True)
    command.add_argument("--cpd-aggregate", required=True)
    command.add_argument("--cpd-coverage", required=True)
    command.add_argument("--runtime-records", required=True)
    command.add_argument("--runtime-aggregate", required=True)
    command.add_argument("--platform-config", required=True)
    command.add_argument("--controller-config", required=True)
    command.add_argument("--output", required=True)
    command.set_defaults(function=_cmd_method_result)

    command = subparsers.add_parser(
        "method-result-verify",
        help="reconstruct and verify a formal method/platform result envelope",
    )
    command.add_argument("--input", required=True)
    command.set_defaults(function=_cmd_method_result_verify)

    command = subparsers.add_parser(
        "extract-evidence",
        help="materialize immutable semantic evidence from a formal generation chain",
    )
    command.add_argument("--responses", required=True)
    command.add_argument("--output", required=True)
    command.add_argument("--roster", required=True)
    command.add_argument("--freeze-manifest", required=True)
    command.add_argument("--library", required=True)
    command.add_argument("--method-config", required=True)
    command.add_argument("--generation-dir", required=True)
    command.set_defaults(function=_cmd_extract_evidence)

    command = subparsers.add_parser(
        "judge-request",
        help="materialize every frozen deterministic-unknown Judge request",
    )
    command.add_argument("--responses", required=True)
    command.add_argument("--evidence", required=True)
    command.add_argument("--roster", required=True)
    command.add_argument("--freeze-manifest", required=True)
    command.add_argument("--output-dir", required=True)
    _add_scoring_generation_chain_arguments(command)
    command.set_defaults(function=_cmd_judge_request)

    command = subparsers.add_parser(
        "judge-run",
        help="execute a frozen Judge request bundle with its frozen runner",
    )
    command.add_argument("--request-dir", required=True)
    command.add_argument("--responses", required=True)
    command.add_argument("--evidence", required=True)
    command.add_argument("--roster", required=True)
    command.add_argument("--freeze-manifest", required=True)
    command.add_argument("--output-dir", required=True)
    command.add_argument("--resume", action="store_true")
    _add_scoring_generation_chain_arguments(command)
    command.set_defaults(function=_cmd_judge_run)

    command = subparsers.add_parser(
        "judge-calibration-context",
        help="freeze a profile-specific pre-run Judge calibration context",
    )
    command.add_argument(
        "--profile",
        choices=("carla_stage", "cross_platform_final"),
        required=True,
    )
    command.add_argument("--query-library", required=True)
    command.add_argument("--oracle", required=True)
    command.add_argument("--roster", required=True)
    command.add_argument("--human-gold", required=True)
    command.add_argument("--prompt", required=True)
    command.add_argument("--output-schema", required=True)
    command.add_argument("--runner-manifest", required=True)
    command.add_argument("--model-id", required=True)
    command.add_argument("--inference-config", required=True)
    command.add_argument("--carla-source-config", required=True)
    command.add_argument("--carla-roster", required=True)
    command.add_argument("--carla-responses", required=True)
    command.add_argument("--carla-evidence", required=True)
    command.add_argument("--metadrive-source-config")
    command.add_argument("--metadrive-roster")
    command.add_argument("--metadrive-responses")
    command.add_argument("--metadrive-evidence")
    command.add_argument("--output", required=True)
    command.set_defaults(function=_cmd_judge_calibration_context)

    command = subparsers.add_parser(
        "judge-calibration-request",
        help="materialize the precommitted Judge calibration request bundle",
    )
    command.add_argument("--context", required=True)
    command.add_argument("--output-dir", required=True)
    command.set_defaults(function=_cmd_judge_calibration_request)

    command = subparsers.add_parser(
        "judge-calibration-run",
        help="execute and validate precommitted Judge calibration requests",
    )
    command.add_argument("--context", required=True)
    command.add_argument("--request-dir", required=True)
    command.add_argument("--output-dir", required=True)
    command.add_argument("--resume", action="store_true")
    command.set_defaults(function=_cmd_judge_calibration_run)

    command = subparsers.add_parser("score", help="score evidence against a layered oracle")
    command.add_argument("--oracle", required=True)
    command.add_argument("--evidence", required=True)
    command.add_argument("--responses")
    command.add_argument("--judge-responses")
    command.add_argument("--judge-run-dir")
    command.add_argument("--output", required=True)
    command.add_argument("--roster", required=True)
    command.add_argument("--freeze-manifest")
    command.add_argument("--development", action="store_true")
    command.add_argument("--allow-draft-oracle", action="store_true")
    _add_scoring_generation_chain_arguments(command)
    command.set_defaults(function=_cmd_score)

    command = subparsers.add_parser("aggregate", help="aggregate semantic, RQS, and UQH metrics")
    command.add_argument("--scores", required=True)
    command.add_argument("--responses")
    command.add_argument("--evidence")
    command.add_argument("--judge-responses")
    command.add_argument("--judge-run-dir")
    command.add_argument("--uqh-assessments")
    command.add_argument("--uqh-assessor-registry")
    command.add_argument("--roster", required=True)
    command.add_argument("--oracle")
    command.add_argument("--freeze-manifest")
    command.add_argument("--development", action="store_true")
    command.add_argument("--output")
    _add_uqh_generation_chain_arguments(command)
    command.set_defaults(function=_cmd_aggregate)

    command = subparsers.add_parser(
        "uqh-request",
        help="export immutable, source-bound requests for the frozen UQH assessor",
    )
    command.add_argument("--responses", required=True)
    command.add_argument("--oracle", required=True)
    command.add_argument("--roster", required=True)
    command.add_argument("--uqh-assessor-registry", required=True)
    command.add_argument("--assessor-id", required=True)
    command.add_argument("--output-dir", required=True)
    command.add_argument("--freeze-manifest")
    command.add_argument("--development", action="store_true")
    _add_uqh_generation_chain_arguments(command)
    command.set_defaults(function=_cmd_uqh_request)

    command = subparsers.add_parser(
        "uqh-attest",
        help="create independently signed UQH assessments from raw assessor executions",
    )
    command.add_argument("--responses", required=True)
    command.add_argument("--oracle", required=True)
    command.add_argument("--roster", required=True)
    command.add_argument("--assessor-executions", required=True)
    command.add_argument("--uqh-assessor-registry", required=True)
    command.add_argument("--assessor-id", required=True)
    command.add_argument("--private-key", required=True)
    command.add_argument("--output-dir", required=True)
    command.add_argument("--freeze-manifest")
    command.add_argument("--development", action="store_true")
    _add_uqh_generation_chain_arguments(command)
    command.set_defaults(function=_cmd_uqh_attest)

    command = subparsers.add_parser("cpd", help="compute five-output CPD_common")
    command.add_argument("--input", required=True)
    command.add_argument("--oracle", required=True)
    command.add_argument("--scores", required=True)
    command.add_argument("--coverage")
    command.add_argument("--responses")
    command.add_argument("--judge-responses")
    command.add_argument("--judge-run-dir")
    command.add_argument("--roster", required=True)
    command.add_argument("--freeze-manifest")
    command.add_argument("--development", action="store_true")
    command.add_argument("--allow-draft-policy", action="store_true")
    command.add_argument("--output")
    _add_scoring_generation_chain_arguments(command)
    command.set_defaults(function=_cmd_cpd)

    command = subparsers.add_parser("bootstrap", help="intent-clustered bootstrap confidence interval")
    command.add_argument("--input", required=True)
    command.add_argument("--value-field", required=True)
    command.add_argument("--cluster-field", default="statistical_intent_cluster_id")
    command.add_argument("--iterations", type=int, default=10000)
    command.add_argument("--seed", type=int, default=0)
    command.add_argument("--output")
    command.set_defaults(function=_cmd_bootstrap)

    command = subparsers.add_parser(
        "audit-sample",
        help="diagnostic sampling-only; not a human assignment or gold workflow",
    )
    command.add_argument("--input", required=True)
    command.add_argument("--rate", type=float, default=0.10)
    command.add_argument("--seed", type=int, default=0)
    command.add_argument("--output")
    command.set_defaults(function=_cmd_audit_sample)

    command = subparsers.add_parser("freeze-create", help="create a hash-addressed freeze manifest")
    command.add_argument("--spec", required=True)
    command.add_argument("--output", required=True)
    command.add_argument("--allow-draft", action="store_true")
    command.set_defaults(function=_cmd_freeze_create)

    command = subparsers.add_parser("freeze-verify", help="verify a freeze manifest and every asset")
    command.add_argument("--manifest", required=True)
    command.add_argument("--allow-draft", action="store_true")
    command.add_argument("--output")
    command.set_defaults(function=_cmd_freeze_verify)
    return parser


def main(argv: List[str] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.function(args)
    except BenchmarkError as exc:
        print("error: {}".format(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
