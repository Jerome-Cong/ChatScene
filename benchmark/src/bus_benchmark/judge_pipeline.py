"""Immutable request and execution bundles for the frozen semantic Judge."""

import datetime as _datetime
import copy
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

from .artifact_bundle import (
    AtomicBundle,
    bound_bytes,
    jsonl_bytes,
    path_has_symlink_component,
    validate_exact_bundle_inventory,
    write_staged_bytes,
)
from .errors import ValidationError
from .jsonio import (
    canonical_json_bytes,
    read_json,
    read_jsonl,
    sha256_bytes,
    sha256_file,
    strict_json_object_bytes,
)
from .judge import (
    judge_config_sha256,
    judge_item_id,
    judge_request_payload,
    judge_response_id,
    parse_raw_judge_response,
)
from .metrics import deterministic_atom_verdict
from .schema import load_schema, validate_schema_instance, validate_schema_records


def _utc_now() -> str:
    return _datetime.datetime.now(_datetime.timezone.utc).isoformat()


def _record_sha256(record: Mapping[str, Any]) -> str:
    return sha256_bytes(canonical_json_bytes(dict(record)))


def _records_sha256(records: Sequence[Mapping[str, Any]]) -> str:
    return sha256_bytes(jsonl_bytes(records))


def _manifest_sha256(manifest: Mapping[str, Any]) -> str:
    payload = dict(manifest)
    payload.pop("manifest_sha256", None)
    return sha256_bytes(canonical_json_bytes(payload))


def _asset_records(
    manifest: Mapping[str, Any], role: str
) -> List[Mapping[str, Any]]:
    return [
        asset for asset in manifest.get("assets", []) if asset.get("role") == role
    ]


def _unique_asset(manifest: Mapping[str, Any], role: str) -> Mapping[str, Any]:
    records = _asset_records(manifest, role)
    if len(records) != 1:
        raise ValidationError("freeze requires exactly one {} asset".format(role))
    record = records[0]
    path = Path(str(record.get("path", "")))
    if path_has_symlink_component(path) or not path.is_file():
        raise ValidationError("frozen {} asset is missing or is a symlink".format(role))
    payload = path.read_bytes()
    if (
        len(payload) != record.get("bytes")
        or sha256_bytes(payload) != record.get("sha256")
    ):
        raise ValidationError("frozen {} asset binding changed".format(role))
    return record


def _binding_for(path: Path, payload: bytes) -> Dict[str, Any]:
    return {
        "path": str(Path(path).resolve()),
        "sha256": sha256_bytes(payload),
        "bytes": len(payload),
    }


def _binding_from_file(path: Path, label: str) -> Dict[str, Any]:
    path = Path(os.path.abspath(str(path)))
    if path_has_symlink_component(path) or not path.is_file():
        raise ValidationError("{} is missing or symlinked".format(label))
    path = path.resolve()
    return _binding_for(path, path.read_bytes())


def _validate_file_binding(
    binding: Mapping[str, Any], label: str, *, require_nonempty: bool = False
) -> bytes:
    return bound_bytes(binding, label, require_nonempty=require_nonempty)


def _load_frozen_judge_runner_material(
    manifest: Mapping[str, Any],
) -> Mapping[str, Any]:
    asset = _unique_asset(manifest, "judge_runner_manifest")
    runner_payload = Path(asset["path"]).read_bytes()
    if (
        len(runner_payload) != asset["bytes"]
        or sha256_bytes(runner_payload) != asset["sha256"]
    ):
        raise ValidationError("frozen Judge runner changed while being loaded")
    runner = strict_json_object_bytes(runner_payload, "judge runner manifest")
    validate_schema_instance(runner, "judge_runner_manifest", context="judge runner")
    environment_payload = _validate_file_binding(
        runner["environment_manifest"], "judge runner environment", require_nonempty=True
    )
    environment = strict_json_object_bytes(
        environment_payload, "judge runner environment"
    )
    validate_schema_instance(
        environment,
        "method_environment_manifest",
        context="judge runner environment",
    )
    if environment.get("decision_status") != "frozen":
        raise ValidationError("judge runner environment is not frozen")
    executable_payload = _validate_file_binding(
        environment["executable"], "judge runner executable"
    )
    if len(executable_payload) != environment["executable"]["bytes"]:
        raise ValidationError("judge runner executable binding changed")
    if environment.get("runtime_kind") == "python":
        from .generation import probe_python_environment

        executable_fd = _sealed_memfd(
            "judge-runner-environment-probe",
            executable_payload,
            0o500,
            executable=True,
        )
        try:
            live_environment = probe_python_environment(
                Path("/proc/self/fd/{}".format(executable_fd)),
                _pass_fds=(executable_fd,),
            )
        finally:
            os.close(executable_fd)
        frozen_environment = {
            "runtime_version": environment["runtime_version"],
            "locked_dependencies": environment["locked_dependencies"],
        }
        if canonical_json_bytes(live_environment) != canonical_json_bytes(
            frozen_environment
        ):
            raise ValidationError("judge runner live Python environment drifted")
    source_paths = []
    source_material = []
    for source in runner["source_files"]:
        payload = _validate_file_binding(
            source, "judge runner source", require_nonempty=True
        )
        source_paths.append(str(Path(source["path"]).resolve()))
        source_material.append((source, payload))
    if source_paths != sorted(source_paths) or len(source_paths) != len(
        set(source_paths)
    ):
        raise ValidationError("judge runner source files must be sorted and unique")
    argv = runner["command"]["argv"]
    if str(Path(argv[0]).resolve()) != str(
        Path(environment["executable"]["path"]).resolve()
    ):
        raise ValidationError("judge runner command does not use its frozen executable")
    source_path_set = set(source_paths)
    for argument in argv[1:]:
        path = Path(argument)
        if path.is_absolute() and str(path.resolve()) not in source_path_set:
            raise ValidationError("judge runner command references an unfrozen source file")
    source_names = [Path(path).name for path in source_paths]
    if len(source_names) != len(set(source_names)):
        raise ValidationError("judge runner source basenames must be unique")
    return {
        "asset": asset,
        "runner": runner,
        "environment": environment,
        "executable_payload": executable_payload,
        "source_material": source_material,
    }


def load_frozen_judge_runner(
    manifest: Mapping[str, Any],
) -> Tuple[Mapping[str, Any], Mapping[str, Any]]:
    """Load and live-verify the unique runner, environment, and source bytes."""

    material = _load_frozen_judge_runner_material(manifest)
    return material["asset"], material["runner"]


@contextmanager
def _runner_execution_workspace(material: Mapping[str, Any]):
    """Provide only an external disposable work root for one request bundle."""

    root = Path(tempfile.mkdtemp(prefix="bus-benchmark-judge-exec-"))
    work = root / "work"
    work.mkdir(mode=0o700)
    try:
        yield material["runner"], work
    finally:
        shutil.rmtree(str(root), ignore_errors=False)


