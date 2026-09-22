"""Hash provenance linking responses, evidence, scores, and frozen evaluators."""

from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence

from .errors import ValidationError
from .jsonio import canonical_json_bytes, read_json, read_jsonl, sha256_bytes, sha256_file
from .schema import supported_schema_paths


def record_sha256(record: Mapping[str, Any]) -> str:
    return sha256_bytes(canonical_json_bytes(record))


def _asset_records(manifest: Mapping[str, Any], role: str) -> Sequence[Mapping[str, Any]]:
    return [asset for asset in manifest.get("assets", []) if asset.get("role") == role]


def frozen_asset_hash(
    manifest: Mapping[str, Any], role: str, path: Optional[Path] = None
) -> str:
    records = _asset_records(manifest, role)
    if path is not None:
        resolved = str(Path(path).resolve())
        records = [
            record
            for record in records
            if str(Path(record.get("path", "")).resolve()) == resolved
        ]
    if len(records) != 1:
        raise ValidationError(
            "expected one frozen {} asset, got {}".format(role, len(records))
        )
    return records[0]["sha256"]


def metric_source_hash(manifest: Mapping[str, Any]) -> str:
    extractor_records = _asset_records(manifest, "semantic_extractor_manifest")
    if len(extractor_records) != 1:
        raise ValidationError("freeze lacks one semantic extractor manifest")
    document = read_json(Path(extractor_records[0]["path"]))
    current_metric_paths = {
        path.resolve() for path in Path(__file__).resolve().parent.glob("*.py")
    }
    current_schema_paths = set(supported_schema_paths())
    metric_bindings = document.get("metric_files", [])
    schema_bindings = document.get("schema_files", [])
    bound_metric_paths = {
        Path(binding.get("path", "")).resolve()
        for binding in metric_bindings
        if isinstance(binding, Mapping)
    }
    bound_schema_paths = {
        Path(binding.get("path", "")).resolve()
        for binding in schema_bindings
        if isinstance(binding, Mapping)
    }
    if (
        bound_metric_paths != current_metric_paths
        or len(metric_bindings) != len(current_metric_paths)
        or bound_schema_paths != current_schema_paths
        or len(schema_bindings) != len(current_schema_paths)
    ):
        raise ValidationError("complete evaluator implementation is not frozen")
    normalized = []
    for kind, bindings in (
        ("metric", metric_bindings),
        ("schema", schema_bindings),
    ):
        for binding in bindings:
            path = Path(binding["path"]).resolve()
            if binding.get("sha256") != sha256_file(path):
                raise ValidationError("frozen evaluator source hash mismatch")
            normalized.append(
                {"kind": kind, "path": str(path), "sha256": binding["sha256"]}
            )
    return sha256_bytes(
        canonical_json_bytes(
            sorted(normalized, key=lambda item: (item["kind"], item["path"]))
        )
    )


