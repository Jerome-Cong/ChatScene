import copy
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from bus_benchmark.fixture_extractor_common import METADRIVE_PYTHON, OBSERVER
from bus_benchmark.errors import FreezeError, ValidationError
from bus_benchmark.freeze import _validate_native_fixture_input
from bus_benchmark.jsonio import (
    canonical_json_bytes,
    read_json,
    sha256_bytes,
    sha256_file,
    write_json,
)
from bus_benchmark.metadrive_pg import (
    AUDITED_TOKEN_CLASSES,
    MetaDrivePGError,
    validate_artifact,
)
from bus_benchmark.schema import validate_schema_instance
from tests.bus_benchmark.formal_freeze_fixture import (
    METADRIVE_STEP_SECONDS,
    _metadrive_document,
    _native_extractor_fixture_input,
    _physical_template,
)


APPROVED_EGO_FACT = {
    "kind": "ego",
    "ego_variant": "approved",
}

ROOT = Path(__file__).resolve().parents[2]
NATIVE_PROBE = ROOT / "scripts" / "metadrive_track_probe.py"
NATIVE_PROBE_EVIDENCE = (
    ROOT
    / "benchmark_artifacts"
    / "evidence"
    / "metadrive_native_probe_evidence_v0_1.json"
)
METADRIVE_PLATFORM_CONFIG = (
    ROOT / "benchmark_configs" / "platforms" / "metadrive_runtime_draft.json"
)
METADRIVE_TOKEN_REGISTRY = (
    ROOT
    / "benchmark_configs"
    / "methods"
    / "metadrive_pg_token_registry_draft.json"
)
METADRIVE_SOURCE = Path("/home/shijie20/CodeSpace/mdsn/metadrive")


class MetaDriveRuntimeContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir="/tmp")
        self.root = Path(self.temporary.name)
        registry = read_json(
            Path(__file__).resolve().parents[2]
            / "benchmark_configs"
            / "methods"
            / "metadrive_pg_token_registry_draft.json"
        )
        registry["decision_status"] = "frozen"
        self.registry = self.root / "registry.json"
        write_json(self.registry, registry)
        self.registry_binding = {
            "path": str(self.registry.resolve()),
            "sha256": sha256_file(self.registry),
            "bytes": self.registry.stat().st_size,
        }

    def tearDown(self):
        self.temporary.cleanup()

    def test_formal_document_uses_only_pg_block_tokens(self):
        document = _metadrive_document(
            _physical_template(APPROVED_EGO_FACT),
            "runtime-contract",
            self.registry_binding,
        )

        self.assertEqual(document["artifact_type"], "metadrive_pg_block_scene_v0.1")
        self.assertEqual(document["time_step_s"], METADRIVE_STEP_SECONDS)
        self.assertEqual(document["map"]["generation_type"], "block_sequence")
        self.assertEqual(document["map"]["block_sequence"], "S")
        self.assertEqual(
            document["map"]["token_registry_sha256"],
            self.registry_binding["sha256"],
        )
        self.assertNotIn("map_features", document)
        self.assertNotIn("map_path", document["map"])
        self.assertEqual(document["ego"]["vehicle_model"], "xl")

    def test_registry_excludes_native_broken_forks_and_constrains_parking(self):
        self.assertNotIn("F", AUDITED_TOKEN_CLASSES)
        self.assertNotIn("f", AUDITED_TOKEN_CLASSES)
        self.assertEqual(
            {entry["token"] for entry in read_json(self.registry)["tokens"]},
            set(AUDITED_TOKEN_CLASSES),
        )
        constraints = {
            entry["token"]: entry["map_parameter_constraints"][
                "lane_num_allowed"
            ]
            for entry in read_json(self.registry)["tokens"]
        }
        self.assertEqual(constraints["P"], [1])
        self.assertEqual(constraints["S"], [1, 2, 3, 4, 5])

        document = _metadrive_document(
            _physical_template(APPROVED_EGO_FACT),
            "runtime-parking-contract",
            self.registry_binding,
        )
        document["map"]["block_sequence"] = "P"
        with self.assertRaisesRegex(MetaDrivePGError, "do not allow lane_num 2"):
            validate_artifact(
                document,
                read_json(self.registry),
                self.registry_binding["sha256"],
            )
        document["map"]["lane_num"] = 1
        validate_artifact(
            document,
            read_json(self.registry),
            self.registry_binding["sha256"],
        )

        document["map"]["block_sequence"] = "F"
        with self.assertRaisesRegex(MetaDrivePGError, "non-whitelisted"):
            validate_artifact(
                document,
                read_json(self.registry),
                self.registry_binding["sha256"],
            )

    def test_live_registry_tokens_materialize_and_native_failures_stay_excluded(self):
        probe = r'''
import json
import sys
from metadrive.envs.metadrive_env import MetaDriveEnv

tokens = json.loads(sys.argv[1])
results = []
cases = [(token, 1 if token == "P" else 2, token) for token in tokens]
cases += [("F", 2, "F"), ("f", 2, "f"), ("P", 2, "P@lane2")]
for token, lane_num, case_id in cases:
    environment = None
    try:
        environment = MetaDriveEnv({
            "map_config": {
                "type": "block_sequence",
                "config": token,
                "lane_num": lane_num,
                "lane_width": 3.5,
                "exit_length": 50.0,
            },
            "num_scenarios": 1,
            "start_seed": 0,
            "random_spawn_lane_index": False,
            "traffic_density": 0.0,
            "random_traffic": False,
            "random_lane_width": False,
            "random_lane_num": False,
            "use_render": False,
            "show_terrain": False,
            "show_skybox": False,
            "show_interface": False,
            "log_level": 50,
            "horizon": 1,
        })
        environment.reset(seed=0)
        results.append({
            "case_id": case_id,
            "actual_block_ids": [block.ID for block in environment.current_map.blocks],
            "error": None,
        })
    except Exception as exc:
        results.append({
            "case_id": case_id,
            "actual_block_ids": None,
            "error": "{}: {}".format(type(exc).__name__, exc),
        })
    finally:
        if environment is not None:
            environment.close()
print(json.dumps(results, sort_keys=True, separators=(",", ":")))
'''
        process = subprocess.run(
            [
                METADRIVE_PYTHON,
                "-c",
                probe,
                json.dumps(sorted(AUDITED_TOKEN_CLASSES)),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=45,
            check=False,
            env={
                "PATH": os.environ.get("PATH", ""),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "MPLCONFIGDIR": "/tmp",
                "PYTHONHASHSEED": "0",
            },
        )
        self.assertEqual(
            process.returncode,
            0,
            process.stderr.decode("utf-8", errors="replace")[-2000:],
        )
        by_token = {
            row["case_id"]: row
            for row in json.loads(process.stdout.decode("utf-8"))
        }
        for token in AUDITED_TOKEN_CLASSES:
            self.assertIsNone(by_token[token]["error"], by_token[token])
            self.assertEqual(by_token[token]["actual_block_ids"], ["I", token])
        for token in ("F", "f"):
            self.assertIn("Bug exists in this block", by_token[token]["error"])
        self.assertIn(
            "Lane number of previous block must be 1",
            by_token["P@lane2"]["error"],
        )

    def test_native_observer_serializes_pgmap_numpy_features(self):
        document = _metadrive_document(
            _physical_template(APPROVED_EGO_FACT),
            "runtime-observer-json",
            self.registry_binding,
        )
        process = subprocess.run(
            [
                METADRIVE_PYTHON,
                str(OBSERVER),
                "metadrive",
                "--token-registry",
                str(self.registry),
            ],
            input=canonical_json_bytes(document),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
            env={
                "PATH": os.environ.get("PATH", ""),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "MPLCONFIGDIR": "/tmp",
                "PYTHONHASHSEED": "0",
            },
        )
        self.assertEqual(
            process.returncode,
            0,
            process.stderr.decode("utf-8", errors="replace")[-2000:],
        )
        observation = json.loads(process.stdout.decode("utf-8"))
        features = list(observation["map_features"].values())
        self.assertTrue(features)
        self.assertIsInstance(features[0]["polyline"], list)

    def test_freeze_rejects_pre_runtime_track_state(self):
        fixture = _native_extractor_fixture_input(
            "metadrive",
            APPROVED_EGO_FACT,
            "runtime-freeze-contract",
            self.registry_binding,
        )
        _validate_native_fixture_input(fixture, "metadrive", "runtime fixture")

        old_fixture = copy.deepcopy(fixture)
        document = json.loads(old_fixture["artifact"]["text"])
        document["map_features"] = {}
        encoded = canonical_json_bytes(document)
        old_fixture["artifact"]["text"] = encoded.decode("utf-8")
        old_fixture["artifact"]["bytes"] = len(encoded)
        old_fixture["artifact"]["sha256"] = sha256_bytes(encoded)

        with self.assertRaisesRegex(FreezeError, "invalid MetaDrive PG"):
            _validate_native_fixture_input(
                old_fixture,
                "metadrive",
                "pre-runtime fixture",
            )

    def test_formal_document_resets_and_steps_with_xl_proxy(self):
        document = _metadrive_document(
            _physical_template(APPROVED_EGO_FACT),
            "runtime-xl-proxy",
            self.registry_binding,
        )
        process = subprocess.run(
            [
                METADRIVE_PYTHON,
                str(OBSERVER),
                "metadrive",
                "--runtime-probe",
                "--token-registry",
                str(self.registry),
            ],
            input=canonical_json_bytes(document),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
            env={
                "PATH": os.environ.get("PATH", ""),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "MPLCONFIGDIR": "/tmp",
                "PYTHONHASHSEED": "0",
            },
        )
        self.assertEqual(
            process.returncode,
            0,
            process.stderr.decode("utf-8", errors="replace")[-2000:],
        )
        result = json.loads(process.stdout.decode("utf-8"))
        self.assertTrue(result["reset_succeeded"])
        self.assertTrue(result["step_succeeded"])
        self.assertEqual(result["vehicle_class"], "XLVehicle")
        self.assertEqual(result["vehicle_model"], "xl")
        self.assertAlmostEqual(result["physical_length_m"], 5.74)
        self.assertAlmostEqual(result["physical_width_m"], 2.3)
        self.assertAlmostEqual(result["physical_height_m"], 2.8)
        self.assertAlmostEqual(result["top_down_length_m"], 5.33)
        self.assertAlmostEqual(result["top_down_width_m"], 2.10)
        self.assertEqual(result["actual_block_ids"], ["I", "S"])

    def test_checked_native_probe_evidence_is_replayable_and_claim_limited(self):
        evidence = read_json(NATIVE_PROBE_EVIDENCE)
        validate_schema_instance(
            evidence,
            "metadrive_native_probe_evidence",
            context="checked MetaDrive native probe evidence",
        )
        self.assertEqual(evidence["status"], "passed")
        self.assertEqual(evidence["evidence_mode"], "development")
        self.assertFalse(evidence["claim_scope"]["human_gold"])
        self.assertFalse(evidence["claim_scope"]["method_run_evidence"])
        self.assertFalse(
            evidence["claim_scope"]["long_sequence_constructibility_proven"]
        )
        self.assertFalse(evidence["claim_scope"]["thirty_second_ne_proven"])
        self.assertFalse(evidence["claim_scope"]["iec_execution_proven"])
        self.assertEqual(
            evidence["bindings"]["probe_runner"]["sha256"],
            sha256_file(NATIVE_PROBE),
        )
        self.assertEqual(
            {row["case_id"] for row in evidence["token_registry_check"]["positive_cases"]},
            set(AUDITED_TOKEN_CLASSES),
        )
        self.assertEqual(
            {
                row["case_id"]
                for row in evidence["token_registry_check"]["expected_native_failures"]
            },
            {"F@lane2", "f@lane2", "P@lane2"},
        )
        self.assertEqual(evidence["reset_step"]["step_action"], [0.0, 0.0])
        self.assertEqual(evidence["reset_step"]["actual_block_ids"], ["I", "S"])
        self.assertAlmostEqual(evidence["reset_step"]["physical_length_m"], 5.74)
        self.assertAlmostEqual(evidence["reset_step"]["top_down_length_m"], 5.33)

        replay = self.root / "native-probe-evidence.json"
        process = subprocess.run(
            [
                METADRIVE_PYTHON,
                str(NATIVE_PROBE),
                "--registry",
                str(METADRIVE_TOKEN_REGISTRY),
                "--platform-config",
                str(METADRIVE_PLATFORM_CONFIG),
                "--source-repository",
                str(METADRIVE_SOURCE),
                "--development",
                "--output",
                str(replay),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=45,
            check=False,
            env={
                "PATH": os.environ.get("PATH", ""),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "MPLCONFIGDIR": "/tmp",
                "PYTHONHASHSEED": "0",
            },
        )
        self.assertEqual(
            process.returncode,
            0,
            process.stderr.decode("utf-8", errors="replace")[-2000:],
        )
        self.assertEqual(read_json(replay), evidence)

    def test_native_probe_schema_rejects_case_and_dimension_mutations(self):
        evidence = read_json(NATIVE_PROBE_EVIDENCE)
        mutations = []

        missing_case = copy.deepcopy(evidence)
        missing_case["token_registry_check"]["positive_cases"].pop()
        mutations.append(missing_case)

        duplicate_case = copy.deepcopy(evidence)
        duplicate_case["token_registry_check"]["positive_cases"][-1] = copy.deepcopy(
            duplicate_case["token_registry_check"]["positive_cases"][0]
        )
        mutations.append(duplicate_case)

        wrong_dimension = copy.deepcopy(evidence)
        wrong_dimension["reset_step"]["top_down_length_m"] = 5.34
        mutations.append(wrong_dimension)

        extra_field = copy.deepcopy(evidence)
        extra_field["claim_scope"]["unsupported_claim"] = True
        mutations.append(extra_field)

        for mutation in mutations:
            with self.assertRaises(ValidationError):
                validate_schema_instance(
                    mutation,
                    "metadrive_native_probe_evidence",
                    context="mutated MetaDrive native probe evidence",
                )


if __name__ == "__main__":
    unittest.main()