def _write_all_fd(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    written = 0
    while written < len(view):
        count = os.write(fd, view[written:])
        if count <= 0:  # pragma: no cover - defensive OS contract guard
            raise OSError("short write while sealing Judge runner bytes")
        written += count
    os.lseek(fd, 0, os.SEEK_SET)


def _sealed_memfd(
    name: str, payload: bytes, mode: int, *, executable: bool
) -> int:
    """Create an immutable anonymous file from already verified in-memory bytes."""

    required_os = ("memfd_create", "MFD_ALLOW_SEALING")
    required_fcntl = (
        "F_ADD_SEALS",
        "F_GET_SEALS",
        "F_SEAL_WRITE",
        "F_SEAL_GROW",
        "F_SEAL_SHRINK",
        "F_SEAL_SEAL",
    )
    if any(not hasattr(os, value) for value in required_os) or any(
        not hasattr(fcntl, value) for value in required_fcntl
    ):
        raise ValidationError(
            "judge runner requires Linux sealed memfd execution support"
        )
    # Linux UAPI values are stable but Python 3.8 does not expose the newer
    # execute-policy constants.  An older kernel rejects them with EINVAL,
    # which is intentionally fail-closed below.
    mfd_exec = getattr(os, "MFD_EXEC", 0x0010)
    mfd_noexec_seal = getattr(os, "MFD_NOEXEC_SEAL", 0x0008)
    seal_exec = getattr(fcntl, "F_SEAL_EXEC", 0x0020)
    flags = (
        os.MFD_ALLOW_SEALING
        | getattr(os, "MFD_CLOEXEC", 0x0001)
        | (mfd_exec if executable else mfd_noexec_seal)
    )
    fd = None
    try:
        fd = os.memfd_create(name, flags=flags)
        _write_all_fd(fd, payload)
        os.fchmod(fd, mode)
        seals = (
            fcntl.F_SEAL_WRITE
            | fcntl.F_SEAL_GROW
            | fcntl.F_SEAL_SHRINK
            | seal_exec
            | fcntl.F_SEAL_SEAL
        )
        fcntl.fcntl(fd, fcntl.F_ADD_SEALS, seals)
        observed_seals = fcntl.fcntl(fd, fcntl.F_GET_SEALS)
        if (observed_seals & seals) != seals:
            raise OSError("Judge runner memfd seals were not fully applied")
        return fd
    except (OSError, ValueError) as exc:
        try:
            if fd is not None:
                os.close(fd)
        except OSError:
            pass
        raise ValidationError("judge runner bytes could not be sealed") from exc


@contextmanager
def _sealed_runner_command(material: Mapping[str, Any]):
    """Build a fresh sealed executable/source set for exactly one request."""

    runner = copy.deepcopy(material["runner"])
    descriptors = []
    try:
        executable_fd = _sealed_memfd(
            "judge-runner-executable",
            material["executable_payload"],
            0o500,
            executable=True,
        )
        descriptors.append(executable_fd)
        source_mapping = {}
        for index, (binding, payload) in enumerate(material["source_material"]):
            source_fd = _sealed_memfd(
                "judge-runner-source-{}".format(index),
                payload,
                0o400,
                executable=False,
            )
            descriptors.append(source_fd)
            source_mapping[str(Path(binding["path"]).resolve())] = (
                "/proc/self/fd/{}".format(source_fd)
            )
        sealed_argv = []
        for index, argument in enumerate(runner["command"]["argv"]):
            if index == 0:
                sealed_argv.append("/proc/self/fd/{}".format(executable_fd))
            else:
                sealed_argv.append(
                    source_mapping.get(str(Path(argument).resolve()), argument)
                )
        runner["command"]["argv"] = sealed_argv
        yield runner, tuple(descriptors)
    finally:
        for fd in descriptors:
            try:
                os.close(fd)
            except OSError:
                pass


def _frozen_judge_context(
    response_records: Iterable[Mapping[str, Any]],
    evidence_records: Iterable[Mapping[str, Any]],
    manifest: Mapping[str, Any],
) -> Dict[str, Any]:
    responses_list = list(response_records)
    evidence_list = list(evidence_records)
    validate_schema_records(
        responses_list, "response_record", context="judge source response"
    )
    validate_schema_records(
        evidence_list, "semantic_evidence", context="judge source evidence"
    )
    responses = {record.get("run_id"): record for record in responses_list}
    evidence = {record.get("run_id"): record for record in evidence_list}
    if (
        None in responses
        or None in evidence
        or len(responses) != len(responses_list)
        or len(evidence) != len(evidence_list)
        or set(responses) != set(evidence)
    ):
        raise ValidationError(
            "judge requests require identical unique response/evidence run IDs"
        )

    rubric_asset = _unique_asset(manifest, "judge_rubric")
    runner_material = _load_frozen_judge_runner_material(manifest)
    runner_asset = runner_material["asset"]
    runner = runner_material["runner"]
    rubric = read_json(Path(rubric_asset["path"]))
    validate_schema_instance(rubric, "judge_rubric", context="judge rubric")
    if rubric.get("runner_manifest_sha256") != runner_asset["sha256"]:
        raise ValidationError("judge rubric is not bound to the frozen runner")
    prompt_payload = _validate_file_binding(
        rubric["prompt"], "judge prompt", require_nonempty=True
    )
    try:
        prompt_text = prompt_payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError("judge prompt is not readable UTF-8") from exc
    output_payload = _validate_file_binding(
        rubric["output_schema"], "judge output schema", require_nonempty=True
    )
    output_schema = strict_json_object_bytes(output_payload, "judge output schema")
    if canonical_json_bytes(output_schema) != canonical_json_bytes(
        load_schema("judge_output")
    ):
        raise ValidationError("judge output schema differs from the registered contract")
    config_sha256 = judge_config_sha256(
        model_id=rubric["model_id"],
        prompt_sha256=rubric["prompt"]["sha256"],
        inference_config=rubric["inference_config"],
        output_schema_sha256=rubric["output_schema"]["sha256"],
        request_contract=rubric["request_contract"],
        runner_manifest_sha256=runner_asset["sha256"],
    )
    if rubric.get("judge_config_sha256") != config_sha256:
        raise ValidationError("judge rubric config hash mismatch")

    library_asset = _unique_asset(manifest, "query_library_test")
    oracle_asset = _unique_asset(manifest, "requirement_oracle_test")
    query_records = read_jsonl(Path(library_asset["path"]))
    oracle_records = read_jsonl(Path(oracle_asset["path"]))
    query_by_id = {record.get("query_id"): record for record in query_records}
    oracle_by_id = {record.get("query_id"): record for record in oracle_records}
    if (
        None in query_by_id
        or None in oracle_by_id
        or len(query_by_id) != len(query_records)
        or len(oracle_by_id) != len(oracle_records)
    ):
        raise ValidationError("frozen Judge query/oracle IDs must be unique")

    items = []
    for run_id, response in responses.items():
        evidence_record = evidence[run_id]
        query_id = response.get("query_id")
        if (
            query_id not in query_by_id
            or query_id not in oracle_by_id
            or evidence_record.get("query_id") != query_id
            or evidence_record.get("platform") != response.get("platform")
        ):
            raise ValidationError("judge source references an inconsistent frozen query")
        if response.get("expected_support") != "supported":
            continue
        response_sha256 = _record_sha256(response)
        evidence_sha256 = _record_sha256(evidence_record)
        for atom in oracle_by_id[query_id].get("atoms", []):
            if not (
                atom.get("decision_status") == "confirmed"
                and atom.get("layer")
                in ("core_required", "surface_required", "forbidden")
                and deterministic_atom_verdict(atom, evidence_record) == "unknown"
            ):
                continue
            artifact = response.get("artifact")
            if not isinstance(artifact, Mapping):
                raise ValidationError("judge-routed response lacks an artifact")
            artifact_payload = _validate_file_binding(
                artifact, "judge source artifact", require_nonempty=True
            )
            try:
                artifact_text = artifact_payload.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValidationError(
                    "judge source artifact is not readable UTF-8"
                ) from exc
            payload = judge_request_payload(
                model_id=rubric["model_id"],
                judge_config_sha256_value=config_sha256,
                inference_config=rubric["inference_config"],
                prompt_text=prompt_text,
                query_text=query_by_id[query_id]["query_text"],
                platform=response["platform"],
                artifact_sha256=artifact["sha256"],
                artifact_text=artifact_text,
                source_response_sha256=response_sha256,
                source_evidence_sha256=evidence_sha256,
                evidence=evidence_record,
                oracle_atom=atom,
                output_schema=output_schema,
                request_contract=rubric["request_contract"],
            )
            payload_bytes = canonical_json_bytes(payload)
            request_sha256 = sha256_bytes(payload_bytes)
            items.append(
                {
                    "item_id": judge_item_id(
                        response_sha256, evidence_sha256, atom["atom_id"]
                    ),
                    "query_id": query_id,
                    "run_id": run_id,
                    "atom_id": atom["atom_id"],
                    "platform": response["platform"],
                    "model_id": rubric["model_id"],
                    "judge_config_sha256": config_sha256,
                    "judge_request_sha256": request_sha256,
                    "payload": payload_bytes,
                    "source_response_sha256": response_sha256,
                    "source_evidence_sha256": evidence_sha256,
                    "source_artifact_sha256": artifact["sha256"],
                }
            )
    items.sort(key=lambda item: item["item_id"])
    item_ids = [item["item_id"] for item in items]
    pairs = [(item["run_id"], item["atom_id"]) for item in items]
    if len(item_ids) != len(set(item_ids)) or len(pairs) != len(set(pairs)):
        raise ValidationError("judge request routing produced duplicate items")
    return {
        "responses": responses_list,
        "evidence": evidence_list,
        "rubric_asset": rubric_asset,
        "runner_asset": runner_asset,
        "runner": runner,
        "runner_material": runner_material,
        "items": items,
    }


def _request_record(item: Mapping[str, Any], bundle_directory: Path) -> Dict[str, Any]:
    request_path = Path(bundle_directory) / "requests" / "{}.json".format(
        item["judge_request_sha256"]
    )
    return {
        "schema_version": "0.1",
        "item_id": item["item_id"],
        "query_id": item["query_id"],
        "run_id": item["run_id"],
        "atom_id": item["atom_id"],
        "platform": item["platform"],
        "model_id": item["model_id"],
        "judge_config_sha256": item["judge_config_sha256"],
        "judge_request_sha256": item["judge_request_sha256"],
        "raw_judge_request": _binding_for(request_path, item["payload"]),
        "provenance": {
            "source_response_sha256": item["source_response_sha256"],
            "source_evidence_sha256": item["source_evidence_sha256"],
            "source_artifact_sha256": item["source_artifact_sha256"],
            "judge_rubric_sha256": item["rubric_asset_sha256"],
            "runner_manifest_sha256": item["runner_asset_sha256"],
            "freeze_manifest_sha256": item["freeze_manifest_sha256"],
        },
    }


def create_judge_request_bundle(
    response_records: Iterable[Mapping[str, Any]],
    evidence_records: Iterable[Mapping[str, Any]],
    manifest: Mapping[str, Any],
    output_directory: Path,
) -> Mapping[str, Any]:
    """Publish every and only deterministic-unknown Judge request atomically."""

    context = _frozen_judge_context(response_records, evidence_records, manifest)
    final_directory = Path(output_directory).resolve()
    enriched_items = []
    for item in context["items"]:
        enriched_items.append(
            {
                **item,
                "rubric_asset_sha256": context["rubric_asset"]["sha256"],
                "runner_asset_sha256": context["runner_asset"]["sha256"],
                "freeze_manifest_sha256": manifest["manifest_sha256"],
            }
        )
    records = [_request_record(item, final_directory) for item in enriched_items]
    validate_schema_records(records, "judge_request_record", context="judge request")
    index_payload = jsonl_bytes(records)
    request_manifest = {
        "schema_version": "0.1",
        "result_kind": "judge_request_bundle",
        "request_count": len(records),
        "request_index_sha256": sha256_bytes(index_payload),
        "responses_sha256": _records_sha256(context["responses"]),
        "evidence_sha256": _records_sha256(context["evidence"]),
        "judge_rubric_sha256": context["rubric_asset"]["sha256"],
        "runner_manifest_sha256": context["runner_asset"]["sha256"],
        "freeze_manifest_sha256": manifest["manifest_sha256"],
    }
    request_manifest["manifest_sha256"] = _manifest_sha256(request_manifest)
    validate_schema_instance(
        request_manifest,
        "judge_request_bundle_manifest",
        context="judge request bundle manifest",
    )
    with AtomicBundle(final_directory, "judge request") as staging:
        written_requests = set()
        for item in enriched_items:
            relative = Path("requests") / "{}.json".format(
                item["judge_request_sha256"]
            )
            if relative in written_requests:
                continue
            write_staged_bytes(staging, relative, item["payload"])
            written_requests.add(relative)
        write_staged_bytes(staging, Path("request_index.jsonl"), index_payload)
        write_staged_bytes(
            staging,
            Path("request_manifest.json"),
            canonical_json_bytes(request_manifest) + b"\n",
        )
        expected_files = {
            Path("request_index.jsonl"),
            Path("request_manifest.json"),
        }
        expected_files.update(
            Path("requests") / "{}.json".format(item["judge_request_sha256"])
            for item in enriched_items
        )
        validate_exact_bundle_inventory(staging, expected_files)
    return request_manifest


def validate_judge_request_bundle(
    request_directory: Path,
    response_records: Iterable[Mapping[str, Any]],
    evidence_records: Iterable[Mapping[str, Any]],
    manifest: Mapping[str, Any],
) -> Tuple[Mapping[str, Any], List[Mapping[str, Any]], Mapping[str, Any]]:
    """Reconstruct and validate one immutable request bundle from frozen sources."""

    request_directory = Path(os.path.abspath(str(request_directory)))
    context = _frozen_judge_context(response_records, evidence_records, manifest)
    expected_files = {
        Path("request_index.jsonl"),
        Path("request_manifest.json"),
    }
    expected_files.update(
        Path("requests") / "{}.json".format(item["judge_request_sha256"])
        for item in context["items"]
    )
    validate_exact_bundle_inventory(request_directory, expected_files)
    request_manifest_path = request_directory / "request_manifest.json"
    request_index_path = request_directory / "request_index.jsonl"
    request_manifest = read_json(request_manifest_path)
    records = read_jsonl(request_index_path)
    validate_schema_instance(
        request_manifest,
        "judge_request_bundle_manifest",
        context="judge request bundle manifest",
    )
    validate_schema_records(records, "judge_request_record", context="judge request")
    enriched_items = [
        {
            **item,
            "rubric_asset_sha256": context["rubric_asset"]["sha256"],
            "runner_asset_sha256": context["runner_asset"]["sha256"],
            "freeze_manifest_sha256": manifest["manifest_sha256"],
        }
        for item in context["items"]
    ]
    expected_records = [
        _request_record(item, request_directory) for item in enriched_items
    ]
    if canonical_json_bytes(records) != canonical_json_bytes(expected_records):
        raise ValidationError("judge request index differs from frozen source routing")
    for record, item in zip(records, enriched_items):
        payload = _validate_file_binding(
            record["raw_judge_request"], "judge request payload", require_nonempty=True
        )
        if payload != item["payload"]:
            raise ValidationError("judge request payload differs from its canonical request")
    index_payload = request_index_path.read_bytes()
    expected_manifest = {
        "schema_version": "0.1",
        "result_kind": "judge_request_bundle",
        "request_count": len(records),
        "request_index_sha256": sha256_bytes(index_payload),
        "responses_sha256": _records_sha256(context["responses"]),
        "evidence_sha256": _records_sha256(context["evidence"]),
        "judge_rubric_sha256": context["rubric_asset"]["sha256"],
        "runner_manifest_sha256": context["runner_asset"]["sha256"],
        "freeze_manifest_sha256": manifest["manifest_sha256"],
    }
    expected_manifest["manifest_sha256"] = _manifest_sha256(expected_manifest)
    if canonical_json_bytes(request_manifest) != canonical_json_bytes(expected_manifest):
        raise ValidationError("judge request bundle manifest is inconsistent")
    return request_manifest, records, context


def build_judge_calibration_context_manifest(
    *,
    query_library_path: Path,
    requirement_oracle_path: Path,
    calibration_roster_path: Path,
    calibration_human_gold_path: Path,
    prompt_path: Path,
    output_schema_path: Path,
    runner_manifest_path: Path,
    development_sources: Sequence[Mapping[str, Any]],
    model_id: str,
    inference_config: Mapping[str, Any],
    request_contract: str = "judge_atom_v0.1",
    calibration_profile: str = "cross_platform_final",
) -> Mapping[str, Any]:
    """Build the content-bound context that must exist before calibration runs."""

    runner_binding = _binding_from_file(
        runner_manifest_path, "Judge calibration runner manifest"
    )
    prompt_binding = _binding_from_file(prompt_path, "Judge calibration prompt")
    output_binding = _binding_from_file(
        output_schema_path, "Judge calibration output schema"
    )
    normalized_sources = []
    for source in sorted(development_sources, key=lambda value: value["platform"]):
        normalized_sources.append(
            {
                "platform": source["platform"],
                "source_config": _binding_from_file(
                    source["source_config"], "Judge calibration source config"
                ),
                "query_roster": _binding_from_file(
                    source["query_roster"], "Judge calibration source roster"
                ),
                "response_records": _binding_from_file(
                    source["response_records"], "Judge calibration source responses"
                ),
                "evidence_records": _binding_from_file(
                    source["evidence_records"], "Judge calibration source evidence"
                ),
            }
        )
    context = {
        "schema_version": "0.1",
        "decision_status": "confirmed",
        "context_kind": "judge_calibration_pre_run_context",
        "calibration_profile": calibration_profile,
        "formal_freeze_eligible": calibration_profile == "cross_platform_final",
        "model_id": model_id,
        "inference_config": copy.deepcopy(dict(inference_config)),
        "request_contract": request_contract,
        "judge_config_sha256": judge_config_sha256(
            model_id=model_id,
            prompt_sha256=prompt_binding["sha256"],
            inference_config=inference_config,
            output_schema_sha256=output_binding["sha256"],
            request_contract=request_contract,
            runner_manifest_sha256=runner_binding["sha256"],
        ),
        "query_library_dev": _binding_from_file(
            query_library_path, "Judge calibration query library"
        ),
        "requirement_oracle_dev": _binding_from_file(
            requirement_oracle_path, "Judge calibration requirement oracle"
        ),
        "calibration_roster": _binding_from_file(
            calibration_roster_path, "Judge calibration roster"
        ),
        "calibration_human_gold": _binding_from_file(
            calibration_human_gold_path, "Judge calibration human gold"
        ),
        "prompt": prompt_binding,
        "output_schema": output_binding,
        "runner_manifest": runner_binding,
        "development_sources": normalized_sources,
    }
    context["manifest_sha256"] = _manifest_sha256(context)
    validate_schema_instance(
        context,
        "judge_calibration_context_manifest",
        context="Judge calibration context",
    )
    return context


def _load_judge_calibration_context(context_path: Path) -> Mapping[str, Any]:
    context_path = Path(context_path).resolve()
    if path_has_symlink_component(context_path) or not context_path.is_file():
        raise ValidationError("Judge calibration context is missing or symlinked")
    context = read_json(context_path)
    validate_schema_instance(
        context,
        "judge_calibration_context_manifest",
        context="Judge calibration context",
    )
    if context.get("manifest_sha256") != _manifest_sha256(context):
        raise ValidationError("Judge calibration context hash mismatch")
    bound_payloads = {}
    for field in (
        "query_library_dev",
        "requirement_oracle_dev",
        "calibration_roster",
        "calibration_human_gold",
        "prompt",
        "output_schema",
        "runner_manifest",
    ):
        bound_payloads[field] = _validate_file_binding(
            context[field], "Judge calibration {}".format(field), require_nonempty=True
        )
    output_schema = strict_json_object_bytes(
        bound_payloads["output_schema"], "Judge calibration output schema"
    )
    if canonical_json_bytes(output_schema) != canonical_json_bytes(
        load_schema("judge_output")
    ):
        raise ValidationError("Judge calibration output schema is not registered")
    try:
        prompt_text = bound_payloads["prompt"].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError("Judge calibration prompt is not UTF-8") from exc
    expected_config_sha256 = judge_config_sha256(
        model_id=context["model_id"],
        prompt_sha256=context["prompt"]["sha256"],
        inference_config=context["inference_config"],
        output_schema_sha256=context["output_schema"]["sha256"],
        request_contract=context["request_contract"],
        runner_manifest_sha256=context["runner_manifest"]["sha256"],
    )
    if context["judge_config_sha256"] != expected_config_sha256:
        raise ValidationError("Judge calibration config hash mismatch")
    runner_material = _load_frozen_judge_runner_material(
        {
            "assets": [
                {"role": "judge_runner_manifest", **context["runner_manifest"]}
            ]
        }
    )

    query_records = read_jsonl(Path(context["query_library_dev"]["path"]))
    oracle_records = read_jsonl(Path(context["requirement_oracle_dev"]["path"]))
    validate_schema_records(
        oracle_records, "oracle_record", context="Judge calibration development oracle"
    )
    query_by_id = {record.get("query_id"): record for record in query_records}
    oracle_by_id = {record.get("query_id"): record for record in oracle_records}
    if (
        None in query_by_id
        or None in oracle_by_id
        or len(query_by_id) != len(query_records)
        or len(oracle_by_id) != len(oracle_records)
        or set(query_by_id) != set(oracle_by_id)
    ):
        raise ValidationError("Judge calibration query/oracle IDs are inconsistent")

    sources = {}
    response_by_run = {}
    evidence_by_run = {}
    profile = context["calibration_profile"]
    expected_platforms = (
        {"carla"} if profile == "carla_stage" else {"carla", "metadrive"}
    )
    source_platforms = [
        source["platform"] for source in context["development_sources"]
    ]
    if (
        source_platforms != sorted(source_platforms)
        or set(source_platforms) != expected_platforms
    ):
        raise ValidationError("Judge calibration sources must be sorted and platform-complete")
    for source_binding in context["development_sources"]:
        platform = source_binding["platform"]
        payloads = {}
        for field in (
            "source_config",
            "query_roster",
            "response_records",
            "evidence_records",
        ):
            payloads[field] = _validate_file_binding(
                source_binding[field],
                "Judge calibration {} {}".format(platform, field),
                require_nonempty=True,
            )
        config = strict_json_object_bytes(
            payloads["source_config"], "Judge calibration source config"
        )
        roster = strict_json_object_bytes(
            payloads["query_roster"], "Judge calibration source roster"
        )
        validate_schema_instance(
            config,
            "judge_calibration_source_config",
            context="Judge calibration source config",
        )
        validate_schema_instance(
            roster, "query_roster", context="Judge calibration source roster"
        )
        if config["platform"] != platform or roster["platform"] != platform:
            raise ValidationError("Judge calibration source platform mismatch")
        if roster["library_sha256"] != context["query_library_dev"]["sha256"]:
            raise ValidationError("Judge calibration source roster uses another library")
        _validate_file_binding(
            config["implementation_bundle"],
            "Judge calibration source implementation",
            require_nonempty=True,
        )
        responses = read_jsonl(Path(source_binding["response_records"]["path"]))
        evidence = read_jsonl(Path(source_binding["evidence_records"]["path"]))
        validate_schema_records(
            responses, "response_record", context="Judge calibration source response"
        )
        validate_schema_records(
            evidence, "semantic_evidence", context="Judge calibration source evidence"
        )
        sources[platform] = {
            "source_id": config["source_id"],
            "responses": responses,
            "evidence": evidence,
        }
        for record in responses:
            run_id = record["run_id"]
            if run_id in response_by_run:
                raise ValidationError("Judge calibration response run IDs repeat")
            response_by_run[run_id] = record
        for record in evidence:
            run_id = record["run_id"]
            if run_id in evidence_by_run:
                raise ValidationError("Judge calibration evidence run IDs repeat")
            evidence_by_run[run_id] = record
    if set(response_by_run) != set(evidence_by_run):
        raise ValidationError("Judge calibration response/evidence coverage differs")

    calibration_roster = strict_json_object_bytes(
        bound_payloads["calibration_roster"], "Judge calibration roster"
    )
    roster_schema = (
        "judge_calibration_roster"
        if profile == "cross_platform_final"
        else "carla_stage_calibration_roster"
    )
    if roster_schema == "carla_stage_calibration_roster":
        from .human_workflow import validate_human_document

        validate_human_document(calibration_roster, roster_schema)
    else:
        validate_schema_instance(
            calibration_roster,
            roster_schema,
            context="Judge calibration roster",
        )
    gold_path = Path(context["calibration_human_gold"]["path"])
    if gold_path.suffix == ".jsonl":
        gold_records = read_jsonl(gold_path)
    elif gold_path.suffix == ".json" and profile == "carla_stage":
        from .human_workflow import validate_human_document

        gold_document = read_json(gold_path)
        validate_human_document(gold_document, "carla_stage_calibration_report")
        gold_records = gold_document["human_gold_records"]
    else:
        raise ValidationError("Judge calibration human gold must match its profile")
    validate_schema_records(
        gold_records, "judge_calibration", context="Judge calibration human gold"
    )
    gold_by_item = {record["item_id"]: record for record in gold_records}
    roster_by_item = {
        record["item_id"]: record for record in calibration_roster["items"]
    }
    if (
        len(gold_by_item) != len(gold_records)
        or len(roster_by_item) != len(calibration_roster["items"])
        or set(gold_by_item) != set(roster_by_item)
    ):
        raise ValidationError("Judge calibration requires exactly one gold per roster item")

    items = []
    roster_fields = (
        "item_id",
        "platform",
        "source_id",
        "query_id",
        "run_id",
        "repetition",
        "atom_id",
        "category",
        "surface_style",
        "source_response_sha256",
        "source_evidence_sha256",
    )
    for roster_item in calibration_roster["items"]:
        gold = gold_by_item[roster_item["item_id"]]
        if any(gold.get(field) != roster_item.get(field) for field in roster_fields):
            raise ValidationError("Judge calibration gold differs from its roster item")
        if any(
            field in gold
            for field in (
                "judge_response_id",
                "judge_response_record_sha256",
                "predicted_verdict",
            )
        ):
            raise ValidationError("Judge calibration human gold contains machine output")
        response = response_by_run.get(roster_item["run_id"])
        evidence = evidence_by_run.get(roster_item["run_id"])
        query = query_by_id.get(roster_item["query_id"])
        oracle = oracle_by_id.get(roster_item["query_id"])
        if response is None or evidence is None or query is None or oracle is None:
            raise ValidationError("Judge calibration roster references missing sources")
        if (
            response["platform"] != roster_item["platform"]
            or evidence["platform"] != roster_item["platform"]
            or response["query_id"] != roster_item["query_id"]
            or evidence["query_id"] != roster_item["query_id"]
            or sources[roster_item["platform"]]["source_id"]
            != roster_item["source_id"]
        ):
            raise ValidationError("Judge calibration roster/source identity mismatch")
        response_sha256 = _record_sha256(response)
        evidence_sha256 = _record_sha256(evidence)
        if (
            response_sha256 != roster_item["source_response_sha256"]
            or evidence_sha256 != roster_item["source_evidence_sha256"]
            or judge_item_id(
                response_sha256, evidence_sha256, roster_item["atom_id"]
            )
            != roster_item["item_id"]
        ):
            raise ValidationError("Judge calibration roster source hash mismatch")
        oracle_atom = next(
            (
                atom
                for atom in oracle["atoms"]
                if atom["atom_id"] == roster_item["atom_id"]
            ),
            None,
        )
        if (
            oracle_atom is None
            or oracle_atom["category"] != roster_item["category"]
            or oracle["surface_style"] != roster_item["surface_style"]
            or deterministic_atom_verdict(oracle_atom, evidence) != "unknown"
        ):
            raise ValidationError("Judge calibration roster atom is not Judge-routed")
        artifact_payload = _validate_file_binding(
            response["artifact"], "Judge calibration artifact", require_nonempty=True
        )
        try:
            artifact_text = artifact_payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValidationError("Judge calibration artifact is not UTF-8") from exc
        payload = judge_request_payload(
            model_id=context["model_id"],
            judge_config_sha256_value=context["judge_config_sha256"],
            inference_config=context["inference_config"],
            prompt_text=prompt_text,
            query_text=query["query_text"],
            platform=response["platform"],
            artifact_sha256=response["artifact"]["sha256"],
            artifact_text=artifact_text,
            source_response_sha256=response_sha256,
            source_evidence_sha256=evidence_sha256,
            evidence=evidence,
            oracle_atom=oracle_atom,
            output_schema=output_schema,
            request_contract=context["request_contract"],
        )
        payload_bytes = canonical_json_bytes(payload)
        items.append(
            {
                **{field: roster_item[field] for field in roster_fields},
                "model_id": context["model_id"],
                "judge_config_sha256": context["judge_config_sha256"],
                "judge_request_sha256": sha256_bytes(payload_bytes),
                "source_artifact_sha256": response["artifact"]["sha256"],
                "payload": payload_bytes,
            }
        )
    items.sort(key=lambda item: item["item_id"])
    return {
        "context": context,
        "runner_material": runner_material,
        "calibration_roster": calibration_roster,
        "gold_records": gold_records,
        "items": items,
    }


def _calibration_request_record(
    item: Mapping[str, Any],
    request_directory: Path,
    calibration_context: Mapping[str, Any],
) -> Dict[str, Any]:
    request_path = Path(request_directory) / "requests" / "{}.json".format(
        item["judge_request_sha256"]
    )
    return {
        "schema_version": "0.1",
        "item_id": item["item_id"],
        "query_id": item["query_id"],
        "run_id": item["run_id"],
        "atom_id": item["atom_id"],
        "platform": item["platform"],
        "source_id": item["source_id"],
        "repetition": item["repetition"],
        "model_id": item["model_id"],
        "judge_config_sha256": item["judge_config_sha256"],
        "judge_request_sha256": item["judge_request_sha256"],
        "raw_judge_request": _binding_for(request_path, item["payload"]),
        "provenance": {
            "source_response_sha256": item["source_response_sha256"],
            "source_evidence_sha256": item["source_evidence_sha256"],
            "source_artifact_sha256": item["source_artifact_sha256"],
            "calibration_context_sha256": calibration_context["manifest_sha256"],
            "calibration_roster_sha256": calibration_context[
                "calibration_roster"
            ]["sha256"],
            "calibration_human_gold_sha256": calibration_context[
                "calibration_human_gold"
            ]["sha256"],
            "runner_manifest_sha256": calibration_context["runner_manifest"][
                "sha256"
            ],
        },
    }


def create_judge_calibration_request_bundle(
    calibration_context_path: Path, output_directory: Path
) -> Mapping[str, Any]:
    """Materialize the exact precommitted calibration requests atomically."""

    loaded = _load_judge_calibration_context(calibration_context_path)
    context = loaded["context"]
    final_directory = Path(output_directory).resolve()
    records = [
        _calibration_request_record(item, final_directory, context)
        for item in loaded["items"]
    ]
    validate_schema_records(
        records,
        "judge_calibration_request_record",
        context="Judge calibration request",
    )
    index_payload = jsonl_bytes(records)
    request_manifest = {
        "schema_version": "0.1",
        "result_kind": "judge_calibration_request_bundle",
        "calibration_profile": context["calibration_profile"],
        "formal_freeze_eligible": context["formal_freeze_eligible"],
        "request_count": len(records),
        "request_index_sha256": sha256_bytes(index_payload),
        "calibration_context_sha256": context["manifest_sha256"],
        "calibration_roster_sha256": context["calibration_roster"]["sha256"],
        "calibration_human_gold_sha256": context["calibration_human_gold"][
            "sha256"
        ],
        "runner_manifest_sha256": context["runner_manifest"]["sha256"],
    }
    request_manifest["manifest_sha256"] = _manifest_sha256(request_manifest)
    validate_schema_instance(
        request_manifest,
        "judge_calibration_request_bundle_manifest",
        context="Judge calibration request bundle",
    )
    with AtomicBundle(final_directory, "Judge calibration request") as staging:
        for item in loaded["items"]:
            write_staged_bytes(
                staging,
                Path("requests") / "{}.json".format(item["judge_request_sha256"]),
                item["payload"],
            )
        write_staged_bytes(staging, Path("request_index.jsonl"), index_payload)
        write_staged_bytes(
            staging,
            Path("request_manifest.json"),
            canonical_json_bytes(request_manifest) + b"\n",
        )
        expected_files = {
            Path("request_index.jsonl"),
            Path("request_manifest.json"),
        }
        expected_files.update(
            Path("requests") / "{}.json".format(item["judge_request_sha256"])
            for item in loaded["items"]
        )
        validate_exact_bundle_inventory(staging, expected_files)
    return request_manifest


def validate_judge_calibration_request_bundle(
    request_directory: Path, calibration_context_path: Path
) -> Tuple[Mapping[str, Any], List[Mapping[str, Any]], Mapping[str, Any]]:
    """Reconstruct every calibration request from the pre-run context."""

    request_directory = Path(os.path.abspath(str(request_directory)))
    loaded = _load_judge_calibration_context(calibration_context_path)
    context = loaded["context"]
    expected_files = {
        Path("request_index.jsonl"),
        Path("request_manifest.json"),
    }
    expected_files.update(
        Path("requests") / "{}.json".format(item["judge_request_sha256"])
        for item in loaded["items"]
    )
    validate_exact_bundle_inventory(request_directory, expected_files)
    records = read_jsonl(request_directory / "request_index.jsonl")
    manifest = read_json(request_directory / "request_manifest.json")
    validate_schema_records(
        records,
        "judge_calibration_request_record",
        context="Judge calibration request",
    )
    validate_schema_instance(
        manifest,
        "judge_calibration_request_bundle_manifest",
        context="Judge calibration request bundle",
    )
    expected_records = [
        _calibration_request_record(item, request_directory, context)
        for item in loaded["items"]
    ]
    if canonical_json_bytes(records) != canonical_json_bytes(expected_records):
        raise ValidationError("Judge calibration requests differ from pre-run context")
    for record, item in zip(records, loaded["items"]):
        if _validate_file_binding(
            record["raw_judge_request"],
            "Judge calibration request payload",
            require_nonempty=True,
        ) != item["payload"]:
            raise ValidationError("Judge calibration request payload is not canonical")
    index_payload = (request_directory / "request_index.jsonl").read_bytes()
    expected_manifest = {
        "schema_version": "0.1",
        "result_kind": "judge_calibration_request_bundle",
        "calibration_profile": context["calibration_profile"],
        "formal_freeze_eligible": context["formal_freeze_eligible"],
        "request_count": len(records),
        "request_index_sha256": sha256_bytes(index_payload),
        "calibration_context_sha256": context["manifest_sha256"],
        "calibration_roster_sha256": context["calibration_roster"]["sha256"],
        "calibration_human_gold_sha256": context["calibration_human_gold"][
            "sha256"
        ],
        "runner_manifest_sha256": context["runner_manifest"]["sha256"],
    }
    expected_manifest["manifest_sha256"] = _manifest_sha256(expected_manifest)
    if canonical_json_bytes(manifest) != canonical_json_bytes(expected_manifest):
        raise ValidationError("Judge calibration request manifest is inconsistent")
    return manifest, records, loaded


def _contains_secret(payload: bytes, secret_values: Sequence[bytes]) -> bool:
    return any(secret and secret in payload for secret in secret_values)


def _terminate_process_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        try:
            process.terminate()
        except OSError:
            return
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            process.kill()
        except OSError:
            pass


def _execute_runner(
    runner: Mapping[str, Any],
    request_payload: bytes,
    stdout_path: Path,
    stderr_path: Path,
    workspace_directory: Path,
    *,
    pass_fds: Sequence[int] = (),
) -> Tuple[bytes, bytes]:
    command = runner["command"]
    if len(request_payload) > command["max_request_bytes"]:
        raise ValidationError("judge request exceeds the frozen byte limit")
    credential_names = command["credential_env_names"]
    home_directory = workspace_directory / "home"
    temporary_directory = workspace_directory / "tmp"
    current_directory = workspace_directory / "cwd"
    for directory in (workspace_directory, home_directory, temporary_directory, current_directory):
        directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    environment = {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "HOME": str(home_directory),
        "TMPDIR": str(temporary_directory),
    }
    secret_values = []
    for name in credential_names:
        value = os.environ.get(name)
        if value is None or not value:
            raise ValidationError("required judge credential environment variable is absent")
        environment[name] = value
        secret_values.append(value.encode("utf-8"))
    if _contains_secret(request_payload, secret_values):
        raise ValidationError("judge request contains a credential value")
    deadline = time.monotonic() + command["timeout_seconds"]
    try:
        process = subprocess.Popen(
            command["argv"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(current_directory),
            env=environment,
            start_new_session=True,
            pass_fds=tuple(pass_fds),
        )
    except (OSError, ValueError) as exc:
        raise ValidationError("judge runner process could not be executed") from exc
    captured = {"stdout": bytearray(), "stderr": bytearray()}
    overflow = []
    overflow_lock = threading.Lock()
    stdin_errors = []

    def read_stream(name, stream, limit):
        total = 0
        try:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    return
                total += len(chunk)
                remaining = limit - len(captured[name])
                if remaining > 0:
                    captured[name].extend(chunk[:remaining])
                if total > limit:
                    with overflow_lock:
                        if not overflow:
                            overflow.append(name)
        finally:
            stream.close()

    readers = (
        threading.Thread(
            target=read_stream,
            args=("stdout", process.stdout, command["max_stdout_bytes"]),
            daemon=True,
        ),
        threading.Thread(
            target=read_stream,
            args=("stderr", process.stderr, command["max_stderr_bytes"]),
            daemon=True,
        ),
    )
    for reader in readers:
        reader.start()
    timed_out = False

    def write_stdin():
        try:
            process.stdin.write(request_payload)
            process.stdin.close()
        except (BrokenPipeError, OSError):
            try:
                process.stdin.close()
            except OSError:
                pass
        except Exception as exc:  # pragma: no cover - defensive OS stream guard
            stdin_errors.append(exc)
            try:
                process.stdin.close()
            except OSError:
                pass

    writer = None
    if time.monotonic() >= deadline:
        timed_out = True
        try:
            process.stdin.close()
        except OSError:
            pass
        _terminate_process_group(process)
    else:
        writer = threading.Thread(target=write_stdin, daemon=True)
        writer.start()
    while process.poll() is None:
        if overflow:
            _terminate_process_group(process)
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            _terminate_process_group(process)
            break
        try:
            process.wait(timeout=min(0.05, remaining))
        except subprocess.TimeoutExpired:
            pass
    if not timed_out and not overflow:
        _terminate_process_group(process)
    for reader in readers:
        reader.join(timeout=2.0)
    if writer is not None:
        writer.join(timeout=2.0)
    if any(reader.is_alive() for reader in readers) or (
        writer is not None and writer.is_alive()
    ):
        raise ValidationError("judge runner streams did not close")
    stdout = bytes(captured["stdout"])
    stderr = bytes(captured["stderr"])
    if timed_out:
        raise ValidationError("judge runner timed out")
    if overflow:
        raise ValidationError("judge runner exceeded a frozen output byte limit")
    if stdin_errors:
        raise ValidationError("judge runner stdin writer failed")
    if _contains_secret(stdout, secret_values) or _contains_secret(stderr, secret_values):
        raise ValidationError("judge runner output contains a credential value")
    if process.returncode != 0:
        raise ValidationError("judge runner exited unsuccessfully")
    try:
        raw_response = stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError("judge runner stdout is not UTF-8") from exc
    parse_raw_judge_response(raw_response)
    for path, payload in ((stdout_path, stdout), (stderr_path, stderr)):
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    return stdout, stderr


def _run_validated_request_records(
    request_manifest_path: Path,
    request_records: Sequence[Mapping[str, Any]],
    runner_material: Mapping[str, Any],
    output_directory: Path,
) -> Mapping[str, Any]:
    """Execute prevalidated requests through the one shared raw-process chain."""

    request_manifest_path = Path(request_manifest_path).resolve()
    output_directory = Path(output_directory).resolve()
    runner_sha256 = runner_material["asset"]["sha256"]
    created_at = _utc_now()
    execution_records = []
    response_outputs = []
    workspace_manager = _runner_execution_workspace(runner_material)
    with workspace_manager as (runner_metadata, work_root), AtomicBundle(
        output_directory, "judge run"
    ) as staging:
        for request_record in request_records:
            item_id = request_record["item_id"]
            item_relative = Path("items") / item_id
            request_payload = _validate_file_binding(
                request_record["raw_judge_request"],
                "judge request payload",
                require_nonempty=True,
            )
            request_path = staging / item_relative / "request.json"
            stdout_path = staging / item_relative / "stdout.bin"
            stderr_path = staging / item_relative / "stderr.bin"
            request_path.parent.mkdir(parents=True, exist_ok=True)
            write_staged_bytes(staging, item_relative / "request.json", request_payload)
            with _sealed_runner_command(runner_material) as (runner, runner_fds):
                stdout, stderr = _execute_runner(
                    runner,
                    request_payload,
                    stdout_path,
                    stderr_path,
                    work_root / item_id,
                    pass_fds=runner_fds,
                )
            execution_id = sha256_bytes(
                canonical_json_bytes(
                    {
                        "item_id": item_id,
                        "judge_request_sha256": request_record[
                            "judge_request_sha256"
                        ],
                        "runner_manifest_sha256": runner_sha256,
                        "attempt": 0,
                    }
                )
            )
            final_item_directory = output_directory / item_relative
            request_binding = _binding_for(
                final_item_directory / "request.json", request_payload
            )
            stdout_binding = _binding_for(final_item_directory / "stdout.bin", stdout)
            stderr_binding = _binding_for(final_item_directory / "stderr.bin", stderr)
            envelope = {
                "execution_id": execution_id,
                "item_id": item_id,
                "judge_request_sha256": request_record["judge_request_sha256"],
                "request_record_sha256": _record_sha256(request_record),
                "attempt": 0,
                "runner_manifest_sha256": runner_sha256,
                "terminal_status": "complete",
                "exit_code": 0,
                "timed_out": False,
                "raw_judge_request": request_binding,
                "stdout": stdout_binding,
                "stderr": stderr_binding,
            }
            process_envelope_sha256 = _record_sha256(envelope)
            execution_record = {
                "schema_version": "0.1",
                "execution_id": execution_id,
                "item_id": item_id,
                "query_id": request_record["query_id"],
                "run_id": request_record["run_id"],
                "atom_id": request_record["atom_id"],
                "judge_request_sha256": request_record["judge_request_sha256"],
                "request_record_sha256": _record_sha256(request_record),
                "attempt": 0,
                "runner_id": runner_metadata["runner_id"],
                "runner_version": runner_metadata["runner_version"],
                "runner_manifest_sha256": runner_sha256,
                "terminal_status": "complete",
                "exit_code": 0,
                "timed_out": False,
                "raw_judge_request": request_binding,
                "stdout": stdout_binding,
                "stderr": stderr_binding,
                "process_envelope_sha256": process_envelope_sha256,
                "created_at_utc": created_at,
            }
            validate_schema_instance(
                execution_record,
                "judge_execution_record",
                context="judge execution",
            )
            raw_response = stdout.decode("utf-8")
            response_record = {
                "schema_version": "0.1",
                "judge_response_id": judge_response_id(
                    request_record["judge_request_sha256"], attempt=0
                ),
                "item_id": item_id,
                "query_id": request_record["query_id"],
                "run_id": request_record["run_id"],
                "atom_id": request_record["atom_id"],
                "attempt": 0,
                "model_id": request_record["model_id"],
                "judge_config_sha256": request_record["judge_config_sha256"],
                "judge_request_sha256": request_record["judge_request_sha256"],
                "terminal_status": "complete",
                "raw_response": raw_response,
                "raw_response_sha256": sha256_bytes(stdout),
                "provenance": {
                    "runner_id": runner["runner_id"],
                    "runner_version": runner["runner_version"],
                    "runner_manifest_sha256": runner_sha256,
                    "execution_id": execution_id,
                    "execution_record_sha256": _record_sha256(execution_record),
                    "request_record_sha256": _record_sha256(request_record),
                    "process_envelope_sha256": process_envelope_sha256,
                    "provider_request_id": None,
                    "provider_response_id": None,
                    "created_at_utc": created_at,
                },
            }
            validate_schema_instance(
                response_record, "judge_response_record", context="judge response"
            )
            write_staged_bytes(
                staging,
                item_relative / "execution.json",
                canonical_json_bytes(execution_record) + b"\n",
            )
            execution_records.append(execution_record)
            response_outputs.append(response_record)
        execution_payload = jsonl_bytes(execution_records)
        response_payload = jsonl_bytes(response_outputs)
        request_manifest_payload = request_manifest_path.read_bytes()
        run_manifest = {
            "schema_version": "0.1",
            "result_kind": "judge_run_bundle",
            "terminal_status": "complete",
            "request_count": len(request_records),
            "execution_count": len(execution_records),
            "response_count": len(response_outputs),
            "request_manifest_sha256": sha256_bytes(request_manifest_payload),
            "request_manifest": _binding_for(
                request_manifest_path, request_manifest_payload
            ),
            "runner_manifest_sha256": runner_sha256,
            "execution_index_sha256": sha256_bytes(execution_payload),
            "judge_responses_sha256": sha256_bytes(response_payload),
            "created_at_utc": created_at,
        }
        run_manifest["manifest_sha256"] = _manifest_sha256(run_manifest)
        validate_schema_instance(
            run_manifest, "judge_run_manifest", context="judge run manifest"
        )
        write_staged_bytes(staging, Path("execution_index.jsonl"), execution_payload)
        write_staged_bytes(staging, Path("judge_responses.jsonl"), response_payload)
        write_staged_bytes(
            staging,
            Path("judge_run_manifest.json"),
            canonical_json_bytes(run_manifest) + b"\n",
        )
        expected_files = {
            Path("execution_index.jsonl"),
            Path("judge_responses.jsonl"),
            Path("judge_run_manifest.json"),
        }
        for request_record in request_records:
            item_directory = Path("items") / request_record["item_id"]
            expected_files.update(
                {
                    item_directory / "request.json",
                    item_directory / "stdout.bin",
                    item_directory / "stderr.bin",
                    item_directory / "execution.json",
                }
            )
        validate_exact_bundle_inventory(staging, expected_files)
    return run_manifest


def run_judge_request_bundle(
    request_directory: Path,
    response_records: Iterable[Mapping[str, Any]],
    evidence_records: Iterable[Mapping[str, Any]],
    manifest: Mapping[str, Any],
    output_directory: Path,
    *,
    resume: bool = False,
) -> Mapping[str, Any]:
    """Execute the frozen runner once per canonical request and publish atomically."""

    response_records = list(response_records)
    evidence_records = list(evidence_records)
    _, request_records, context = validate_judge_request_bundle(
        request_directory, response_records, evidence_records, manifest
    )
    output_directory = Path(output_directory).resolve()
    if resume and output_directory.exists():
        run_manifest, _ = validate_judge_run_bundle(
            output_directory,
            response_records,
            evidence_records,
            manifest,
            request_directory=request_directory,
        )
        return run_manifest
    return _run_validated_request_records(
        Path(request_directory).resolve() / "request_manifest.json",
        request_records,
        context["runner_material"],
        output_directory,
    )


def run_judge_calibration_request_bundle(
    request_directory: Path,
    calibration_context_path: Path,
    output_directory: Path,
    *,
    resume: bool = False,
) -> Mapping[str, Any]:
    """Execute a pre-run calibration bundle through the formal Judge chain."""

    _, request_records, loaded = validate_judge_calibration_request_bundle(
        request_directory, calibration_context_path
    )
    output_directory = Path(output_directory).resolve()
    if resume and output_directory.exists():
        run_manifest, _ = validate_judge_calibration_run_bundle(
            output_directory,
            calibration_context_path,
            request_directory=request_directory,
        )
        return run_manifest
    return _run_validated_request_records(
        Path(request_directory).resolve() / "request_manifest.json",
        request_records,
        loaded["runner_material"],
        output_directory,
    )


def _validate_run_for_requests(
    run_directory: Path,
    request_manifest_path: Path,
    requests: Sequence[Mapping[str, Any]],
    runner_material: Mapping[str, Any],
) -> Tuple[Mapping[str, Any], List[Mapping[str, Any]]]:
    """Validate exact coverage and raw bytes for any prevalidated request bundle."""

    run_directory = Path(os.path.abspath(str(run_directory)))
    request_manifest_path = Path(request_manifest_path).resolve()
    run_manifest_path = run_directory / "judge_run_manifest.json"
    if path_has_symlink_component(run_manifest_path) or not run_manifest_path.is_file():
        raise ValidationError("Judge run manifest is missing or symlinked")
    initial_manifest = read_json(run_manifest_path)
    validate_schema_instance(
        initial_manifest, "judge_run_manifest", context="judge run manifest"
    )
    request_manifest_payload = _validate_file_binding(
        initial_manifest["request_manifest"],
        "Judge source request manifest",
        require_nonempty=True,
    )
    bound_request_manifest_path = Path(initial_manifest["request_manifest"]["path"])
    if bound_request_manifest_path.resolve() != request_manifest_path:
        raise ValidationError("Judge run is bound to a different request bundle")
    if bound_request_manifest_path.name != "request_manifest.json":
        raise ValidationError("Judge request manifest binding has an invalid filename")
    expected_files = {
        Path("execution_index.jsonl"),
        Path("judge_responses.jsonl"),
        Path("judge_run_manifest.json"),
    }
    for request_record in requests:
        item_directory = Path("items") / request_record["item_id"]
        expected_files.update(
            {
                item_directory / "request.json",
                item_directory / "stdout.bin",
                item_directory / "stderr.bin",
                item_directory / "execution.json",
            }
        )
    validate_exact_bundle_inventory(run_directory, expected_files)
    run_manifest = initial_manifest
    executions = read_jsonl(run_directory / "execution_index.jsonl")
    responses = read_jsonl(run_directory / "judge_responses.jsonl")
    validate_schema_instance(
        run_manifest, "judge_run_manifest", context="judge run manifest"
    )
    validate_schema_records(
        executions, "judge_execution_record", context="judge execution"
    )
    validate_schema_records(
        responses, "judge_response_record", context="judge response"
    )
    if run_manifest.get("terminal_status") != "complete":
        raise ValidationError("formal Judge run is not complete")
    if not (
        len(requests)
        == len(executions)
        == len(responses)
        == run_manifest.get("request_count")
        == run_manifest.get("execution_count")
        == run_manifest.get("response_count")
    ):
        raise ValidationError("Judge run does not exactly cover its request bundle")
    request_by_item = {record["item_id"]: record for record in requests}
    execution_by_item = {record["item_id"]: record for record in executions}
    response_by_item = {record["item_id"]: record for record in responses}
    expected_items = set(request_by_item)
    if (
        len(request_by_item) != len(requests)
        or len(execution_by_item) != len(executions)
        or len(response_by_item) != len(responses)
        or set(execution_by_item) != expected_items
        or set(response_by_item) != expected_items
    ):
        raise ValidationError("Judge run contains duplicate, missing, or orphan items")
    runner = runner_material["runner"]
    runner_sha256 = runner_material["asset"]["sha256"]
    for item_id in sorted(expected_items):
        request_record = request_by_item[item_id]
        execution = execution_by_item[item_id]
        response = response_by_item[item_id]
        final_item_directory = run_directory / "items" / item_id
        expected_execution_id = sha256_bytes(
            canonical_json_bytes(
                {
                    "item_id": item_id,
                    "judge_request_sha256": request_record[
                        "judge_request_sha256"
                    ],
                    "runner_manifest_sha256": runner_sha256,
                    "attempt": 0,
                }
            )
        )
        request_payload = _validate_file_binding(
            execution["raw_judge_request"],
            "Judge execution request",
            require_nonempty=True,
        )
        stdout = _validate_file_binding(
            execution["stdout"], "Judge execution stdout", require_nonempty=True
        )
        stderr = _validate_file_binding(execution["stderr"], "Judge execution stderr")
        expected_paths = {
            "raw_judge_request": final_item_directory / "request.json",
            "stdout": final_item_directory / "stdout.bin",
            "stderr": final_item_directory / "stderr.bin",
        }
        for field, expected_path in expected_paths.items():
            if Path(execution[field]["path"]).resolve() != expected_path.resolve():
                raise ValidationError("Judge execution binding escapes its item directory")
        if request_payload != _validate_file_binding(
            request_record["raw_judge_request"],
            "Judge source request",
            require_nonempty=True,
        ):
            raise ValidationError("Judge execution request differs from its source request")
        envelope = {
            "execution_id": expected_execution_id,
            "item_id": item_id,
            "judge_request_sha256": request_record["judge_request_sha256"],
            "request_record_sha256": _record_sha256(request_record),
            "attempt": 0,
            "runner_manifest_sha256": runner_sha256,
            "terminal_status": "complete",
            "exit_code": 0,
            "timed_out": False,
            "raw_judge_request": execution["raw_judge_request"],
            "stdout": execution["stdout"],
            "stderr": execution["stderr"],
        }
        if (
            execution.get("execution_id") != expected_execution_id
            or execution.get("query_id") != request_record["query_id"]
            or execution.get("run_id") != request_record["run_id"]
            or execution.get("atom_id") != request_record["atom_id"]
            or execution.get("judge_request_sha256")
            != request_record["judge_request_sha256"]
            or execution.get("request_record_sha256")
            != _record_sha256(request_record)
            or execution.get("runner_id") != runner["runner_id"]
            or execution.get("runner_version") != runner["runner_version"]
            or execution.get("runner_manifest_sha256") != runner_sha256
            or execution.get("process_envelope_sha256") != _record_sha256(envelope)
        ):
            raise ValidationError("Judge execution is not bound to its exact request/runner")
        try:
            raw_response = stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValidationError("Judge execution stdout is not UTF-8") from exc
        parse_raw_judge_response(raw_response)
        expected_response_id = judge_response_id(
            request_record["judge_request_sha256"], attempt=0
        )
        expected_provenance = {
            "runner_id": runner["runner_id"],
            "runner_version": runner["runner_version"],
            "runner_manifest_sha256": runner_sha256,
            "execution_id": expected_execution_id,
            "execution_record_sha256": _record_sha256(execution),
            "request_record_sha256": _record_sha256(request_record),
            "process_envelope_sha256": execution["process_envelope_sha256"],
            "provider_request_id": None,
            "provider_response_id": None,
            "created_at_utc": execution["created_at_utc"],
        }
        if (
            response.get("judge_response_id") != expected_response_id
            or response.get("query_id") != request_record["query_id"]
            or response.get("run_id") != request_record["run_id"]
            or response.get("atom_id") != request_record["atom_id"]
            or response.get("model_id") != request_record["model_id"]
            or response.get("judge_config_sha256")
            != request_record["judge_config_sha256"]
            or response.get("judge_request_sha256")
            != request_record["judge_request_sha256"]
            or response.get("raw_response") != raw_response
            or response.get("raw_response_sha256") != sha256_bytes(stdout)
            or canonical_json_bytes(response.get("provenance"))
            != canonical_json_bytes(expected_provenance)
        ):
            raise ValidationError("Judge response is not bound to its raw execution")
        if not stderr and execution["stderr"]["bytes"] != 0:
            raise ValidationError("Judge stderr binding is inconsistent")
    execution_payload = (run_directory / "execution_index.jsonl").read_bytes()
    response_payload = (run_directory / "judge_responses.jsonl").read_bytes()
    expected_manifest = {
        "schema_version": "0.1",
        "result_kind": "judge_run_bundle",
        "terminal_status": "complete",
        "request_count": len(requests),
        "execution_count": len(executions),
        "response_count": len(responses),
        "request_manifest_sha256": sha256_bytes(request_manifest_payload),
        "request_manifest": initial_manifest["request_manifest"],
        "runner_manifest_sha256": runner_sha256,
        "execution_index_sha256": sha256_bytes(execution_payload),
        "judge_responses_sha256": sha256_bytes(response_payload),
        "created_at_utc": run_manifest["created_at_utc"],
    }
    expected_manifest["manifest_sha256"] = _manifest_sha256(expected_manifest)
    if canonical_json_bytes(run_manifest) != canonical_json_bytes(expected_manifest):
        raise ValidationError("Judge run manifest is inconsistent")
    return run_manifest, responses


def _bound_request_manifest_path(
    run_directory: Path, request_directory: Path = None
) -> Path:
    run_directory = Path(os.path.abspath(str(run_directory)))
    run_manifest_path = run_directory / "judge_run_manifest.json"
    if path_has_symlink_component(run_manifest_path) or not run_manifest_path.is_file():
        raise ValidationError("Judge run manifest is missing or symlinked")
    run_manifest = read_json(run_manifest_path)
    validate_schema_instance(
        run_manifest, "judge_run_manifest", context="judge run manifest"
    )
    _validate_file_binding(
        run_manifest["request_manifest"],
        "Judge source request manifest",
        require_nonempty=True,
    )
    path = Path(run_manifest["request_manifest"]["path"])
    if path.name != "request_manifest.json":
        raise ValidationError("Judge request manifest binding has an invalid filename")
    if request_directory is not None and path.resolve().parent != Path(
        request_directory
    ).resolve():
        raise ValidationError("Judge run is bound to a different request bundle")
    return path.resolve()


def validate_judge_run_bundle(
    run_directory: Path,
    response_records: Iterable[Mapping[str, Any]],
    evidence_records: Iterable[Mapping[str, Any]],
    manifest: Mapping[str, Any],
    *,
    request_directory: Path = None,
) -> Tuple[Mapping[str, Any], List[Mapping[str, Any]]]:
    """Validate a formal test-set Judge run against frozen source routing."""

    request_manifest_path = _bound_request_manifest_path(
        run_directory, request_directory=request_directory
    )
    request_manifest, requests, context = validate_judge_request_bundle(
        request_manifest_path.parent,
        list(response_records),
        list(evidence_records),
        manifest,
    )
    runner_sha256 = context["runner_asset"]["sha256"]
    if request_manifest["runner_manifest_sha256"] != runner_sha256:
        raise ValidationError("Judge request/run runner bindings differ")
    return _validate_run_for_requests(
        run_directory,
        request_manifest_path,
        requests,
        context["runner_material"],
    )


def validate_judge_calibration_run_bundle(
    run_directory: Path,
    calibration_context_path: Path,
    *,
    request_directory: Path = None,
) -> Tuple[Mapping[str, Any], List[Mapping[str, Any]]]:
    """Validate calibration predictions only from their exact raw executions."""

    request_manifest_path = _bound_request_manifest_path(
        run_directory, request_directory=request_directory
    )
    request_manifest, requests, loaded = validate_judge_calibration_request_bundle(
        request_manifest_path.parent, calibration_context_path
    )
    runner_sha256 = loaded["runner_material"]["asset"]["sha256"]
    if request_manifest["runner_manifest_sha256"] != runner_sha256:
        raise ValidationError("Judge calibration request/run runner bindings differ")
    return _validate_run_for_requests(
        run_directory,
        request_manifest_path,
        requests,
        loaded["runner_material"],
    )


def compute_judge_calibration_run_diagnostics(
    run_directory: Path,
    calibration_context_path: Path,
    *,
    request_directory: Path = None,
) -> Mapping[str, Any]:
    """Compute profile-aware diagnostics from validated process stdout only."""

    _, responses = validate_judge_calibration_run_bundle(
        run_directory,
        calibration_context_path,
        request_directory=request_directory,
    )
    loaded = _load_judge_calibration_context(calibration_context_path)
    predictions = {
        record["item_id"]: parse_raw_judge_response(record["raw_response"])[
            "verdict"
        ]
        for record in responses
    }
    from .calibration import compute_judge_calibration

    return compute_judge_calibration(
        loaded["gold_records"],
        predictions,
        calibration_profile=loaded["context"]["calibration_profile"],
    )


__all__ = [
    "build_judge_calibration_context_manifest",
    "compute_judge_calibration_run_diagnostics",
    "create_judge_calibration_request_bundle",
    "create_judge_request_bundle",
    "load_frozen_judge_runner",
    "run_judge_calibration_request_bundle",
    "run_judge_request_bundle",
    "validate_judge_calibration_request_bundle",
    "validate_judge_calibration_run_bundle",
    "validate_judge_request_bundle",
    "validate_judge_run_bundle",
]