def validate_frozen_judge_response_chain(
    raw_judge_records: Iterable[Mapping[str, Any]],
    response_records: Iterable[Mapping[str, Any]],
    evidence_records: Iterable[Mapping[str, Any]],
    manifest: Mapping[str, Any],
) -> Dict[str, Dict[str, Mapping[str, Any]]]:
    """Validate raw Judge responses and return score-safe, source-bound verdicts."""

    from .judge import (
        judge_config_sha256,
        judge_item_id,
        judge_request_sha256,
        judge_response_id,
        parse_raw_judge_response,
    )
    from .judge_pipeline import load_frozen_judge_runner
    from .metrics import deterministic_atom_verdict
    from .schema import load_schema, validate_schema_instance, validate_schema_records

    raw_judge_records = list(raw_judge_records)
    response_records = list(response_records)
    evidence_records = list(evidence_records)
    validate_schema_records(
        raw_judge_records, "judge_response_record", context="judge response"
    )
    responses = {record.get("run_id"): record for record in response_records}
    evidence = {record.get("run_id"): record for record in evidence_records}
    if (
        None in responses
        or None in evidence
        or len(responses) != len(response_records)
        or len(evidence) != len(evidence_records)
        or set(responses) != set(evidence)
    ):
        raise ValidationError(
            "judge validation requires identical unique response/evidence run IDs"
        )

    rubric_assets = _asset_records(manifest, "judge_rubric")
    if len(rubric_assets) != 1:
        raise ValidationError("freeze lacks one judge rubric")
    rubric_path = Path(rubric_assets[0]["path"])
    if sha256_file(rubric_path) != rubric_assets[0]["sha256"]:
        raise ValidationError("frozen judge rubric hash mismatch")
    rubric = read_json(rubric_path)
    validate_schema_instance(rubric, "judge_rubric", context="judge rubric")
    runner_assets = _asset_records(manifest, "judge_runner_manifest")
    if len(runner_assets) > 1:
        raise ValidationError("freeze has multiple judge runners")
    runner_asset = None
    runner = None
    if runner_assets:
        runner_asset, runner = load_frozen_judge_runner(manifest)
        if rubric.get("runner_manifest_sha256") != runner_asset["sha256"]:
            raise ValidationError("judge rubric is not bound to the frozen runner")
    elif rubric.get("runner_manifest_sha256") is not None:
        raise ValidationError("judge rubric references a missing frozen runner")

    prompt_binding = rubric["prompt"]
    output_binding = rubric["output_schema"]
    prompt_path = Path(prompt_binding["path"])
    output_path = Path(output_binding["path"])
    for path, binding, label in (
        (prompt_path, prompt_binding, "judge prompt"),
        (output_path, output_binding, "judge output schema"),
    ):
        if (
            not path.is_file()
            or sha256_file(path) != binding["sha256"]
            or path.stat().st_size != binding["bytes"]
        ):
            raise ValidationError("{} binding changed".format(label))
    try:
        prompt_text = prompt_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValidationError("judge prompt is not readable UTF-8") from exc
    output_schema = read_json(output_path)
    if canonical_json_bytes(output_schema) != canonical_json_bytes(
        load_schema("judge_output")
    ):
        raise ValidationError("judge output schema differs from the frozen contract")
    computed_config_sha256 = judge_config_sha256(
        model_id=rubric["model_id"],
        prompt_sha256=prompt_binding["sha256"],
        inference_config=rubric["inference_config"],
        output_schema_sha256=output_binding["sha256"],
        request_contract=rubric["request_contract"],
        runner_manifest_sha256=(runner_asset["sha256"] if runner_asset else None),
    )
    if rubric["judge_config_sha256"] != computed_config_sha256:
        raise ValidationError("judge rubric config hash mismatch")

    library_assets = _asset_records(manifest, "query_library_test")
    oracle_assets = _asset_records(manifest, "requirement_oracle_test")
    if len(library_assets) != 1 or len(oracle_assets) != 1:
        raise ValidationError("freeze lacks a unique test library/oracle")
    query_by_id = {
        record["query_id"]: record
        for record in read_jsonl(Path(library_assets[0]["path"]))
    }
    oracle_by_id = {
        record["query_id"]: record
        for record in read_jsonl(Path(oracle_assets[0]["path"]))
    }

    eligible = {}
    for run_id, response in responses.items():
        evidence_record = evidence[run_id]
        query_id = response.get("query_id")
        if query_id not in query_by_id or query_id not in oracle_by_id:
            raise ValidationError("judge response source references an unknown query")
        if response.get("expected_support") != "supported":
            continue
        response_sha256 = record_sha256(response)
        evidence_sha256 = record_sha256(evidence_record)
        for atom in oracle_by_id[query_id].get("atoms", []):
            if (
                atom.get("decision_status") == "confirmed"
                and atom.get("layer")
                in ("core_required", "surface_required", "forbidden")
                and deterministic_atom_verdict(atom, evidence_record) == "unknown"
            ):
                eligible[(run_id, atom["atom_id"])] = {
                    "response": response,
                    "evidence": evidence_record,
                    "oracle_atom": atom,
                    "query": query_by_id[query_id],
                    "source_response_sha256": response_sha256,
                    "source_evidence_sha256": evidence_sha256,
                }

    bindings: Dict[str, Dict[str, Mapping[str, Any]]] = {}
    seen_pairs = set()
    seen_response_ids = set()
    for raw_record in raw_judge_records:
        pair = (raw_record["run_id"], raw_record["atom_id"])
        if pair in seen_pairs or raw_record["judge_response_id"] in seen_response_ids:
            raise ValidationError("judge response records contain a duplicate")
        seen_pairs.add(pair)
        seen_response_ids.add(raw_record["judge_response_id"])
        source = eligible.get(pair)
        if source is None:
            raise ValidationError("judge response targets a non-judge-routed atom")
        response = source["response"]
        evidence_record = source["evidence"]
        oracle_atom = source["oracle_atom"]
        query = source["query"]
        response_sha256 = source["source_response_sha256"]
        evidence_sha256 = source["source_evidence_sha256"]
        expected_item_id = judge_item_id(
            response_sha256, evidence_sha256, oracle_atom["atom_id"]
        )
        artifact = response.get("artifact")
        if not isinstance(artifact, Mapping):
            raise ValidationError("judge-routed response lacks an artifact")
        artifact_path = Path(str(artifact.get("path", "")))
        if (
            not artifact_path.is_file()
            or sha256_file(artifact_path) != artifact.get("sha256")
            or artifact_path.stat().st_size != artifact.get("bytes")
        ):
            raise ValidationError("judge source artifact binding changed")
        try:
            artifact_text = artifact_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ValidationError("judge source artifact is not readable UTF-8") from exc
        expected_request_sha256 = judge_request_sha256(
            model_id=rubric["model_id"],
            judge_config_sha256_value=computed_config_sha256,
            inference_config=rubric["inference_config"],
            prompt_text=prompt_text,
            query_text=query["query_text"],
            platform=response["platform"],
            artifact_sha256=artifact["sha256"],
            artifact_text=artifact_text,
            source_response_sha256=response_sha256,
            source_evidence_sha256=evidence_sha256,
            evidence=evidence_record,
            oracle_atom=oracle_atom,
            output_schema=output_schema,
            request_contract=rubric["request_contract"],
        )
        expected_response_id = judge_response_id(expected_request_sha256, attempt=0)
        if (
            raw_record.get("judge_response_id") != expected_response_id
            or raw_record.get("item_id") != expected_item_id
            or raw_record.get("query_id") != response["query_id"]
            or raw_record.get("model_id") != rubric["model_id"]
            or raw_record.get("judge_config_sha256") != computed_config_sha256
            or raw_record.get("judge_request_sha256") != expected_request_sha256
            or (
                runner is not None
                and raw_record.get("provenance", {}).get("runner_id")
                != runner["runner_id"]
            )
            or (
                runner is not None
                and raw_record.get("provenance", {}).get("runner_version")
                != runner["runner_version"]
            )
        ):
            raise ValidationError("raw judge response is not bound to its exact request")
        raw_response = raw_record["raw_response"]
        if raw_record["raw_response_sha256"] != sha256_bytes(
            raw_response.encode("utf-8")
        ):
            raise ValidationError("judge raw-response byte hash mismatch")
        parsed = parse_raw_judge_response(raw_response)
        bindings.setdefault(pair[0], {})[pair[1]] = {
            "verdict": parsed["verdict"],
            "judge_response_id": raw_record["judge_response_id"],
            "judge_response_record_sha256": record_sha256(raw_record),
        }
    if seen_pairs != set(eligible):
        missing = sorted(set(eligible) - seen_pairs)
        raise ValidationError(
            "judge responses must exactly cover deterministic-unknown atoms; missing {}".format(
                missing[:5]
            )
        )
    return bindings


