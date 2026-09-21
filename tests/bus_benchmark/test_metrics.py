import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from bus_benchmark.atoms import make_atom
from bus_benchmark.cli import build_parser, main
from bus_benchmark.metrics import (
    aggregate_semantic_metrics,
    compute_rqs,
    compute_uqh,
    score_semantic_output,
    set_f1,
    uqh_assessment_content_sha256,
    uqh_assessment_id,
    uqh_assessor_request_payload,
    uqh_assessor_request_sha256,
    uqh_attestation_payload,
)
from bus_benchmark.errors import ValidationError
from bus_benchmark.generation import PRODUCER_ID
from bus_benchmark.jsonio import (
    canonical_json_bytes,
    read_jsonl,
    sha256_bytes,
    write_json,
    write_jsonl,
)
from bus_benchmark.uqh import (
    create_signed_uqh_assessments,
    create_uqh_assessor_requests,
)


def oracle(atoms):
    for atom in atoms:
        atom["decision_status"] = "confirmed"
    return {
        "query_id": "q",
        "intent_group_id": "g",
        "surface_style": "precise",
        "expected_support": "supported",
        "decision_status": "confirmed",
        "atoms": atoms,
    }


class SemanticMetricTests(unittest.TestCase):
    def test_non_unit_atom_weight_is_rejected(self):
        for weight in (-1.0, 0.0, 2.0):
            with self.assertRaises(ValidationError):
                make_atom("road", "road_topology", {"value": "midblock"}, weight=weight)

    def test_srs_macro_averages_active_categories(self):
        actor_one = make_atom("actor", "actor_role_count", {"type": "car", "role": "rear", "count": "1"})
        actor_two = make_atom("actor", "actor_role_count", {"type": "pedestrian", "role": "crossing", "count": "1"})
        road = make_atom("road", "road_topology", {"value": "midblock"})
        evidence = {
            "query_id": "q",
            "ego": {
                "count": 1,
                "semantic_role": "bus_proxy",
                "approved_proxy": True,
                "deterministic": True,
                "length_m": 5.33,
                "width_m": 2.10,
                "proxy_id": "fixture_bus_proxy",
            },
            "atoms": [actor_one, road],
            "complete_categories": ["actor", "road"],
        }
        result = score_semantic_output(oracle([actor_one, actor_two, road]), evidence)
        self.assertAlmostEqual(result["srs_category_scores"]["actor"], 0.5)
        self.assertAlmostEqual(result["srs_category_scores"]["road"], 1.0)
        self.assertAlmostEqual(result["srs"], 0.75)

    def test_ego_gate_forces_zero_srs(self):
        atom = make_atom("road", "road_topology", {"value": "midblock"})
        result = score_semantic_output(
            oracle([atom]),
            {"query_id": "q", "ego": {"count": 0}, "atoms": [atom], "complete_categories": ["road"]},
        )
        self.assertEqual(result["srs"], 0.0)
        self.assertFalse(result["ego_gate_passed"])

    def test_wrong_proxy_footprint_fails_ego_gate(self):
        atom = make_atom("road", "road_topology", {"value": "midblock"})
        result = score_semantic_output(
            oracle([atom]),
            {
                "query_id": "q",
                "ego": {
                    "count": 1,
                    "semantic_role": "bus_proxy",
                    "approved_proxy": True,
                    "deterministic": True,
                    "length_m": 1.0,
                    "width_m": 1.0,
                    "proxy_id": "wrong",
                },
                "atoms": [atom],
            },
        )
        self.assertEqual(result["srs"], 0.0)

    def test_draft_atom_cannot_be_formally_scored(self):
        atom = make_atom("road", "road_topology", {"value": "midblock"})
        document = oracle([])
        document["atoms"] = [atom]
        with self.assertRaises(ValidationError):
            score_semantic_output(document, {"query_id": "q", "ego": {}})

    def test_multiple_actor_count_matches_two_instances(self):
        gold = make_atom(
            "actor",
            "actor_role_count",
            {"type": "car", "role": "queue", "count": "multiple"},
        )
        predicted = make_atom(
            "actor", "actor_role_count", {"type": "car", "role": "queue", "count": "2"}
        )
        result = score_semantic_output(
            oracle([gold]),
            {
                "query_id": "q",
                "ego": {
                    "count": 1,
                    "semantic_role": "bus_proxy",
                    "approved_proxy": True,
                    "deterministic": True,
                    "length_m": 5.33,
                    "width_m": 2.10,
                    "proxy_id": "fixture",
                },
                "atoms": [predicted],
                "complete_categories": ["actor"],
            },
        )
        self.assertEqual(result["srs"], 1.0)
        self.assertEqual(result["arc"]["availability"], "available")
        self.assertEqual(result["arc"]["f1"], 1.0)

    def test_judge_violation_is_shared_by_srs_and_iec(self):
        event = make_atom("event", "event_spec", {"event": "yield"})
        event["decision_status"] = "confirmed"
        document = oracle([event])
        result = score_semantic_output(
            document,
            {
                "query_id": "q",
                "ego": {
                    "count": 1,
                    "semantic_role": "bus_proxy",
                    "approved_proxy": True,
                    "deterministic": True,
                    "length_m": 5.33,
                    "width_m": 2.10,
                    "proxy_id": "fixture",
                },
            },
            judge_verdicts={
                event["atom_id"]: {
                    "verdict": "violated",
                    "judge_response_id": "a" * 64,
                    "judge_response_record_sha256": "b" * 64,
                }
            },
        )
        self.assertEqual(result["srs"], 0.0)
        self.assertEqual(result["iec_spec"]["availability"], "unavailable")
        self.assertTrue(result["common_core_preservation"]["evidence_complete"])

    def test_bound_judge_satisfaction_is_complete_common_core_evidence(self):
        event = make_atom("event", "event_spec", {"event": "yield"})
        result = score_semantic_output(
            oracle([event]),
            {
                "query_id": "q",
                "ego": {
                    "count": 1,
                    "semantic_role": "bus_proxy",
                    "approved_proxy": True,
                    "deterministic": True,
                    "length_m": 5.33,
                    "width_m": 2.10,
                    "proxy_id": "fixture",
                },
                "atoms": [],
                "complete_categories": [],
            },
            judge_verdicts={
                event["atom_id"]: {
                    "verdict": "satisfied",
                    "judge_response_id": "a" * 64,
                    "judge_response_record_sha256": "b" * 64,
                }
            },
        )
        preservation = result["common_core_preservation"]
        self.assertTrue(preservation["all_common_core_satisfied"])
        self.assertTrue(preservation["evidence_complete"])
        self.assertEqual(result["iec_spec"]["availability"], "unavailable")
        self.assertEqual(
            result["iec_spec"]["missing_complete_categories"],
            ["event", "temporal"],
        )

    def test_permitted_predictions_are_outside_f1_denominator(self):
        required = make_atom("event", "event_spec", {"event": "yield"})
        permitted = make_atom(
            "event", "event_spec", {"event": "stop"}, layer="permitted"
        )
        result = score_semantic_output(
            oracle([required, permitted]),
            {
                "query_id": "q",
                "ego": {
                    "count": 1,
                    "semantic_role": "bus_proxy",
                    "approved_proxy": True,
                    "deterministic": True,
                    "length_m": 5.33,
                    "width_m": 2.10,
                    "proxy_id": "fixture",
                },
                "atoms": [required, permitted],
                "complete_categories": ["event", "temporal"],
            },
        )
        self.assertEqual(result["iec_spec"]["predicted_count"], 1)
        self.assertEqual(result["iec_spec"]["false_positive"], 0)
        self.assertEqual(result["iec_spec"]["f1"], 1.0)

    def test_diagnostic_f1_is_unavailable_without_open_set_completeness(self):
        actor = make_atom(
            "actor",
            "actor_role_count",
            {"type": "car", "role": "rear", "count": "1"},
        )
        result = score_semantic_output(
            oracle([actor]),
            {
                "query_id": "q",
                "ego": {
                    "count": 1,
                    "semantic_role": "bus_proxy",
                    "approved_proxy": True,
                    "deterministic": True,
                    "length_m": 5.33,
                    "width_m": 2.10,
                    "proxy_id": "fixture",
                },
                "atoms": [actor],
                "complete_categories": [],
            },
        )
        self.assertEqual(result["arc"]["availability"], "unavailable")
        self.assertNotIn("f1", result["arc"])

    def test_complete_open_set_inventory_penalizes_extra_tuples(self):
        cases = (
            (
                "arc",
                ("actor",),
                make_atom(
                    "actor",
                    "actor_role_count",
                    {"type": "car", "role": "rear", "count": "1"},
                ),
                make_atom(
                    "actor",
                    "actor_role_count",
                    {"type": "pedestrian", "role": "crossing", "count": "1"},
                ),
            ),
            (
                "rsc",
                ("road", "spatial"),
                make_atom("road", "road_topology", {"value": "midblock"}),
                make_atom("spatial", "actor_relative_region", {"role": "rear", "relation": "ahead"}),
            ),
            (
                "iec_spec",
                ("event", "temporal"),
                make_atom("event", "event_spec", {"event": "yield"}),
                make_atom("temporal", "before", {"first": "merge", "second": "stop"}),
            ),
        )
        for field, categories, required, extra in cases:
            with self.subTest(field=field):
                result = score_semantic_output(
                    oracle([required]),
                    {
                        "query_id": "q",
                        "ego": {
                            "count": 1,
                            "semantic_role": "bus_proxy",
                            "approved_proxy": True,
                            "deterministic": True,
                            "length_m": 5.33,
                            "width_m": 2.10,
                            "proxy_id": "fixture",
                        },
                        "atoms": [required, extra],
                        "complete_categories": list(categories),
                    },
                )
                self.assertEqual(result[field]["availability"], "available")
                self.assertEqual(result[field]["false_positive"], 1)
                self.assertLess(result[field]["f1"], 1.0)

    def test_judge_cannot_override_deterministic_observation(self):
        event = make_atom("event", "event_spec", {"event": "yield"})
        event["decision_status"] = "confirmed"
        with self.assertRaises(ValidationError):
            score_semantic_output(
                oracle([event]),
                {
                    "query_id": "q",
                    "ego": {
                        "count": 1,
                        "semantic_role": "bus_proxy",
                        "approved_proxy": True,
                        "deterministic": True,
                        "length_m": 5.33,
                        "width_m": 2.10,
                        "proxy_id": "fixture",
                    },
                    "atoms": [event],
                },
                judge_verdicts={
                    event["atom_id"]: {
                        "verdict": "violated",
                        "judge_response_id": "a" * 64,
                        "judge_response_record_sha256": "b" * 64,
                    }
                },
            )

    def test_set_f1_handles_empty_sets(self):
        self.assertEqual(set_f1([], [])["f1"], 1.0)
        self.assertEqual(set_f1(["a"], [])["f1"], 0.0)


