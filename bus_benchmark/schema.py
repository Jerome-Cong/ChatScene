"""Runtime validation for versioned benchmark JSON records.

The benchmark keeps schemas as data under ``benchmark_configs/schemas``.  This
module provides one strict, local-only validation entry point so callers do not
need to know about jsonschema resolver or Python type-checker details.
"""

import copy
import datetime as _datetime
import json
import math
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Tuple
from urllib.parse import urldefrag, urljoin

from .errors import ValidationError
from .paths import asset_path

try:
    from jsonschema import Draft7Validator, FormatChecker, RefResolver, validators
except ImportError as exc:  # pragma: no cover - exercised only in dependency-broken installs
    Draft7Validator = None
    FormatChecker = None
    RefResolver = None
    validators = None
    _JSONSCHEMA_IMPORT_ERROR = exc
else:
    _JSONSCHEMA_IMPORT_ERROR = None


SCHEMA_DIRECTORY = asset_path("schemas")

SCHEMA_FILES: Mapping[str, str] = {
    "common_ontology": "common_ontology.schema.json",
    "agent_semantic_review_fragment": "agent_semantic_review_fragment.schema.json",
    "agent_query_review_draft": "agent_query_review_draft.schema.json",
    "cross_platform_fixture_corpus": "cross_platform_fixture_corpus.schema.json",
    "requirement_atom": "requirement_atom.schema.json",
    "oracle_record": "oracle_record.schema.json",
    "query_roster": "query_roster.schema.json",
    "method_config": "method_config.schema.json",
    "method_environment_manifest": "method_environment_manifest.schema.json",
    "metadrive_pg_block_registry": "metadrive_pg_block_registry.schema.json",
    "metadrive_pg_block_scene": "metadrive_pg_block_scene.schema.json",
    "metadrive_pg_token_registry": "metadrive_pg_token_registry.schema.json",
    "metadrive_native_probe_evidence": "metadrive_native_probe_evidence.schema.json",
    "metadrive_track_draft_decisions": "metadrive_track_draft_decisions.schema.json",
    "human_query_gold": "human_query_gold_record.schema.json",
    "judge_calibration": "judge_calibration.schema.json",
    "judge_calibration_roster": "judge_calibration_roster.schema.json",
    "judge_calibration_source_config": "judge_calibration_source_config.schema.json",
    "judge_calibration_context_manifest": "judge_calibration_context_manifest.schema.json",
    "judge_calibration_request_record": "judge_calibration_request_record.schema.json",
    "judge_calibration_request_bundle_manifest": "judge_calibration_request_bundle_manifest.schema.json",
    "judge_output": "judge_output.schema.json",
    "judge_runner_manifest": "judge_runner_manifest.schema.json",
    "judge_request_record": "judge_request_record.schema.json",
    "judge_request_bundle_manifest": "judge_request_bundle_manifest.schema.json",
    "judge_execution_record": "judge_execution_record.schema.json",
    "judge_run_manifest": "judge_run_manifest.schema.json",
    "judge_rubric": "judge_rubric.schema.json",
    "judge_response_record": "judge_response_record.schema.json",
    "platform_fixture_review": "platform_fixture_review.schema.json",
    "platform_capability": "platform_capability.schema.json",
    "protocol_decision_review": "protocol_decision_review.schema.json",
    "response_record": "response_record.schema.json",
    "controller_config": "controller_config.schema.json",
    "platform_runtime_config": "platform_runtime_config.schema.json",
    "runtime_record": "runtime_record.schema.json",
    "uqh_assessment": "uqh_assessment.schema.json",
    "uqh_assessor_execution": "uqh_assessor_execution.schema.json",
    "uqh_assessor_output": "uqh_assessor_output.schema.json",
    "uqh_assessor_request_record": "uqh_assessor_request_record.schema.json",
    "uqh_assessor_registry": "uqh_assessor_registry.schema.json",
    "semantic_evidence": "semantic_evidence.schema.json",
    "semantic_extractor_manifest": "semantic_extractor_manifest.schema.json",
    "semantic_fixture_corpus": "semantic_fixture_corpus.schema.json",
    "semantic_fixture_input": "semantic_fixture_input.schema.json",
    "semantic_fixture_results": "semantic_fixture_results.schema.json",
    "semantic_projection": "semantic_projection.schema.json",
    "semantic_score": "semantic_score.schema.json",
    "cpd_input_record": "cpd_input_record.schema.json",
    "cpd_coverage": "cpd_coverage.schema.json",
    "cpd_aggregate": "cpd_aggregate.schema.json",
    "method_result": "method_result.schema.json",
    "freeze_asset": "freeze_asset.schema.json",
    "freeze_protocol": "freeze_protocol.schema.json",
    "freeze_manifest": "freeze_manifest.schema.json",
}