def validate_evidence_response_chain(
    evidence_records: Iterable[Mapping[str, Any]],
    response_records: Iterable[Mapping[str, Any]],
    manifest: Mapping[str, Any],
    method_id: str,
) -> Dict[str, Mapping[str, Any]]:
    from .freeze import (
        EXTRACTOR_PROJECTION_FIELDS,
        execute_frozen_extractor,
        load_frozen_extractor_runtime,
    )

    evidence_records = list(evidence_records)
    response_records = list(response_records)
    responses = {}
    for response in response_records:
        run_id = response.get("run_id")
        if not run_id or run_id in responses:
            raise ValidationError("responses require unique non-empty run_id values")
        responses[run_id] = response
    evidence_ids = [record.get("run_id") for record in evidence_records]
    if any(not run_id for run_id in evidence_ids) or len(set(evidence_ids)) != len(evidence_ids):
        raise ValidationError("evidence requires unique non-empty run_id values")
    if set(evidence_ids) != set(responses):
        raise ValidationError("evidence and responses must cover identical run IDs")

    extractor_hash = frozen_asset_hash(manifest, "semantic_extractor_manifest")
    method_configs = []
    for asset in _asset_records(manifest, "method_config"):
        document = read_json(Path(asset["path"]))
        if document.get("method_id") == method_id:
            method_configs.append(asset)
    if len(method_configs) != 1:
        raise ValidationError("method has no unique frozen configuration")
    config_hash = method_configs[0]["sha256"]
    libraries = _asset_records(manifest, "query_library_test")
    if len(libraries) != 1:
        raise ValidationError("freeze lacks one locked test query library")
    query_records = {
        record["query_id"]: record for record in read_jsonl(Path(libraries[0]["path"]))
    }
    oracle_assets = _asset_records(manifest, "requirement_oracle_test")
    if len(oracle_assets) != 1:
        raise ValidationError("freeze lacks one locked test requirement oracle")
    oracle_records = {
        record["query_id"]: record
        for record in read_jsonl(Path(oracle_assets[0]["path"]))
    }
    extractor_document, extractor_runtime = load_frozen_extractor_runtime(manifest)

    for evidence in evidence_records:
        response = responses[evidence["run_id"]]
        for field in (
            "query_id",
            "intent_group_id",
            "statistical_intent_cluster_id",
            "surface_style",
            "expected_support",
            "repetition",
            "method_id",
            "platform",
            "terminal_status",
        ):
            if evidence.get(field) != response.get(field):
                raise ValidationError(
                    "evidence/response {} mismatch for {}".format(
                        field, evidence["run_id"]
                    )
                )
        if response.get("config_sha256") != config_hash:
            raise ValidationError("response was not produced by the frozen method config")
        query_record = query_records.get(response.get("query_id"))
        if query_record is None:
            raise ValidationError("response query is outside the locked test library")
        expected_request_hash = sha256_bytes(
            canonical_json_bytes({"query_text": query_record["query_text"]})
        )
        if response.get("request_sha256") != expected_request_hash:
            raise ValidationError("response request hash is not the query_text-only payload")
        oracle_record = oracle_records.get(response.get("query_id"))
        if oracle_record is None:
            raise ValidationError("response query lacks a frozen requirement oracle")
        provenance = evidence.get("provenance", {})
        if provenance.get("source_response_sha256") != record_sha256(response):
            raise ValidationError("evidence response hash mismatch")
        artifact = response.get("artifact")
        expected_artifact_hash = artifact.get("sha256") if isinstance(artifact, Mapping) else None
        if isinstance(artifact, Mapping):
            artifact_path = Path(str(artifact.get("path", "")))
            if not artifact_path.is_file():
                raise ValidationError("response artifact file is missing")
            if sha256_file(artifact_path) != expected_artifact_hash:
                raise ValidationError("response artifact file hash mismatch")
            if artifact_path.stat().st_size != artifact.get("bytes"):
                raise ValidationError("response artifact byte count mismatch")
        if provenance.get("source_artifact_sha256") != expected_artifact_hash:
            raise ValidationError("evidence artifact hash mismatch")
        if provenance.get("extractor_config_sha256") != extractor_hash:
            raise ValidationError("evidence extractor manifest hash mismatch")
        if (
            provenance.get("extractor_id") != extractor_document.get("extractor_id")
            or provenance.get("extractor_version")
            != extractor_document.get("extractor_version")
        ):
            raise ValidationError("evidence extractor identity is not frozen")
        live_projection = execute_frozen_extractor(extractor_runtime, response)
        evidence_projection = {
            field: evidence.get(field)
            for field in EXTRACTOR_PROJECTION_FIELDS
        }
        if canonical_json_bytes(live_projection) != canonical_json_bytes(
            evidence_projection
        ):
            raise ValidationError(
                "semantic evidence differs from live frozen-extractor output"
            )
    return responses


