"""Create source-bound, independently signed UQH assessment records."""

import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence

from .errors import ValidationError
from .jsonio import canonical_json_bytes, sha256_bytes, strict_json_object_bytes
from .metrics import (
    compute_uqh,
    uqh_assessment_content_sha256,
    uqh_assessment_id,
    uqh_assessor_request_payload,
    uqh_assessor_request_sha256,
    uqh_attestation_payload,
    validate_uqh_response_producers,
)
from .schema import validate_schema_instance, validate_schema_records


def _jsonl_bytes(records: Sequence[Mapping[str, Any]]) -> bytes:
    payload = b"\n".join(canonical_json_bytes(dict(record)) for record in records)
    return payload + (b"\n" if records else b"")


class _AtomicBundle:
    """Stage a complete directory and publish it with one atomic rename."""

    def __init__(self, output_directory: Path) -> None:
        self.output_directory = Path(output_directory).resolve()
        self.parent = self.output_directory.parent
        self.lock_path = self.parent / ".{}.lock".format(self.output_directory.name)
        self.lock_descriptor = None
        self.staging_directory = None

    def __enter__(self) -> Path:
        self.parent.mkdir(parents=True, exist_ok=True)
        if self.output_directory.exists() or self.output_directory.is_symlink():
            raise ValidationError("immutable UQH output bundle already exists")
        try:
            self.lock_descriptor = os.open(
                str(self.lock_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
        except FileExistsError as exc:
            raise ValidationError("another UQH bundle publication is in progress") from exc
        try:
            self.staging_directory = Path(
                tempfile.mkdtemp(
                    prefix=".{}-staging-".format(self.output_directory.name),
                    dir=str(self.parent),
                )
            )
        except Exception:
            os.close(self.lock_descriptor)
            self.lock_descriptor = None
            self.lock_path.unlink()
            raise
        return self.staging_directory

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        try:
            if exc_type is None:
                if self.output_directory.exists() or self.output_directory.is_symlink():
                    raise ValidationError("immutable UQH output bundle already exists")
                os.rename(str(self.staging_directory), str(self.output_directory))
                self.staging_directory = None
                directory_descriptor = os.open(str(self.parent), os.O_RDONLY)
                try:
                    os.fsync(directory_descriptor)
                finally:
                    os.close(directory_descriptor)
        finally:
            if self.staging_directory is not None and self.staging_directory.exists():
                shutil.rmtree(str(self.staging_directory))
            if self.lock_descriptor is not None:
                os.close(self.lock_descriptor)
            try:
                self.lock_path.unlink()
            except FileNotFoundError:
                pass
        return False


def _write_staged_bytes(staging_directory: Path, relative_path: Path, payload: bytes) -> None:
    target = staging_directory / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _bound_bytes(
    binding: Mapping[str, Any], label: str, *, require_nonempty: bool = True
) -> bytes:
    if not isinstance(binding, Mapping) or set(binding) != {"path", "sha256", "bytes"}:
        raise ValidationError("{} binding is malformed".format(label))
    path = Path(str(binding.get("path", "")))
    if not path.is_file():
        raise ValidationError("{} file is missing".format(label))
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ValidationError("{} file is unreadable".format(label)) from exc
    if (
        (require_nonempty and not payload)
        or len(payload) != binding.get("bytes")
        or sha256_bytes(payload) != binding.get("sha256")
    ):
        raise ValidationError("{} byte binding changed".format(label))
    return payload


def _prevalidate_response_files(responses: Sequence[Mapping[str, Any]]) -> None:
    """Read every source file before any assessor signing occurs."""

    for response in responses:
        raw = response["raw_method_response"]
        envelope = {
            "stdout": dict(raw["stdout"]),
            "stderr": dict(raw["stderr"]),
            "exit_code": raw["exit_code"],
            "timed_out": raw["timed_out"],
        }
        for stream_name in ("stdout", "stderr"):
            _bound_bytes(
                raw[stream_name],
                "UQH raw method {}".format(stream_name),
                require_nonempty=False,
            )
        if raw["envelope_sha256"] != sha256_bytes(canonical_json_bytes(envelope)):
            raise ValidationError("UQH raw method response envelope changed")
        artifact = response.get("artifact")
        if artifact is not None:
            artifact_bytes = _bound_bytes(
                artifact, "UQH response artifact", require_nonempty=False
            )
            if response.get("disposition") == "generate" and not artifact_bytes:
                raise ValidationError("UQH generated artifact must not be empty")


def _openssl(args: Sequence[str], *, payload: Optional[bytes] = None) -> bytes:
    try:
        completed = subprocess.run(
            ["openssl"] + list(args),
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValidationError("OpenSSL is unavailable for UQH attestation") from exc
    if completed.returncode != 0:
        raise ValidationError("OpenSSL rejected the UQH attestation key or operation")
    return completed.stdout


def _validate_external_private_key(
    private_key_path: Path, public_key_path: Path
) -> None:
    module_path = Path(__file__).resolve()
    # Source checkouts may import either the root package or benchmark/src.
    # A .git file is also a repository marker (e.g. linked worktrees).
    repository_root = next(
        (
            parent for parent in module_path.parents
            if (parent / ".git").is_file() or (parent / ".git" / "HEAD").is_file()
        ),
        None,
    )
    if repository_root is None:
        package_parent = module_path.parents[1]
        if package_parent.name == "src" and package_parent.parent.name == "benchmark":
            # Preserve the same boundary for a source archive without .git.
            repository_root = package_parent.parent.parent
        else:
            # Standalone wheels have no checkout: exclude the installation
            # directory; root-layout source archives exclude their source root.
            repository_root = package_parent
    private_key_path = Path(private_key_path).resolve()
    try:
        private_key_path.relative_to(repository_root)
    except ValueError:
        pass
    else:
        raise ValidationError(
            "UQH assessor private key must be mounted outside the benchmark repository or installation directory"
        )
    if not private_key_path.is_file():
        raise ValidationError("UQH assessor private key is missing")
    if os.name == "posix":
        key_mode = stat.S_IMODE(private_key_path.stat().st_mode)
        if key_mode not in (0o400, 0o600):
            raise ValidationError(
                "UQH assessor private key mode must be owner-only 0400 or 0600"
            )
    private_public_der = _openssl(
        ["pkey", "-in", str(private_key_path), "-pubout", "-outform", "DER"]
    )
    registered_public_der = _openssl(
        ["pkey", "-pubin", "-in", str(public_key_path), "-outform", "DER"]
    )
    if private_public_der != registered_public_der:
        raise ValidationError("UQH private key does not match the frozen assessor public key")


def _sign_payload(private_key_path: Path, payload: bytes) -> bytes:
    return _openssl(
        [
            "dgst",
            "-sha256",
            "-sign",
            str(private_key_path.resolve()),
            "-sigopt",
            "rsa_padding_mode:pss",
            "-sigopt",
            "rsa_pss_saltlen:-1",
        ],
        payload=payload,
    )


def create_uqh_assessor_requests(
    response_records: Iterable[Mapping[str, Any]],
    oracle_records: Iterable[Mapping[str, Any]],
    roster: Mapping[str, Any],
    assessor_registry: Mapping[str, Any],
    assessor_id: str,
    output_directory: Path,
    *,
    expected_config_sha256: Optional[str] = None,
) -> Sequence[Mapping[str, Any]]:
    """Export the exact requests an external independent assessor must execute."""

    from .roster import roster_entries, validate_records_against_roster

    responses = list(response_records)
    oracles = list(oracle_records)
    validate_schema_records(responses, "response_record", context="UQH responses")
    validate_schema_records(oracles, "oracle_record", context="UQH oracle")
    validate_schema_instance(roster, "query_roster", context="UQH query roster")
    validate_records_against_roster(responses, roster, require_confirmed=True)
    validate_uqh_response_producers(responses)
    _prevalidate_response_files(responses)
    validate_schema_instance(
        assessor_registry, "uqh_assessor_registry", context="UQH assessor registry"
    )
    if assessor_registry.get("status") != "frozen":
        raise ValidationError("UQH request export requires a frozen assessor registry")
    assessors = [
        value
        for value in assessor_registry.get("assessors", [])
        if value.get("assessor_id") == assessor_id
    ]
    if len(assessors) != 1:
        raise ValidationError("UQH request export requires one registered assessor")
    assessor = assessors[0]
    for field in ("assessment_config", "assessor_source", "attestation_public_key"):
        _bound_bytes(assessor.get(field), "UQH assessor {}".format(field))

    config_hashes = {record["config_sha256"] for record in responses}
    if len(config_hashes) != 1:
        raise ValidationError("UQH responses must share one frozen main configuration")
    config_sha256 = next(iter(config_hashes))
    if expected_config_sha256 is not None and config_sha256 != expected_config_sha256:
        raise ValidationError("UQH responses were not produced by the frozen method config")
    entries = roster_entries(roster)
    oracle_by_query = {record["query_id"]: record for record in oracles}
    if len(oracle_by_query) != len(oracles) or set(oracle_by_query) != set(entries):
        raise ValidationError("UQH oracle must exactly cover the frozen query roster")
    for query_id, oracle in oracle_by_query.items():
        entry = entries[query_id]
        if oracle.get("decision_status") != "confirmed" or any(
            oracle.get(field) != entry.get(field)
            for field in ("intent_group_id", "surface_style", "expected_support")
        ):
            raise ValidationError("UQH oracle differs from the frozen roster")

    registry_sha256 = sha256_bytes(canonical_json_bytes(assessor_registry))
    output_directory = Path(output_directory).resolve()
    request_records = []
    request_payloads = {}
    for response in sorted(responses, key=lambda value: value["run_id"]):
        if not (
            response.get("expected_support") == "unsupported"
            and response.get("disposition") in ("reject", "clarification")
        ):
            continue
        if response.get("terminal_status") != "complete":
            raise ValidationError(
                "UQH reject/clarification must complete before independent assessment"
            )
        producer_id = response.get("provenance", {}).get("producer_id")
        if producer_id not in assessor.get("independent_from_producer_ids", []):
            raise ValidationError("UQH assessor is not independent of the response producer")
        handling = response.get("unsupported_handling")
        response_stream = (
            handling.get("response_stream") if isinstance(handling, Mapping) else None
        )
        if response_stream not in ("stdout", "stderr"):
            raise ValidationError("UQH response lacks a selected method-response stream")
        response_bytes = _bound_bytes(
            response["raw_method_response"][response_stream],
            "UQH selected method response",
        )
        oracle = oracle_by_query[response["query_id"]]
        request_context = {
            "run_id": response["run_id"],
            "query_id": response["query_id"],
            "disposition": response["disposition"],
            "assessment_source": assessor["assessment_source"],
            "provenance": {
                "assessor_id": assessor_id,
                "assessor_version": assessor["assessor_version"],
                "assessor_registry_sha256": registry_sha256,
                "assessment_config_sha256": assessor["assessment_config"]["sha256"],
                "assessor_source_sha256": assessor["assessor_source"]["sha256"],
            },
        }
        payload = canonical_json_bytes(
            uqh_assessor_request_payload(
                request_context, response, oracle, response_stream, response_bytes
            )
        )
        request_sha256 = sha256_bytes(payload)
        relative_request_path = Path("requests") / "{}.json".format(request_sha256)
        request_path = output_directory / relative_request_path
        request_payloads[relative_request_path] = payload
        request_records.append(
            {
                "schema_version": "0.1",
                "run_id": response["run_id"],
                "query_id": response["query_id"],
                "assessor_id": assessor_id,
                "assessor_request_sha256": request_sha256,
                "raw_assessor_request": {
                    "path": str(request_path),
                    "sha256": request_sha256,
                    "bytes": len(payload),
                },
            }
        )
    validate_schema_records(
        request_records,
        "uqh_assessor_request_record",
        context="UQH assessor requests",
    )
    with _AtomicBundle(output_directory) as staging_directory:
        for relative_path, payload in sorted(
            request_payloads.items(), key=lambda item: str(item[0])
        ):
            _write_staged_bytes(staging_directory, relative_path, payload)
        _write_staged_bytes(
            staging_directory,
            Path("request_index.jsonl"),
            _jsonl_bytes(request_records),
        )
    for record in request_records:
        _bound_bytes(record["raw_assessor_request"], "published UQH assessor request")
    return request_records


def create_signed_uqh_assessments(
    response_records: Iterable[Mapping[str, Any]],
    oracle_records: Iterable[Mapping[str, Any]],
    execution_records: Iterable[Mapping[str, Any]],
    roster: Mapping[str, Any],
    assessor_registry: Mapping[str, Any],
    assessor_id: str,
    private_key_path: Path,
    output_directory: Path,
    *,
    expected_config_sha256: Optional[str] = None,
) -> Sequence[Mapping[str, Any]]:
    """Materialize signed assessments from raw external-assessor executions.

    Raw request/response hashes provide reproducibility. The detached signature,
    rooted in the frozen registry public key, provides assessor authentication.
    """

    responses = list(response_records)
    oracles = list(oracle_records)
    executions = list(execution_records)
    from .roster import roster_entries, validate_records_against_roster

    validate_schema_records(responses, "response_record", context="UQH responses")
    validate_schema_records(oracles, "oracle_record", context="UQH oracle")
    validate_schema_records(
        executions, "uqh_assessor_execution", context="UQH assessor executions"
    )
    validate_schema_instance(roster, "query_roster", context="UQH query roster")
    validate_records_against_roster(responses, roster, require_confirmed=True)
    validate_uqh_response_producers(responses)
    _prevalidate_response_files(responses)
    validate_schema_instance(
        assessor_registry, "uqh_assessor_registry", context="UQH assessor registry"
    )
    if assessor_registry.get("status") != "frozen":
        raise ValidationError("UQH attestation requires a frozen assessor registry")
    matching_assessors = [
        value
        for value in assessor_registry.get("assessors", [])
        if value.get("assessor_id") == assessor_id
    ]
    if len(matching_assessors) != 1:
        raise ValidationError("UQH attestation requires one registered assessor")
    assessor = matching_assessors[0]
    if assessor.get("attestation_algorithm") != "rsa_pss_sha256":
        raise ValidationError("UQH assessor does not use the supported attestation algorithm")
    for field in ("assessment_config", "assessor_source"):
        _bound_bytes(assessor.get(field), "UQH assessor {}".format(field))
    public_key_binding = assessor.get("attestation_public_key")
    _bound_bytes(public_key_binding, "UQH assessor public key")
    public_key_path = Path(public_key_binding["path"])
    _validate_external_private_key(Path(private_key_path), public_key_path)

    config_hashes = {record["config_sha256"] for record in responses}
    if len(config_hashes) != 1:
        raise ValidationError("UQH responses must share one frozen main configuration")
    config_sha256 = next(iter(config_hashes))
    if expected_config_sha256 is not None and config_sha256 != expected_config_sha256:
        raise ValidationError("UQH responses were not produced by the frozen method config")
    entries = roster_entries(roster)
    oracle_by_query = {record["query_id"]: record for record in oracles}
    if len(oracle_by_query) != len(oracles) or set(oracle_by_query) != set(entries):
        raise ValidationError("UQH oracle must exactly cover the frozen query roster")
    for query_id, oracle in oracle_by_query.items():
        entry = entries[query_id]
        if oracle.get("decision_status") != "confirmed" or any(
            oracle.get(field) != entry.get(field)
            for field in ("intent_group_id", "surface_style", "expected_support")
        ):
            raise ValidationError("UQH oracle differs from the frozen roster")
    required_responses = {
        record["run_id"]: record
        for record in responses
        if record.get("expected_support") == "unsupported"
        and record.get("disposition") in ("reject", "clarification")
        and record.get("terminal_status") == "complete"
    }
    execution_by_run = {record["run_id"]: record for record in executions}
    if (
        len(execution_by_run) != len(executions)
        or len({record["external_execution_id"] for record in executions})
        != len(executions)
        or set(execution_by_run) != set(required_responses)
    ):
        raise ValidationError(
            "UQH assessor executions must uniquely and exactly cover required responses"
        )

    output_directory = Path(output_directory).resolve()
    if output_directory.exists() or output_directory.is_symlink():
        raise ValidationError("immutable UQH output bundle already exists")
    registry_sha256 = sha256_bytes(canonical_json_bytes(assessor_registry))
    prepared_assessments = []
    for run_id in sorted(required_responses):
        response = required_responses[run_id]
        execution = execution_by_run[run_id]
        oracle = oracle_by_query.get(response["query_id"])
        if oracle is None:
            raise ValidationError("UQH response lacks an oracle record")
        raw_output_bytes = _bound_bytes(
            execution["raw_assessor_response"], "UQH raw assessor response"
        )
        raw_output = strict_json_object_bytes(
            raw_output_bytes, "UQH raw assessor response"
        )
        validate_schema_instance(
            raw_output, "uqh_assessor_output", context="UQH raw assessor response"
        )
        assessment: Dict[str, Any] = {
            "schema_version": "0.1",
            "run_id": run_id,
            "query_id": response["query_id"],
            "disposition": response["disposition"],
            "response_record_sha256": sha256_bytes(canonical_json_bytes(response)),
            "raw_response_envelope_sha256": response["raw_method_response"][
                "envelope_sha256"
            ],
            "oracle_record_sha256": sha256_bytes(canonical_json_bytes(oracle)),
            "verdict": raw_output["verdict"],
            "reason_codes": raw_output["reason_codes"],
            "assessment_source": assessor["assessment_source"],
            "evidence": raw_output["evidence"],
            "rationale": raw_output["rationale"],
            "provenance": {
                "assessor_id": assessor_id,
                "assessor_version": assessor["assessor_version"],
                "assessor_registry_sha256": registry_sha256,
                "assessment_config_sha256": assessor["assessment_config"]["sha256"],
                "assessor_source_sha256": assessor["assessor_source"]["sha256"],
                "assessor_request_sha256": "",
                "raw_assessor_request": dict(execution["raw_assessor_request"]),
                "raw_assessor_response": dict(execution["raw_assessor_response"]),
                "external_execution_id": execution["external_execution_id"],
                "created_at_utc": execution["created_at_utc"],
            },
        }
        handling = response.get("unsupported_handling")
        response_stream = (
            handling.get("response_stream") if isinstance(handling, Mapping) else None
        )
        if response_stream not in ("stdout", "stderr"):
            raise ValidationError("UQH response lacks a selected method-response stream")
        selected_response_bytes = _bound_bytes(
            response["raw_method_response"][response_stream],
            "UQH selected method response",
        )
        expected_request = canonical_json_bytes(
            uqh_assessor_request_payload(
                assessment,
                response,
                oracle,
                response_stream,
                selected_response_bytes,
            )
        )
        if _bound_bytes(
            execution["raw_assessor_request"], "UQH raw assessor request"
        ) != expected_request:
            raise ValidationError("UQH raw assessor request differs from exact inputs")
        assessment["provenance"]["assessor_request_sha256"] = (
            uqh_assessor_request_sha256(
                assessment,
                response,
                oracle,
                response_stream,
                selected_response_bytes,
            )
        )
        assessment["assessment_content_sha256"] = uqh_assessment_content_sha256(
            assessment
        )
        assessment["assessment_id"] = uqh_assessment_id(assessment)
        payload = canonical_json_bytes(uqh_attestation_payload(assessment))
        selected_evidence = False
        for evidence in assessment["evidence"]:
            evidence_stream = evidence["response_stream"]
            stream_bytes = _bound_bytes(
                response["raw_method_response"][evidence_stream],
                "UQH cited method response",
                require_nonempty=False,
            )
            start = evidence["byte_start"]
            end = evidence["byte_end"]
            if end < start or end > len(stream_bytes):
                raise ValidationError("UQH assessor evidence byte range is invalid")
            excerpt = stream_bytes[start:end]
            if sha256_bytes(excerpt) != evidence["bytes_sha256"]:
                raise ValidationError("UQH assessor evidence bytes changed")
            if evidence_stream == response_stream and excerpt:
                selected_evidence = True
        if assessment["verdict"] == "valid" and not selected_evidence:
            raise ValidationError(
                "valid UQH assessment requires selected-stream evidence"
            )
        prepared_assessments.append((assessment, payload))

    published_assessments = []
    with _AtomicBundle(output_directory) as staging_directory:
        staged_assessments = []
        for assessment, payload in prepared_assessments:
            signature = _sign_payload(Path(private_key_path), payload)
            relative_signature_path = Path("signatures") / "{}.sig".format(
                assessment["assessment_id"]
            )
            _write_staged_bytes(
                staging_directory, relative_signature_path, signature
            )
            assessment["attestation"] = {
                "algorithm": "rsa_pss_sha256",
                "signed_payload_sha256": sha256_bytes(payload),
                "signature": {
                    "path": str(staging_directory / relative_signature_path),
                    "sha256": sha256_bytes(signature),
                    "bytes": len(signature),
                },
            }
            staged_assessments.append(assessment)

        compute_uqh(
            responses,
            oracles,
            staged_assessments,
            roster,
            assessor_registry,
            expected_config_sha256=expected_config_sha256,
        )
        for assessment in staged_assessments:
            assessment["attestation"]["signature"]["path"] = str(
                output_directory
                / "signatures"
                / "{}.sig".format(assessment["assessment_id"])
            )
        published_assessments = staged_assessments
        _write_staged_bytes(
            staging_directory,
            Path("assessment_index.jsonl"),
            _jsonl_bytes(published_assessments),
        )

    compute_uqh(
        responses,
        oracles,
        published_assessments,
        roster,
        assessor_registry,
        expected_config_sha256=expected_config_sha256,
    )
    return published_assessments