# Formal provenance and freeze admission are intentionally fail-closed.  A
# schema copied into ``benchmark_configs/schemas`` is not part of the active
# benchmark until its exact filename is listed here (or in ``SCHEMA_FILES``).
_ADDITIONAL_SUPPORTED_SCHEMA_FILES = frozenset(
    {
        "generation_evidence.schema.json",
        "generation_run_manifest.schema.json",
        "human_bound_review_packet.schema.json",
        "human_bound_review_submission.schema.json",
        "human_calibration_gold_record.schema.json",
        "human_carla_stage_calibration_report.schema.json",
        "human_carla_stage_calibration_roster.schema.json",
        "human_carla_stage_platform_report.schema.json",
        "human_machine_draft_registry.schema.json",
        "human_machine_draft_summary.schema.json",
        "human_platform_gold_record.schema.json",
        "human_post_test_private.schema.json",
        "human_post_test_public.schema.json",
        "human_post_test_report.schema.json",
        "human_post_test_submission.schema.json",
        "human_query_review_packet.schema.json",
        "human_query_review_submission.schema.json",
    }
)
SUPPORTED_SCHEMA_FILENAMES = frozenset(SCHEMA_FILES.values()).union(
    _ADDITIONAL_SUPPORTED_SCHEMA_FILES
)


def supported_schema_paths() -> Tuple[Path, ...]:
    """Return the explicit active schema inventory used by freeze/provenance."""

    paths = tuple(
        SCHEMA_DIRECTORY / filename for filename in sorted(SUPPORTED_SCHEMA_FILENAMES)
    )
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise ValidationError(
            "supported benchmark schema is missing: {}".format(missing[0])
        )
    return tuple(path.resolve() for path in paths)


def _strict_integer(_checker: Any, instance: Any) -> bool:
    """Accept JSON integers only; bool and integral floats are not run indices."""

    return type(instance) is int


def _strict_number(_checker: Any, instance: Any) -> bool:
    """Accept finite JSON numbers while explicitly excluding bool."""

    if type(instance) is int:
        return True
    return type(instance) is float and math.isfinite(instance)


def _require_jsonschema() -> None:
    if _JSONSCHEMA_IMPORT_ERROR is not None:
        raise ValidationError(
            "runtime schema validation requires the installed jsonschema dependency: {}".format(
                _JSONSCHEMA_IMPORT_ERROR
            )
        )


@lru_cache(maxsize=1)
def _format_checker() -> Any:
    """Supply date-time checking missing from minimal jsonschema 3.x installs."""

    _require_jsonschema()
    checker = FormatChecker()

    @checker.checks("date-time", raises=(TypeError, ValueError))
    def is_utc_datetime(value: Any) -> bool:
        if not isinstance(value, str):
            return False
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = _datetime.datetime.fromisoformat(normalized)
        return parsed.tzinfo is not None and parsed.utcoffset() == _datetime.timedelta(0)

    return checker


def available_schemas() -> Tuple[str, ...]:
    """Return stable public names accepted by the validation functions."""

    return tuple(sorted(SCHEMA_FILES))


def _canonical_schema_name(schema_name: str) -> str:
    if not isinstance(schema_name, str) or not schema_name:
        raise ValidationError("schema name must be a non-empty string")
    if schema_name in SCHEMA_FILES:
        return schema_name
    filename = Path(schema_name).name
    for name, registered_filename in SCHEMA_FILES.items():
        if filename == registered_filename:
            return name
    raise ValidationError(
        "unknown benchmark schema {!r}; available: {}".format(
            schema_name, ", ".join(available_schemas())
        )
    )


@lru_cache(maxsize=1)
def _validator_class() -> Any:
    _require_jsonschema()
    type_checker = Draft7Validator.TYPE_CHECKER.redefine(
        "integer", _strict_integer
    ).redefine("number", _strict_number)
    return validators.extend(Draft7Validator, type_checker=type_checker)