def validate_scores_against_evidence(
    score_records: Iterable[Mapping[str, Any]],
    evidence_records: Iterable[Mapping[str, Any]],
    oracle_records: Iterable[Mapping[str, Any]],
    roster: Mapping[str, Any],
    judge_verdicts_by_run: Mapping[str, Mapping[str, Mapping[str, Any]]] = None,
) -> None:
    """Recompute every semantic score and reject self-consistent forged score files."""

    from .metrics import score_semantic_output
    from .roster import roster_entries

    score_records = list(score_records)
    evidence_records = list(evidence_records)
    oracle_records = list(oracle_records)
    scores = {record.get("run_id"): record for record in score_records}
    evidence = {record.get("run_id"): record for record in evidence_records}
    oracles = {record.get("query_id"): record for record in oracle_records}
    if (
        None in scores
        or None in evidence
        or len(scores) != len(score_records)
        or len(evidence) != len(evidence_records)
        or set(scores) != set(evidence)
    ):
        raise ValidationError("scores and evidence require identical unique run IDs")
    entries = roster_entries(roster)
    supported_fields = (
        "query_id",
        "intent_group_id",
        "statistical_intent_cluster_id",
        "surface_style",
        "expected_support",
        "ego_gate_passed",
        "srs",
        "srs_common",
        "common_core_preservation",
        "srs_category_scores",
        "srs_common_category_scores",
        "arc",
        "rsc",
        "iec_spec",
        "atom_results",
        "judge_bindings_sha256",
    )
    unsupported_fields = (
        "query_id",
        "intent_group_id",
        "statistical_intent_cluster_id",
        "surface_style",
        "expected_support",
        "semantic_metrics",
        "reason",
    )
    judge_verdicts_by_run = dict(judge_verdicts_by_run or {})
    if not set(judge_verdicts_by_run) <= set(evidence):
        raise ValidationError("judge bindings reference an unknown run")
    for run_id, evidence_record in evidence.items():
        query_id = evidence_record.get("query_id")
        if query_id not in oracles or query_id not in entries:
            raise ValidationError("evidence references an unknown oracle or roster query")
        oracle = dict(oracles[query_id])
        oracle["statistical_intent_cluster_id"] = entries[query_id][
            "statistical_intent_cluster_id"
        ]
        recomputed = score_semantic_output(
            oracle,
            evidence_record,
            judge_verdicts=judge_verdicts_by_run.get(run_id, {}),
        )
        actual = scores[run_id]
        fields = (
            supported_fields
            if recomputed.get("expected_support") == "supported"
            else unsupported_fields
        )
        expected_projection = {field: recomputed.get(field) for field in fields}
        actual_projection = {field: actual.get(field) for field in fields}
        if canonical_json_bytes(expected_projection) != canonical_json_bytes(
            actual_projection
        ):
            raise ValidationError("semantic score differs from live frozen-evaluator output")
        if actual.get("provenance", {}).get(
            "evidence_record_sha256"
        ) != record_sha256(evidence_record):
            raise ValidationError("semantic score is not bound to its evidence record")


