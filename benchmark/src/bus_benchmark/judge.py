"""Canonical configuration, request, and raw-response handling for the frozen judge."""
from typing import Any, Dict, Mapping

from .errors import ValidationError
from .jsonio import canonical_json_bytes, sha256_bytes, strict_json_object_bytes
from .schema import validate_schema_instance


JUDGE_REQUEST_CONTRACT = "judge_atom_v0.1"
JUDGE_PROJECTION_FIELDS = (
    "ego",
    "atoms",
    "common_atoms",
    "complete_categories",
)


def judge_config_payload(
    model_id: str,
    prompt_sha256: str,
    inference_config: Mapping[str, Any],
    output_schema_sha256: str,
    request_contract: str = JUDGE_REQUEST_CONTRACT,
    runner_manifest_sha256: str = None,
) -> Dict[str, Any]:
    payload = {
        "request_contract": request_contract,
        "model_id": model_id,
        "prompt_sha256": prompt_sha256,
        "inference_config": dict(inference_config),
        "output_schema_sha256": output_schema_sha256,
    }
    if runner_manifest_sha256 is not None:
        payload["runner_manifest_sha256"] = runner_manifest_sha256
    return payload


def judge_config_sha256(**kwargs: Any) -> str:
    return sha256_bytes(canonical_json_bytes(judge_config_payload(**kwargs)))


def evidence_projection(evidence: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        field: evidence.get(field)
        for field in JUDGE_PROJECTION_FIELDS
    }


def judge_item_id(
    source_response_sha256: str, source_evidence_sha256: str, atom_id: str
) -> str:
    return sha256_bytes(
        canonical_json_bytes(
            {
                "source_response_sha256": source_response_sha256,
                "source_evidence_sha256": source_evidence_sha256,
                "atom_id": atom_id,
            }
        )
    )


def judge_request_payload(
    *,
    model_id: str,
    judge_config_sha256_value: str,
    inference_config: Mapping[str, Any],
    prompt_text: str,
    query_text: str,
    platform: str,
    artifact_sha256: str,
    artifact_text: str,
    source_response_sha256: str,
    source_evidence_sha256: str,
    evidence: Mapping[str, Any],
    oracle_atom: Mapping[str, Any],
    output_schema: Mapping[str, Any],
    request_contract: str = JUDGE_REQUEST_CONTRACT,
) -> Dict[str, Any]:
    return {
        "contract": request_contract,
        "model_id": model_id,
        "judge_config_sha256": judge_config_sha256_value,
        "inference_config": dict(inference_config),
        "messages": [
            {"role": "system", "content": prompt_text},
            {
                "role": "user",
                "content": {
                    "query_text": query_text,
                    "platform": platform,
                    "artifact": {
                        "sha256": artifact_sha256,
                        "text": artifact_text,
                    },
                    "source_response_sha256": source_response_sha256,
                    "source_evidence_sha256": source_evidence_sha256,
                    "deterministic_evidence": evidence_projection(evidence),
                    "oracle_atom": dict(oracle_atom),
                },
            },
        ],
        "output_schema": dict(output_schema),
    }


def judge_request_sha256(**kwargs: Any) -> str:
    return sha256_bytes(canonical_json_bytes(judge_request_payload(**kwargs)))


def judge_response_id(judge_request_sha256_value: str, attempt: int = 0) -> str:
    return sha256_bytes(
        canonical_json_bytes(
            {
                "judge_request_sha256": judge_request_sha256_value,
                "attempt": attempt,
            }
        )
    )


def parse_raw_judge_response(raw_response: str) -> Mapping[str, Any]:
    if not isinstance(raw_response, str) or not raw_response:
        raise ValidationError("judge raw response must be non-empty UTF-8 text")
    value = strict_json_object_bytes(
        raw_response.encode("utf-8"), "judge raw response"
    )
    validate_schema_instance(value, "judge_output", context="judge raw response")
    return value