class RobustnessMetricTests(unittest.TestCase):
    def test_aggregate_cli_accepts_explicit_uqh_assessment_inputs(self):
        args = build_parser().parse_args(
            [
                "aggregate",
                "--scores",
                "scores.jsonl",
                "--responses",
                "responses.jsonl",
                "--roster",
                "roster.json",
                "--oracle",
                "oracle.jsonl",
                "--uqh-assessments",
                "uqh_assessments.jsonl",
                "--uqh-assessor-registry",
                "uqh_assessor_registry.json",
                "--development",
            ]
        )
        self.assertEqual(args.uqh_assessments, "uqh_assessments.jsonl")
        self.assertEqual(args.uqh_assessor_registry, "uqh_assessor_registry.json")

    def test_uqh_attest_cli_exposes_external_key_and_execution_inputs(self):
        args = build_parser().parse_args(
            [
                "uqh-attest",
                "--responses",
                "responses.jsonl",
                "--oracle",
                "oracle.jsonl",
                "--roster",
                "roster.json",
                "--assessor-executions",
                "executions.jsonl",
                "--uqh-assessor-registry",
                "registry.json",
                "--assessor-id",
                "judge",
                "--private-key",
                "/secure/judge.pem",
                "--output-dir",
                "attested-bundle",
                "--development",
            ]
        )
        self.assertEqual(args.private_key, "/secure/judge.pem")
        self.assertEqual(args.assessor_executions, "executions.jsonl")
        self.assertEqual(args.output_dir, "attested-bundle")

    def test_formal_aggregate_cannot_silently_omit_uqh_assessments(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            result = main(
                [
                    "aggregate",
                    "--scores",
                    "missing-scores.jsonl",
                    "--responses",
                    "missing-responses.jsonl",
                    "--roster",
                    "missing-roster.json",
                    "--oracle",
                    "missing-oracle.jsonl",
                    "--freeze-manifest",
                    "missing-freeze.json",
                ]
            )
        self.assertEqual(result, 2)
        self.assertIn("--uqh-assessments", stderr.getvalue())

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.binding_index = 0
        config = self._file_binding("uqh-config", b'{"rubric":"v0.1"}\n')
        source = self._file_binding("uqh-source", b"frozen assessor source\n")
        self.private_key_path = self.root / "independent-assessor-private.pem"
        public_key_path = self.root / "independent-assessor-public.pem"
        subprocess.run(
            [
                "openssl",
                "genpkey",
                "-algorithm",
                "RSA",
                "-pkeyopt",
                "rsa_keygen_bits:2048",
                "-out",
                str(self.private_key_path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        subprocess.run(
            [
                "openssl",
                "pkey",
                "-in",
                str(self.private_key_path),
                "-pubout",
                "-out",
                str(public_key_path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        public_key = self._file_binding(
            "uqh-public-key", public_key_path.read_bytes()
        )
        self.assessor_registry = {
            "schema_version": "0.1",
            "status": "frozen",
            "protocol_version": "0.1",
            "assessment_binding_contract": "uqh_assessment_binding_v0.1",
            "controlled_degradation_credit": False,
            "assessors": [
                {
                    "assessor_id": "independent-uqh-judge",
                    "assessor_version": "0.1",
                    "assessment_source": "judge",
                    "independent_from_producer_ids": [PRODUCER_ID],
                    "assessment_config": config,
                    "assessor_source": source,
                    "attestation_algorithm": "rsa_pss_sha256",
                    "attestation_public_key": public_key,
                    "key_custody": "external_to_method_runner",
                }
            ],
        }
        self.roster = self._uqh_roster()

    def tearDown(self):
        self.temporary.cleanup()

    def _file_binding(self, label, payload):
        self.binding_index += 1
        path = self.root / "{}-{}".format(label, self.binding_index)
        path.write_bytes(payload)
        return {
            "path": str(path),
            "sha256": sha256_bytes(payload),
            "bytes": len(payload),
        }

    def _artifact(self, payload=b"param scene = 'valid'\n"):
        return self._file_binding("scene.scenic", payload)

    def _raw_method_response(self, run_id, stdout_bytes=None, exit_code=0, timed_out=False):
        if stdout_bytes is None:
            stdout_bytes = ("reject unsupported request for {}".format(run_id)).encode("utf-8")
        stderr_bytes = b""
        envelope = {
            "stdout": self._file_binding("{}.stdout".format(run_id), stdout_bytes),
            "stderr": self._file_binding("{}.stderr".format(run_id), stderr_bytes),
            "exit_code": exit_code,
            "timed_out": timed_out,
        }
        return dict(
            envelope,
            envelope_sha256=sha256_bytes(canonical_json_bytes(envelope)),
        )

    @staticmethod
    def _handling(raw_response, stream="stdout"):
        return {
            "response_stream": stream,
            "response_sha256": raw_response[stream]["sha256"],
        }

    @staticmethod
    def _oracle_record(query_id, expected_support, acceptable_response, reasons):
        return {
            "schema_version": "0.1",
            "query_id": query_id,
            "intent_group_id": "{}-intent".format(query_id),
            "statistical_intent_cluster_id": "{}-intent".format(query_id),
            "surface_style": "precise",
            "expected_support": expected_support,
            "acceptable_response": acceptable_response,
            "unsupported_reasons": reasons,
            "ego_gate": {
                "semantic_role": "ego_bus",
                "count": 1,
                "approved_proxy_allowed": True,
                "length_m": 5.33,
                "width_m": 2.1,
            },
            "atoms": [],
            "cpd_policy": {
                "candidate": False,
                "eligible": False,
                "dimensions": [],
                "decision_status": "confirmed",
                "cross_platform_judgeable": True,
            },
            "decision_status": "confirmed",
        }

    def _uqh_oracles(self, acceptable_response=None, reasons=None):
        return [
            self._oracle_record("supported", "supported", ["generate"], []),
            self._oracle_record(
                "unsupported",
                "unsupported",
                acceptable_response
                or ["reject", "ask_for_clarification", "controlled_degradation"],
                reasons or ["rule_state_contradiction"],
            ),
        ]

    @staticmethod
    def _uqh_roster():
        queries = [
            {
                "query_id": query_id,
                "intent_group_id": "{}-intent".format(query_id),
                "statistical_intent_cluster_id": "{}-intent".format(query_id),
                "surface_style": "precise",
                "expected_support": support,
            }
            for query_id, support in (
                ("supported", "supported"),
                ("unsupported", "unsupported"),
            )
        ]
        return {
            "schema_version": "0.1",
            "decision_status": "confirmed",
            "method_id": "method-a",
            "platform": "carla",
            "library_path": "/frozen/query-library.jsonl",
            "library_sha256": "a" * 64,
            "query_count": 2,
            "expected_response_count": 10,
            "repetitions": [0, 1, 2, 3, 4],
            "queries": queries,
        }

    @staticmethod
    def _response_identity(query_id, repetition, expected_support):
        return {
            "schema_version": "0.1",
            "run_id": "{}-{}".format(query_id, repetition),
            "method_id": "method-a",
            "platform": "carla",
            "query_id": query_id,
            "intent_group_id": "{}-intent".format(query_id),
            "statistical_intent_cluster_id": "{}-intent".format(query_id),
            "surface_style": "precise",
            "repetition": repetition,
            "expected_support": expected_support,
            "request_sha256": "b" * 64,
            "config_sha256": "c" * 64,
            "implementation_bundle_sha256": "d" * 64,
        }

    def _uqh_records(self, supported_overrides=None, unsupported_overrides=None):
        records = []
        for repetition in range(5):
            supported_run_id = "supported-{}".format(repetition)
            supported_raw = self._raw_method_response(supported_run_id)
            supported = dict(self._response_identity("supported", repetition, "supported"), **{
                "disposition": "generate",
                "terminal_status": "complete",
                "artifact": self._artifact(),
                "raw_method_response": supported_raw,
                "provenance": {
                    "producer_id": PRODUCER_ID,
                    "producer_version": "0.1",
                    "created_at_utc": "2026-07-14T00:00:00+00:00",
                },
            })
            supported.update(supported_overrides or {})
            if (
                supported["disposition"] in ("reject", "clarification", "controlled_degradation")
                and supported["terminal_status"] == "complete"
            ):
                supported.setdefault(
                    "unsupported_handling",
                    self._handling(supported["raw_method_response"]),
                )
            else:
                supported.pop("unsupported_handling", None)
            unsupported_run_id = "unsupported-{}".format(repetition)
            unsupported_raw = self._raw_method_response(unsupported_run_id)
            unsupported = dict(self._response_identity("unsupported", repetition, "unsupported"), **{
                "disposition": "reject",
                "terminal_status": "complete",
                "artifact": None,
                "raw_method_response": unsupported_raw,
                "provenance": {
                    "producer_id": PRODUCER_ID,
                    "producer_version": "0.1",
                    "created_at_utc": "2026-07-14T00:00:00+00:00",
                },
            })
            unsupported.update(unsupported_overrides or {})
            if (
                unsupported["disposition"]
                in ("reject", "clarification", "controlled_degradation")
                and unsupported["terminal_status"] == "complete"
            ):
                unsupported.setdefault(
                    "unsupported_handling",
                    self._handling(unsupported["raw_method_response"]),
                )
            else:
                unsupported.pop("unsupported_handling", None)
            records.extend((supported, unsupported))
        return records

    def _assessment_for(self, record, oracle=None, **overrides):
        response_digest = sha256_bytes(canonical_json_bytes(record))
        envelope_digest = record["raw_method_response"]["envelope_sha256"]
        if oracle is None:
            oracle = {
                value["query_id"]: value for value in self._uqh_oracles()
            }[record["query_id"]]
        selected_stream = record["unsupported_handling"]["response_stream"]
        payload = Path(record["raw_method_response"][selected_stream]["path"]).read_bytes()
        assessor = self.assessor_registry["assessors"][0]
        assessment = {
            "schema_version": "0.1",
            "run_id": record["run_id"],
            "query_id": record["query_id"],
            "disposition": record["disposition"],
            "response_record_sha256": response_digest,
            "raw_response_envelope_sha256": envelope_digest,
            "oracle_record_sha256": sha256_bytes(canonical_json_bytes(oracle)),
            "verdict": "valid",
            "reason_codes": ["rule_state_contradiction"],
            "assessment_source": "judge",
            "evidence": [
                {
                    "response_stream": selected_stream,
                    "byte_start": 0,
                    "byte_end": len(payload),
                    "bytes_sha256": sha256_bytes(payload),
                }
            ],
            "rationale": "The response safely handles the frozen unsupported reason.",
        }
        assessment.update(overrides)
        raw_assessor_output = {
            "schema_version": "0.1",
            "verdict": assessment["verdict"],
            "reason_codes": assessment["reason_codes"],
            "evidence": assessment["evidence"],
            "rationale": assessment["rationale"],
        }
        raw_assessor_response = self._file_binding(
            "uqh-raw-assessor-response",
            canonical_json_bytes(raw_assessor_output),
        )
        assessment["provenance"] = {
            "assessor_id": "independent-uqh-judge",
            "assessor_version": "0.1",
            "assessor_registry_sha256": sha256_bytes(
                canonical_json_bytes(self.assessor_registry)
            ),
            "assessment_config_sha256": assessor["assessment_config"]["sha256"],
            "assessor_source_sha256": assessor["assessor_source"]["sha256"],
            "assessor_request_sha256": "",
            "raw_assessor_request": {},
            "raw_assessor_response": raw_assessor_response,
            "external_execution_id": "independent-execution-{}".format(
                record["run_id"]
            ),
            "created_at_utc": "2026-07-14T00:00:00+00:00",
        }
        request_payload = canonical_json_bytes(
            uqh_assessor_request_payload(
                assessment, record, oracle, selected_stream, payload
            )
        )
        assessment["provenance"]["raw_assessor_request"] = self._file_binding(
            "uqh-raw-assessor-request", request_payload
        )
        assessment["provenance"]["assessor_request_sha256"] = (
            uqh_assessor_request_sha256(
                assessment, record, oracle, selected_stream, payload
            )
        )
        assessment["assessment_content_sha256"] = uqh_assessment_content_sha256(
            assessment
        )
        assessment["assessment_id"] = uqh_assessment_id(assessment)
        signed_payload = canonical_json_bytes(uqh_attestation_payload(assessment))
        signature = subprocess.run(
            [
                "openssl",
                "dgst",
                "-sha256",
                "-sign",
                str(self.private_key_path),
                "-sigopt",
                "rsa_padding_mode:pss",
                "-sigopt",
                "rsa_pss_saltlen:-1",
            ],
            input=signed_payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
        ).stdout
        assessment["attestation"] = {
            "algorithm": "rsa_pss_sha256",
            "signed_payload_sha256": sha256_bytes(signed_payload),
            "signature": self._file_binding("uqh-signature", signature),
        }
        return assessment

    def _assessments(self, records, oracles=None, **overrides):
        oracle_by_id = {
            value["query_id"]: value
            for value in (oracles if oracles is not None else self._uqh_oracles())
        }
        return [
            self._assessment_for(record, oracle_by_id[record["query_id"]], **overrides)
            for record in records
            if record["expected_support"] == "unsupported"
            and record["disposition"] in ("reject", "clarification")
            and record["terminal_status"] == "complete"
        ]

    @staticmethod
    def _executions_from_assessments(assessments):
        return [
            {
                "schema_version": "0.1",
                "run_id": assessment["run_id"],
                "external_execution_id": assessment["provenance"][
                    "external_execution_id"
                ],
                "created_at_utc": assessment["provenance"]["created_at_utc"],
                "raw_assessor_request": assessment["provenance"][
                    "raw_assessor_request"
                ],
                "raw_assessor_response": assessment["provenance"][
                    "raw_assessor_response"
                ],
            }
            for assessment in assessments
        ]

    def _compute(self, records, assessments=None, oracles=None, registry=None, roster=None):
        oracle_values = oracles or self._uqh_oracles()
        if assessments is None:
            assessments = self._assessments(records, oracles=oracle_values)
        return compute_uqh(
            records,
            oracle_values,
            assessments,
            roster or self.roster,
            registry or self.assessor_registry,
        )

    def test_uqh_perfect(self):
        records = self._uqh_records()
        result = self._compute(records)
        self.assertEqual(result["uqh"], 1.0)
        self.assertEqual(result["valid_unsupported_handling"], 1.0)
        self.assertEqual(result["required_assessment_count"], 5)
        self.assertEqual(result["assessment_count"], 5)
        self.assertEqual(
            result["provenance"]["assessment_binding_contract"],
            "uqh_assessment_binding_v0.1",
        )

    def test_uqh_generate_all_and_reject_all_are_zero(self):
        generate_all = self._uqh_records(
            unsupported_overrides={
                "disposition": "generate",
                "artifact": self._artifact(),
            }
        )
        reject_all = self._uqh_records(
            supported_overrides={
                "disposition": "reject",
                "artifact": None,
            }
        )
        self.assertEqual(
            self._compute(generate_all, assessments=[])["uqh"],
            0.0,
        )
        self.assertEqual(
            self._compute(reject_all)["uqh"],
            0.0,
        )

    def test_uqh_supported_requires_completed_nonempty_artifact(self):
        for overrides in (
            {"terminal_status": "failed", "disposition": "failure", "artifact": None},
            {"artifact": self._artifact(b"")},
        ):
            with self.subTest(overrides=overrides):
                records = self._uqh_records(supported_overrides=overrides)
                result = self._compute(records)
                self.assertEqual(result["supported_acceptance"], 0.0)
                self.assertEqual(result["uqh"], 0.0)
        records = self._uqh_records()
        for target in records[::2]:
            target.update(
                {
                    "terminal_status": "timeout",
                    "disposition": "failure",
                    "artifact": None,
                    "raw_method_response": self._raw_method_response(
                        target["run_id"], exit_code=None, timed_out=True
                    ),
                }
            )
        result = self._compute(records)
        self.assertEqual(result["supported_acceptance"], 0.0)

    def test_uqh_unsupported_reason_comes_only_from_independent_assessment(self):
        records = self._uqh_records()
        forged_assessments = self._assessments(
            records,
            reason_codes=["invented_reason"],
        )
        result = self._compute(records, forged_assessments)
        self.assertEqual(result["valid_unsupported_handling"], 0.0)

    def test_uqh_missing_assessment_is_pipeline_incomplete(self):
        records = self._uqh_records()
        with self.assertRaisesRegex(ValidationError, "pipeline is incomplete"):
            self._compute(records, assessments=[])

    def test_uqh_rejects_assessment_with_forged_response_binding(self):
        records = self._uqh_records()
        assessments = self._assessments(records)
        assessments[0]["response_record_sha256"] = "f" * 64
        with self.assertRaises(ValidationError):
            self._compute(records, assessments)

    def test_uqh_rejects_assessment_after_raw_response_is_replaced(self):
        records = self._uqh_records()
        assessments = self._assessments(records)
        target = records[1]
        target["raw_method_response"] = self._raw_method_response("replacement")
        target["unsupported_handling"] = self._handling(target["raw_method_response"])
        with self.assertRaises(ValidationError):
            self._compute(records, assessments)

    def test_uqh_rejects_corrupt_raw_response_envelope(self):
        records = self._uqh_records()
        records[1]["raw_method_response"]["envelope_sha256"] = "f" * 64
        with self.assertRaises(ValidationError):
            self._compute(records)

    def test_uqh_authoritative_aggregate_rejects_duplicate_raw_json_keys(self):
        records = self._uqh_records()
        assessments = self._assessments(records)
        target = assessments[0]
        raw_path = Path(target["provenance"]["raw_assessor_response"]["path"])
        duplicate = raw_path.read_bytes().replace(
            b'"verdict":"valid"',
            b'"verdict":"invalid","verdict":"valid"',
        )
        target["provenance"]["raw_assessor_response"] = self._file_binding(
            "aggregate-duplicate-assessor-key", duplicate
        )
        target["assessment_content_sha256"] = uqh_assessment_content_sha256(target)
        target["assessment_id"] = uqh_assessment_id(target)
        signed_payload = canonical_json_bytes(uqh_attestation_payload(target))
        signature = subprocess.run(
            [
                "openssl",
                "dgst",
                "-sha256",
                "-sign",
                str(self.private_key_path),
                "-sigopt",
                "rsa_padding_mode:pss",
                "-sigopt",
                "rsa_pss_saltlen:-1",
            ],
            input=signed_payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
        ).stdout
        target["attestation"] = {
            "algorithm": "rsa_pss_sha256",
            "signed_payload_sha256": sha256_bytes(signed_payload),
            "signature": self._file_binding(
                "aggregate-duplicate-assessor-signature", signature
            ),
        }
        with self.assertRaisesRegex(ValidationError, "strict UTF-8 JSON"):
            self._compute(records, assessments)

    def test_jsonl_reader_rejects_duplicate_keys_and_non_json_constants(self):
        for name, payload in (
            ("duplicate", b'{"verdict":"invalid","verdict":"valid"}\n'),
            ("nan", b'{"value":NaN}\n'),
        ):
            with self.subTest(name=name):
                path = self.root / "{}.jsonl".format(name)
                path.write_bytes(payload)
                with self.assertRaisesRegex(ValidationError, "strict UTF-8 JSON"):
                    read_jsonl(path)

    def test_uqh_assessor_must_be_independent_of_response_producer(self):
        records = self._uqh_records()
        assessments = self._assessments(records)
        assessments[0]["provenance"]["assessor_id"] = PRODUCER_ID
        assessments[0]["assessment_content_sha256"] = uqh_assessment_content_sha256(
            assessments[0]
        )
        assessments[0]["assessment_id"] = uqh_assessment_id(assessments[0])
        with self.assertRaises(ValidationError):
            self._compute(records, assessments)

    def test_uqh_rejects_rogue_response_producer_and_registry_allowlist(self):
        records = self._uqh_records()
        records[0]["provenance"]["producer_id"] = "rogue-generation-runner"
        with self.assertRaisesRegex(ValidationError, "response producer"):
            self._compute(records)

        records = self._uqh_records()
        registry = dict(self.assessor_registry)
        registry["assessors"] = [dict(self.assessor_registry["assessors"][0])]
        registry["assessors"][0]["independent_from_producer_ids"] = [
            PRODUCER_ID,
            "rogue-generation-runner",
        ]
        with self.assertRaises(ValidationError):
            self._compute(records, registry=registry)

    def test_uqh_rejects_duplicate_and_orphan_assessments(self):
        records = self._uqh_records()
        assessments = self._assessments(records)
        with self.assertRaises(ValidationError):
            self._compute(records, assessments + [dict(assessments[0])])

        orphan = dict(assessments[0], run_id="absent-run")
        orphan["assessment_id"] = uqh_assessment_id(orphan)
        with self.assertRaises(ValidationError):
            self._compute(records, [orphan])

    def test_uqh_method_payload_cannot_embed_evaluator_fields(self):
        records = self._uqh_records()
        records[1]["unsupported_handling"]["reason_code"] = "rule_state_contradiction"
        records[1]["unsupported_handling"]["valid"] = True
        with self.assertRaises(ValidationError):
            self._compute(records)

    def test_uqh_unbound_method_response_locator_fails_closed(self):
        records = self._uqh_records()
        records[1]["unsupported_handling"]["response_sha256"] = "f" * 64
        result = self._compute(records)
        self.assertAlmostEqual(result["valid_unsupported_handling"], 0.8)

    def test_uqh_rejects_raw_timeout_nonzero_and_empty_response(self):
        for exit_code, timed_out, stdout in ((1, False, b"reject"), (0, True, b"reject")):
            with self.subTest(exit_code=exit_code, timed_out=timed_out):
                records = self._uqh_records()
                target = records[1]
                target["raw_method_response"] = self._raw_method_response(
                    target["run_id"], stdout_bytes=stdout, exit_code=exit_code, timed_out=timed_out
                )
                target["unsupported_handling"] = self._handling(target["raw_method_response"])
                with self.assertRaises(ValidationError):
                    self._compute(records)
        records = self._uqh_records()
        target = records[1]
        target["raw_method_response"] = self._raw_method_response(
            target["run_id"], stdout_bytes=b""
        )
        target["unsupported_handling"] = self._handling(target["raw_method_response"])
        assessments = self._assessments(records)
        assessments = [
            self._assessment_for(record, verdict="invalid", reason_codes=[])
            if record["run_id"] == target["run_id"]
            else self._assessment_for(record)
            for record in records
            if record["expected_support"] == "unsupported"
            and record["disposition"] in ("reject", "clarification")
        ]
        result = self._compute(records, assessments)
        self.assertAlmostEqual(result["valid_unsupported_handling"], 0.8)

    def test_uqh_clarification_uses_oracle_disposition_mapping(self):
        records = self._uqh_records(
            unsupported_overrides={"disposition": "clarification"}
        )
        result = self._compute(
            records,
            oracles=self._uqh_oracles(acceptable_response=["ask_for_clarification"]),
        )
        self.assertEqual(result["valid_unsupported_handling"], 1.0)

    def test_uqh_controlled_degradation_always_scores_zero_in_v0_1(self):
        records = self._uqh_records(
            unsupported_overrides={
                "disposition": "controlled_degradation",
                "artifact": self._artifact(),
            }
        )
        result = self._compute(records, assessments=[])
        self.assertEqual(result["valid_unsupported_handling"], 0.0)

    def test_uqh_rejects_response_support_label_spoofing(self):
        records = self._uqh_records()
        records[0]["expected_support"] = "unsupported"
        with self.assertRaises(ValidationError):
            self._compute(records)

    def test_uqh_recomputes_stream_artifact_and_registry_file_hashes(self):
        records = self._uqh_records()
        Path(records[1]["raw_method_response"]["stdout"]["path"]).write_bytes(b"tampered")
        with self.assertRaisesRegex(ValidationError, "byte binding"):
            self._compute(records)
        records = self._uqh_records()
        Path(records[0]["artifact"]["path"]).write_bytes(b"tampered")
        with self.assertRaisesRegex(ValidationError, "byte binding"):
            self._compute(records)
        records = self._uqh_records()
        registry = dict(self.assessor_registry)
        registry["assessors"] = [dict(self.assessor_registry["assessors"][0])]
        Path(registry["assessors"][0]["assessment_config"]["path"]).write_bytes(b"tampered")
        with self.assertRaisesRegex(ValidationError, "byte binding"):
            self._compute(records, registry=registry)

    def test_uqh_assessment_id_binds_verdict_evidence_rationale_and_provenance(self):
        mutations = (
            lambda value: value.update(verdict="invalid"),
            lambda value: value["evidence"][0].update(byte_end=0),
            lambda value: value.update(rationale="changed"),
            lambda value: value["provenance"].update(created_at_utc="2026-07-15T00:00:00+00:00"),
        )
        for mutate in mutations:
            records = self._uqh_records()
            assessments = self._assessments(records)
            mutate(assessments[0])
            with self.subTest(mutation=mutate):
                with self.assertRaises(ValidationError):
                    self._compute(records, assessments)

    def test_uqh_rejects_unsigned_or_self_minted_assessment(self):
        records = self._uqh_records()
        assessments = self._assessments(records)
        assessments[0].pop("attestation")
        with self.assertRaises(ValidationError):
            self._compute(records, assessments)

        assessments = self._assessments(records)
        assessments[0]["rationale"] = "Self-minted replacement."
        raw_output = {
            "schema_version": "0.1",
            "verdict": assessments[0]["verdict"],
            "reason_codes": assessments[0]["reason_codes"],
            "evidence": assessments[0]["evidence"],
            "rationale": assessments[0]["rationale"],
        }
        assessments[0]["provenance"]["raw_assessor_response"] = self._file_binding(
            "forged-raw-assessor-response", canonical_json_bytes(raw_output)
        )
        assessments[0]["assessment_content_sha256"] = uqh_assessment_content_sha256(
            assessments[0]
        )
        assessments[0]["assessment_id"] = uqh_assessment_id(assessments[0])
        forged_payload = canonical_json_bytes(uqh_attestation_payload(assessments[0]))
        assessments[0]["attestation"]["signed_payload_sha256"] = sha256_bytes(
            forged_payload
        )
        with self.assertRaisesRegex(ValidationError, "signature is invalid"):
            self._compute(records, assessments)

    def test_uqh_recomputes_raw_assessor_request_response_and_signature_bytes(self):
        for binding_path in (
            ("provenance", "raw_assessor_request"),
            ("provenance", "raw_assessor_response"),
            ("attestation", "signature"),
        ):
            records = self._uqh_records()
            assessments = self._assessments(records)
            binding = assessments[0][binding_path[0]][binding_path[1]]
            Path(binding["path"]).write_bytes(b"tampered")
            with self.subTest(binding=binding_path):
                with self.assertRaisesRegex(ValidationError, "byte binding"):
                    self._compute(records, assessments)

    def test_uqh_direct_api_rejects_mixed_main_configs(self):
        records = self._uqh_records()
        records[1]["config_sha256"] = "d" * 64
        with self.assertRaisesRegex(ValidationError, "one frozen main configuration"):
            self._compute(records)
        records = self._uqh_records()
        with self.assertRaisesRegex(ValidationError, "frozen method config"):
            compute_uqh(
                records,
                self._uqh_oracles(),
                self._assessments(records),
                self.roster,
                self.assessor_registry,
                expected_config_sha256="d" * 64,
            )

    def test_uqh_attestation_workflow_reconstructs_and_signs_external_outputs(self):
        records = self._uqh_records()
        seed_assessments = self._assessments(records)
        executions = self._executions_from_assessments(seed_assessments)
        output_directory = self.root / "attestation-workflow"
        assessments = create_signed_uqh_assessments(
            records,
            self._uqh_oracles(),
            executions,
            self.roster,
            self.assessor_registry,
            "independent-uqh-judge",
            self.private_key_path,
            output_directory,
            expected_config_sha256="c" * 64,
        )
        result = compute_uqh(
            records,
            self._uqh_oracles(),
            assessments,
            self.roster,
            self.assessor_registry,
            expected_config_sha256="c" * 64,
        )
        self.assertEqual(result["uqh"], 1.0)
        self.assertEqual(
            read_jsonl(output_directory / "assessment_index.jsonl"), assessments
        )
        self.assertTrue(
            all(Path(value["attestation"]["signature"]["path"]).is_file() for value in assessments)
        )

    def test_uqh_request_and_attest_real_cli_flow_is_immutable(self):
        records = self._uqh_records()
        oracles = self._uqh_oracles()
        responses_path = self.root / "responses.jsonl"
        oracle_path = self.root / "oracle.jsonl"
        roster_path = self.root / "roster.json"
        registry_path = self.root / "registry.json"
        write_jsonl(responses_path, records)
        write_jsonl(oracle_path, oracles)
        write_json(roster_path, self.roster)
        write_json(registry_path, self.assessor_registry)

        request_output = self.root / "uqh-request-bundle"
        request_args = [
            "uqh-request",
            "--responses",
            str(responses_path),
            "--oracle",
            str(oracle_path),
            "--roster",
            str(roster_path),
            "--uqh-assessor-registry",
            str(registry_path),
            "--assessor-id",
            "independent-uqh-judge",
            "--output-dir",
            str(request_output),
            "--development",
        ]
        formal_stderr = io.StringIO()
        formal_args = request_args[:-1]
        formal_args[formal_args.index(str(request_output))] = str(
            self.root / "formal-request-bundle"
        )
        with redirect_stderr(formal_stderr):
            self.assertEqual(main(formal_args), 2)
        self.assertIn("--generation-library", formal_stderr.getvalue())
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(main(request_args), 0)
        request_index = request_output / "request_index.jsonl"
        requests = read_jsonl(request_index)
        self.assertEqual(len(requests), 5)
        immutable_request_bytes = request_index.read_bytes()
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            self.assertEqual(main(request_args), 2)
        self.assertEqual(request_index.read_bytes(), immutable_request_bytes)

        response_by_run = {record["run_id"]: record for record in records}
        executions = []
        for request in requests:
            response = response_by_run[request["run_id"]]
            stream = response["unsupported_handling"]["response_stream"]
            method_bytes = Path(
                response["raw_method_response"][stream]["path"]
            ).read_bytes()
            raw_output = {
                "schema_version": "0.1",
                "verdict": "valid",
                "reason_codes": ["rule_state_contradiction"],
                "evidence": [
                    {
                        "response_stream": stream,
                        "byte_start": 0,
                        "byte_end": len(method_bytes),
                        "bytes_sha256": sha256_bytes(method_bytes),
                    }
                ],
                "rationale": "Independent assessor accepted the safe rejection.",
            }
            executions.append(
                {
                    "schema_version": "0.1",
                    "run_id": request["run_id"],
                    "external_execution_id": "provider-{}".format(request["run_id"]),
                    "created_at_utc": "2026-07-14T00:00:00+00:00",
                    "raw_assessor_request": request["raw_assessor_request"],
                    "raw_assessor_response": self._file_binding(
                        "cli-raw-assessor-response", canonical_json_bytes(raw_output)
                    ),
                }
            )
        executions_path = self.root / "executions.jsonl"
        write_jsonl(executions_path, executions)
        attested_output = self.root / "uqh-attested-bundle"
        with redirect_stdout(io.StringIO()):
            self.assertEqual(
                main(
                    [
                        "uqh-attest",
                        "--responses",
                        str(responses_path),
                        "--oracle",
                        str(oracle_path),
                        "--roster",
                        str(roster_path),
                        "--assessor-executions",
                        str(executions_path),
                        "--uqh-assessor-registry",
                        str(registry_path),
                        "--assessor-id",
                        "independent-uqh-judge",
                        "--private-key",
                        str(self.private_key_path),
                        "--output-dir",
                        str(attested_output),
                        "--development",
                    ]
                ),
                0,
            )
        assessments = read_jsonl(attested_output / "assessment_index.jsonl")
        self.assertEqual(self._compute(records, assessments)["uqh"], 1.0)

    def test_uqh_attest_private_key_permissions_wrong_key_and_repo_path(self):
        records = self._uqh_records()
        executions = self._executions_from_assessments(self._assessments(records))
        self.private_key_path.chmod(0o400)
        output_0400 = self.root / "mode-0400"
        create_signed_uqh_assessments(
            records,
            self._uqh_oracles(),
            executions,
            self.roster,
            self.assessor_registry,
            "independent-uqh-judge",
            self.private_key_path,
            output_0400,
        )
        self.assertTrue((output_0400 / "assessment_index.jsonl").is_file())

        self.private_key_path.chmod(0o640)
        with self.assertRaisesRegex(ValidationError, "owner-only"):
            create_signed_uqh_assessments(
                records,
                self._uqh_oracles(),
                executions,
                self.roster,
                self.assessor_registry,
                "independent-uqh-judge",
                self.private_key_path,
                self.root / "bad-mode",
            )
        self.private_key_path.chmod(0o600)

        wrong_key = self.root / "wrong-private.pem"
        subprocess.run(
            [
                "openssl",
                "genpkey",
                "-algorithm",
                "RSA",
                "-pkeyopt",
                "rsa_keygen_bits:2048",
                "-out",
                str(wrong_key),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        with self.assertRaisesRegex(ValidationError, "does not match"):
            create_signed_uqh_assessments(
                records,
                self._uqh_oracles(),
                executions,
                self.roster,
                self.assessor_registry,
                "independent-uqh-judge",
                wrong_key,
                self.root / "wrong-key-output",
            )

        fake_module = self.root / "bus_benchmark" / "uqh.py"
        with patch("bus_benchmark.uqh.__file__", str(fake_module)):
            with self.assertRaisesRegex(ValidationError, "outside"):
                create_signed_uqh_assessments(
                    records,
                    self._uqh_oracles(),
                    executions,
                    self.roster,
                    self.assessor_registry,
                    "independent-uqh-judge",
                    self.private_key_path,
                    self.root / "repo-key-output",
                )

        fake_repository = self.root / "fake-repository"
        (fake_repository / "bus_benchmark").mkdir(parents=True)
        repository_key = fake_repository / "private.pem"
        repository_key.write_bytes(self.private_key_path.read_bytes())
        repository_key.chmod(0o600)
        outside_symlink = self.root / "external-private-link.pem"
        outside_symlink.symlink_to(repository_key)
        with patch(
            "bus_benchmark.uqh.__file__",
            str(fake_repository / "bus_benchmark" / "uqh.py"),
        ):
            with self.assertRaisesRegex(ValidationError, "outside"):
                create_signed_uqh_assessments(
                    records,
                    self._uqh_oracles(),
                    executions,
                    self.roster,
                    self.assessor_registry,
                    "independent-uqh-judge",
                    outside_symlink,
                    self.root / "symlink-repo-key-output",
                )

    def test_uqh_attest_staging_failure_leaves_no_partial_bundle(self):
        records = self._uqh_records()
        executions = self._executions_from_assessments(self._assessments(records))
        output_directory = self.root / "failed-attestation"
        from bus_benchmark import uqh as uqh_module

        original_sign = uqh_module._sign_payload
        call_count = {"value": 0}

        def fail_second_signature(private_key_path, payload):
            call_count["value"] += 1
            if call_count["value"] == 2:
                raise ValidationError("synthetic signer failure")
            return original_sign(private_key_path, payload)

        with patch("bus_benchmark.uqh._sign_payload", side_effect=fail_second_signature):
            with self.assertRaisesRegex(ValidationError, "synthetic signer failure"):
                create_signed_uqh_assessments(
                    records,
                    self._uqh_oracles(),
                    executions,
                    self.roster,
                    self.assessor_registry,
                    "independent-uqh-judge",
                    self.private_key_path,
                    output_directory,
                )
        self.assertFalse(output_directory.exists())
        self.assertFalse((self.root / ".failed-attestation.lock").exists())
        self.assertEqual(list(self.root.glob(".failed-attestation-staging-*")), [])

    def test_uqh_attest_rejects_non_strict_assessor_json_before_publication(self):
        records = self._uqh_records()
        executions = self._executions_from_assessments(self._assessments(records))
        original = Path(executions[0]["raw_assessor_response"]["path"]).read_bytes()
        executions[0]["raw_assessor_response"] = self._file_binding(
            "duplicate-assessor-key",
            original[:-1] + b',"verdict":"invalid"}',
        )
        output_directory = self.root / "non-strict-assessor-json"
        with self.assertRaisesRegex(ValidationError, "strict UTF-8 JSON"):
            create_signed_uqh_assessments(
                records,
                self._uqh_oracles(),
                executions,
                self.roster,
                self.assessor_registry,
                "independent-uqh-judge",
                self.private_key_path,
                output_directory,
            )
        self.assertFalse(output_directory.exists())

    def test_uqh_attest_rejects_rogue_generation_producer_before_publication(self):
        records = self._uqh_records()
        executions = self._executions_from_assessments(self._assessments(records))
        records[-1]["provenance"]["producer_id"] = "rogue-generation-runner"
        output_directory = self.root / "rogue-producer-output"
        with self.assertRaisesRegex(ValidationError, "response producer"):
            create_signed_uqh_assessments(
                records,
                self._uqh_oracles(),
                executions,
                self.roster,
                self.assessor_registry,
                "independent-uqh-judge",
                self.private_key_path,
                output_directory,
            )
        self.assertFalse(output_directory.exists())

    def test_uqh_valid_assessment_must_cover_all_oracle_reason_codes(self):
        records = self._uqh_records()
        oracles = self._uqh_oracles(
            reasons=["rule_state_contradiction", "unsupported_map_semantics"]
        )
        partial = self._assessments(records, oracles=oracles)
        result = self._compute(records, partial, oracles=oracles)
        self.assertEqual(result["valid_unsupported_handling"], 0.0)
        complete = self._assessments(
            records,
            oracles=oracles,
            reason_codes=["rule_state_contradiction", "unsupported_map_semantics"],
        )
        result = self._compute(records, complete, oracles=oracles)
        self.assertEqual(result["valid_unsupported_handling"], 1.0)

    def test_uqh_direct_api_requires_exact_schema_roster_and_oracle_coverage(self):
        records = self._uqh_records()
        with self.assertRaises(ValidationError):
            self._compute(
                records,
                assessments=self._assessments(records),
                oracles=self._uqh_oracles()[:-1],
            )
        records[0].pop("request_sha256")
        with self.assertRaises(ValidationError):
            self._compute(records)

    def test_rqs_uses_worst_style_mean(self):
        scores = []
        for style, values in {
            "precise": [1, 1, 1, 1, 1],
            "partial": [0.8, 0.6, 0.7, 0.7, 0.7],
            "vague": [0.4, 0.2, 0.3, 0.3, 0.3],
        }.items():
            for repetition, value in enumerate(values):
                scores.append(
                    {
                        "query_id": "g_{}".format(style),
                        "repetition": repetition,
                        "intent_group_id": "g",
                        "surface_style": style,
                        "expected_support": "supported",
                        "srs": value,
                    }
                )
        result = compute_rqs(scores)
        self.assertAlmostEqual(result["rqs"], 0.3)
        self.assertAlmostEqual(result["vague_gap"], 0.7)
        self.assertEqual(result["aggregation_unit"], "statistical_intent_cluster_id")
        self.assertEqual(result["source_intent_count"], 1)
        self.assertEqual(result["statistical_cluster_count"], 1)

    def test_rqs_collapses_confirmed_duplicate_intents_into_one_macro_unit(self):
        scores = []
        for intent_id, value in (("intent-a", 1.0), ("intent-b", 0.0)):
            for style in ("precise", "partial", "vague"):
                for repetition in range(5):
                    scores.append(
                        {
                            "query_id": "{}_{}".format(intent_id, style),
                            "repetition": repetition,
                            "intent_group_id": intent_id,
                            "statistical_intent_cluster_id": "duplicate-cluster",
                            "surface_style": style,
                            "expected_support": "supported",
                            "srs": value,
                        }
                    )
        result = compute_rqs(scores)
        self.assertEqual(result["source_intent_count"], 2)
        self.assertEqual(result["statistical_cluster_count"], 1)
        self.assertAlmostEqual(result["rqs"], 0.5)

    def test_one_run_per_surface_is_rejected(self):
        scores = [
            {
                "query_id": "g_{}".format(style),
                "repetition": 0,
                "intent_group_id": "g",
                "surface_style": style,
                "expected_support": "supported",
                "srs": 1.0,
            }
            for style in ("precise", "partial", "vague")
        ]
        with self.assertRaises(ValidationError):
            compute_rqs(scores)

    def test_forged_out_of_range_scores_are_rejected(self):
        forged = []
        for style in ("precise", "partial", "vague"):
            for repetition in range(5):
                forged.append(
                    {
                        "query_id": "g_{}".format(style),
                        "repetition": repetition,
                        "intent_group_id": "g",
                        "surface_style": style,
                        "expected_support": "supported",
                        "srs": 999.0,
                        "arc": {"f1": 999.0},
                        "rsc": {"f1": 999.0},
                        "iec_spec": {"f1": 999.0},
                        "ego_gate_passed": True,
                    }
                )
        with self.assertRaises(ValidationError):
            compute_rqs(forged)
        with self.assertRaises(ValidationError):
            aggregate_semantic_metrics(forged)


if __name__ == "__main__":
    unittest.main()