def validate_score_provenance(
    score_records: Iterable[Mapping[str, Any]],
    manifest: Mapping[str, Any],
    roster_path: Path,
    oracle_path: Path,
) -> None:
    roster_hash = frozen_asset_hash(manifest, "query_roster", roster_path)
    oracle_hash = frozen_asset_hash(manifest, "requirement_oracle_test", oracle_path)
    extractor_hash = frozen_asset_hash(manifest, "semantic_extractor_manifest")
    evaluator_hash = metric_source_hash(manifest)
    freeze_hash = manifest.get("manifest_sha256")
    oracle_records = {record.get("query_id"): record for record in read_jsonl(oracle_path)}
    run_ids = set()
    for score in score_records:
        run_id = score.get("run_id")
        if not run_id or run_id in run_ids:
            raise ValidationError("scores require unique non-empty run_id values")
        run_ids.add(run_id)
        provenance = score.get("provenance", {})
        expected = {
            "roster_sha256": roster_hash,
            "oracle_sha256": oracle_hash,
            "extractor_manifest_sha256": extractor_hash,
            "evaluator_source_sha256": evaluator_hash,
            "freeze_manifest_sha256": freeze_hash,
        }
        if any(provenance.get(field) != value for field, value in expected.items()):
            raise ValidationError("semantic score provenance does not match the freeze")
        for field in (
            "response_record_sha256",
            "evidence_record_sha256",
            "oracle_record_sha256",
        ):
            value = provenance.get(field)
            if not isinstance(value, str) or len(value) != 64:
                raise ValidationError("semantic score lacks {}".format(field))
        oracle_record = oracle_records.get(score.get("query_id"))
        if oracle_record is None or provenance.get("oracle_record_sha256") != record_sha256(
            oracle_record
        ):
            raise ValidationError("semantic score oracle-record hash mismatch")


def validate_score_judge_run_provenance(
    score_records: Iterable[Mapping[str, Any]],
    expected: Mapping[str, str],
) -> None:
    required = {"judge_run_manifest_sha256", "judge_responses_sha256"}
    if set(expected) != required or any(
        not isinstance(expected[field], str) or len(expected[field]) != 64
        for field in required
    ):
        raise ValidationError("validated Judge run provenance is malformed")
    for score in score_records:
        provenance = score.get("provenance", {})
        if any(provenance.get(field) != expected[field] for field in required):
            raise ValidationError("semantic score is bound to a different Judge run")


def validate_aggregate_response_chain(
    response_records: Iterable[Mapping[str, Any]],
    score_records: Iterable[Mapping[str, Any]],
) -> None:
    response_records = list(response_records)
    score_records = list(score_records)
    responses = {record.get("run_id"): record for record in response_records}
    scores = {record.get("run_id"): record for record in score_records}
    if (
        None in responses
        or None in scores
        or len(responses) != len(response_records)
        or len(scores) != len(score_records)
    ):
        raise ValidationError("response and score run IDs must be unique and non-empty")
    if set(responses) != set(scores):
        raise ValidationError("UQH responses and semantic scores must cover the same runs")
    for run_id, response in responses.items():
        if scores[run_id].get("provenance", {}).get(
            "response_record_sha256"
        ) != record_sha256(response):
            raise ValidationError("UQH response differs from the scored response")