def _read_schema(path: Path) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError("cannot load benchmark schema {}: {}".format(path, exc)) from exc
    if not isinstance(value, dict):
        raise ValidationError("benchmark schema {} must contain a JSON object".format(path))
    return value


def _iter_references(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        reference = value.get("$ref")
        if isinstance(reference, str):
            yield reference
        for child in value.values():
            yield from _iter_references(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_references(child)


@lru_cache(maxsize=1)
def _schema_store() -> Dict[str, Dict[str, Any]]:
    validator_class = _validator_class()
    store: Dict[str, Dict[str, Any]] = {}
    schemas = []
    for filename in sorted(set(SCHEMA_FILES.values())):
        path = SCHEMA_DIRECTORY / filename
        schema = _read_schema(path)
        try:
            validator_class.check_schema(schema)
        except Exception as exc:
            raise ValidationError("invalid benchmark schema {}: {}".format(path, exc)) from exc
        schemas.append((filename, path, schema))
        store[filename] = schema
        store[path.resolve().as_uri()] = schema
        schema_id = schema.get("$id")
        if schema_id:
            store[str(schema_id)] = schema
    for filename, path, schema in schemas:
        base_uri = str(schema.get("$id") or path.resolve().as_uri())
        for reference in _iter_references(schema):
            if reference.startswith("#"):
                continue
            resolved = urljoin(base_uri, reference)
            reference_base, _ = urldefrag(reference)
            resolved_base, _ = urldefrag(resolved)
            if (
                reference not in store
                and resolved not in store
                and reference_base not in store
                and resolved_base not in store
            ):
                raise ValidationError(
                    "benchmark schema {} has non-local or unknown $ref {!r}".format(
                        filename, reference
                    )
                )
    return store


def load_schema(schema_name: str) -> Dict[str, Any]:
    """Load a registered schema by public name or exact schema filename."""

    canonical_name = _canonical_schema_name(schema_name)
    return copy.deepcopy(_schema_store()[SCHEMA_FILES[canonical_name]])


def _instance_path(parts: Iterable[Any]) -> str:
    result = "$"
    for part in parts:
        if isinstance(part, int):
            result += "[{}]".format(part)
        elif isinstance(part, str) and part.replace("_", "a").isalnum():
            result += ".{}".format(part)
        else:
            result += "[{}]".format(json.dumps(part, ensure_ascii=False))
    return result


def validate_schema_instance(
    instance: Any,
    schema_name: str,
    *,
    context: str = "record",
    max_errors: int = 5,
) -> Any:
    """Validate one JSON-compatible value and return it unchanged.

    Validation is local-only: relative ``$ref`` values are resolved from the
    repository schema directory, never from the network.  At most ``max_errors``
    concise failures are included in the exception to avoid dumping artifacts.
    """

    if type(max_errors) is not int or max_errors < 1:
        raise ValidationError("max_errors must be a positive integer")
    canonical_name = _canonical_schema_name(schema_name)
    schema = load_schema(canonical_name)
    validator_class = _validator_class()
    resolver = RefResolver.from_schema(schema, store=_schema_store())
    validator = validator_class(
        schema,
        resolver=resolver,
        format_checker=_format_checker(),
    )
    errors = sorted(
        validator.iter_errors(instance),
        key=lambda error: (tuple(str(part) for part in error.absolute_path), error.message),
    )
    if errors:
        details = [
            "{}: {}".format(_instance_path(error.absolute_path), error.message)
            for error in errors[:max_errors]
        ]
        if len(errors) > max_errors:
            details.append("... {} additional error(s)".format(len(errors) - max_errors))
        raise ValidationError(
            "{} violates {} schema: {}".format(
                context, canonical_name, "; ".join(details)
            )
        )
    return instance


def validate_schema_records(
    records: Iterable[Any],
    schema_name: str,
    *,
    context: str = "record",
) -> Dict[str, Any]:
    """Validate an iterable record-by-record and report its exact count."""

    count = 0
    for count, record in enumerate(records, 1):
        validate_schema_instance(
            record,
            schema_name,
            context="{}[{}]".format(context, count - 1),
        )
    return {"schema": _canonical_schema_name(schema_name), "record_count": count}
