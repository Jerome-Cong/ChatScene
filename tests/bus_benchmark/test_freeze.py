import tempfile
import unittest
import copy
from unittest import mock
from pathlib import Path

from bus_benchmark.errors import FreezeError
from bus_benchmark.freeze import (
    REQUIRED_FREEZE_ROLES,
    REQUIRED_PROTOCOL,
    create_freeze_manifest,
    verify_freeze_manifest,
    _execute_extractor_once,
    _require_exact_metadrive_registry_binding,
    build_extractor_input,
    execute_frozen_extractor,
    load_frozen_extractor_runtime,
)
from bus_benchmark.atoms import make_atom
from bus_benchmark.metrics import deterministic_atom_verdict
from bus_benchmark.jsonio import write_json
from bus_benchmark.jsonio import canonical_json_bytes, sha256_bytes, sha256_file


class FreezeTests(unittest.TestCase):
    def full_assets(self, asset):
        return [{"role": role, "path": str(asset)} for role in sorted(REQUIRED_FREEZE_ROLES)]

    def _extractor_runtime_manifest(self, root):
        source = root / "extractor.py"
        source.write_text("def extract(payload):\n    return payload\n", encoding="utf-8")
        extractor = root / "extractor.json"
        write_json(
            extractor,
            {
                "decision_status": "confirmed",
                "extractor_id": "fixture-extractor",
                "extractor_version": "0.1",
                "input_contract": "artifact_observation_v0.1",
                "output_contract": "semantic_projection_v0.1",
                "entrypoints": [
                    {
                        "platform": platform,
                        "path": str(source),
                        "sha256": sha256_file(source),
                        "callable": "extract",
                    }
                    for platform in ("carla", "metadrive")
                ],
            },
        )
        registry = root / "registry.json"
        write_json(registry, {"registry_id": "trusted"})
        manifest = {
            "assets": [
                {
                    "role": "semantic_extractor_manifest",
                    "path": str(extractor),
                    "sha256": sha256_file(extractor),
                    "bytes": extractor.stat().st_size,
                },
                {
                    "role": "metadrive_pg_token_registry",
                    "path": str(registry),
                    "sha256": sha256_file(registry),
                    "bytes": registry.stat().st_size,
                },
            ]
        }
        return manifest, registry

    def test_draft_asset_blocks_freeze(self):
        with tempfile.TemporaryDirectory() as directory:
            asset = Path(directory) / "asset.json"
            write_json(asset, {"decision_status": "draft"})
            with self.assertRaises(FreezeError):
                create_freeze_manifest([{"role": "oracle", "path": str(asset)}], {})

    def test_pending_review_blocks_freeze(self):
        with tempfile.TemporaryDirectory() as directory:
            asset = Path(directory) / "asset.jsonl"
            asset.write_text('{"annotator_a_status" : "pending"}\n', encoding="utf-8")
            with self.assertRaises(FreezeError):
                create_freeze_manifest(self.full_assets(asset), dict(REQUIRED_PROTOCOL))

    def test_protocol_draft_blocks_freeze(self):
        with tempfile.TemporaryDirectory() as directory:
            asset = Path(directory) / "asset.json"
            write_json(asset, {"decision_status": "confirmed"})
            protocol = dict(REQUIRED_PROTOCOL)
            protocol["decision_status"] = "draft"
            with self.assertRaises(FreezeError):
                create_freeze_manifest(self.full_assets(asset), protocol)

    def test_missing_required_roles_blocks_freeze(self):
        with tempfile.TemporaryDirectory() as directory:
            asset = Path(directory) / "asset.json"
            write_json(asset, {"decision_status": "confirmed"})
            with self.assertRaises(FreezeError):
                create_freeze_manifest([{"role": "query_library_test", "path": str(asset)}], dict(REQUIRED_PROTOCOL))

    def test_frozen_manifest_detects_asset_change(self):
        with tempfile.TemporaryDirectory() as directory:
            asset = Path(directory) / "asset.json"
            write_json(asset, {"decision_status": "confirmed"})
            manifest = create_freeze_manifest(
                [{"role": "development_fixture", "path": str(asset)}],
                {},
                allow_draft=True,
            )
            self.assertTrue(
                verify_freeze_manifest(manifest, require_frozen=False)["verified"]
            )
            write_json(asset, {"decision_status": "confirmed", "changed": True})
            with self.assertRaises(FreezeError):
                verify_freeze_manifest(manifest, require_frozen=False)

    def test_forged_empty_frozen_manifest_is_rejected(self):
        manifest = create_freeze_manifest([], dict(REQUIRED_PROTOCOL), allow_draft=True)
        manifest["status"] = "frozen"
        manifest["unfinished_decision_count"] = 0
        manifest["unfinished_decision_paths"] = []
        manifest.pop("manifest_sha256")
        manifest["manifest_sha256"] = sha256_bytes(canonical_json_bytes(manifest))
        with self.assertRaises(FreezeError):
            verify_freeze_manifest(manifest)

    def test_one_file_cannot_impersonate_all_required_roles(self):
        with tempfile.TemporaryDirectory() as directory:
            asset = Path(directory) / "asset.json"
            write_json(asset, {"decision_status": "confirmed"})
            with self.assertRaises(FreezeError):
                create_freeze_manifest(self.full_assets(asset), dict(REQUIRED_PROTOCOL))

    def test_extractor_timeout_is_bounded_in_an_isolated_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "hanging_extractor.py"
            source.write_text(
                "def extract(payload):\n    while True:\n        pass\n",
                encoding="utf-8",
            )
            with mock.patch("bus_benchmark.freeze.EXTRACTOR_TIMEOUT_SECONDS", 0.2):
                with self.assertRaises(FreezeError):
                    _execute_extractor_once(
                        {"path": source, "callable": "extract"}, {}, "hanging"
                    )

    def test_no_artifact_terminal_states_are_query_blind_and_need_no_judge(self):
        cases = (
            {"terminal_status": "failed", "disposition": "generate"},
            {"terminal_status": "timeout", "disposition": "generate"},
            {"terminal_status": "complete", "disposition": "reject"},
            {"terminal_status": "complete", "disposition": "clarification"},
        )
        required = make_atom(
            "event", "event_spec", {"event": "bus_departure_merge"}
        )
        runtime = {"carla": {"path": Path("unused"), "callable": "unused"}}
        with mock.patch("bus_benchmark.freeze._execute_extractor_once") as execute:
            for case in cases:
                projection = execute_frozen_extractor(
                    runtime,
                    {
                        "platform": "carla",
                        "artifact": None,
                        **case,
                    },
                )
                self.assertEqual(projection["atoms"], [])
                self.assertEqual(projection["common_atoms"], [])
                self.assertEqual(projection["ego"]["count"], 0)
                self.assertEqual(
                    deterministic_atom_verdict(required, projection), "violated"
                )
        execute.assert_not_called()

    def test_failed_or_timed_out_run_with_artifact_still_gets_semantic_extraction(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "scene.scenic"
            artifact.write_text("ego = Car", encoding="utf-8")
            projection = {
                "ego": {
                    "count": 1,
                    "semantic_role": "bus_proxy",
                    "approved_proxy": True,
                    "deterministic": True,
                    "length_m": 5.33,
                    "width_m": 2.1,
                    "blueprint": "vehicle.chevrolet.impala",
                    "vehicle_model": "xl",
                },
                "atoms": [],
                "common_atoms": [],
                "complete_categories": [],
            }
            runtime = {
                "carla": {
                    "entrypoint": {"path": Path("unused"), "callable": "unused"},
                    "artifact_format": "scenic",
                    "trusted_dependencies": [],
                }
            }
            for terminal_status in ("failed", "timeout"):
                with mock.patch(
                    "bus_benchmark.freeze._execute_extractor_once",
                    return_value=projection,
                ) as execute:
                    actual = execute_frozen_extractor(
                        runtime,
                        {
                            "platform": "carla",
                            "terminal_status": terminal_status,
                            "disposition": "generate",
                            "artifact": {
                                "path": str(artifact),
                                "sha256": sha256_file(artifact),
                                "bytes": artifact.stat().st_size,
                            },
                        },
                    )
                self.assertEqual(actual, projection)
                execute.assert_called_once()

    def test_frozen_runtime_injects_only_benchmark_owned_registry_context(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, registry = self._extractor_runtime_manifest(root)
            _, runtime = load_frozen_extractor_runtime(manifest)
            artifact = root / "scene.json"
            artifact.write_text("{}", encoding="utf-8")
            projection = {
                "ego": {
                    "count": 0,
                    "semantic_role": "missing",
                    "approved_proxy": False,
                    "deterministic": True,
                    "length_m": 0.0,
                    "width_m": 0.0,
                    "blueprint": "",
                    "vehicle_model": "",
                },
                "atoms": [],
                "common_atoms": [],
                "complete_categories": [],
            }
            response = {
                "platform": "metadrive",
                "artifact_format": "scenic",
                "terminal_status": "complete",
                "disposition": "generate",
                "artifact": {
                    "path": str(artifact),
                    "sha256": sha256_file(artifact),
                    "bytes": artifact.stat().st_size,
                    "dependencies": [
                        {
                            "kind": "metadrive_pg_token_registry",
                            "path": "/attacker/registry.json",
                            "sha256": "0" * 64,
                            "bytes": 1,
                        },
                        {"kind": "extra", "path": "/attacker/extra"},
                    ],
                },
            }
            with mock.patch(
                "bus_benchmark.freeze._execute_extractor_once",
                return_value=projection,
            ) as execute:
                self.assertEqual(
                    execute_frozen_extractor(runtime, response), projection
                )
            payload = execute.call_args.args[1]
            self.assertEqual(
                payload["artifact_format"], "metadrive_pg_block_scene_v0.1"
            )
            self.assertEqual(
                payload["artifact"]["dependencies"],
                [
                    {
                        "kind": "metadrive_pg_token_registry",
                        "path": str(registry.resolve()),
                        "sha256": sha256_file(registry),
                        "bytes": registry.stat().st_size,
                    }
                ],
            )
            self.assertNotIn("/attacker/registry.json", str(payload))

            carla_response = copy.deepcopy(response)
            carla_response["platform"] = "carla"
            with mock.patch(
                "bus_benchmark.freeze._execute_extractor_once",
                return_value=projection,
            ) as execute:
                execute_frozen_extractor(runtime, carla_response)
            carla_payload = execute.call_args.args[1]
            self.assertEqual(carla_payload["artifact_format"], "scenic")
            self.assertEqual(carla_payload["artifact"]["dependencies"], [])

    def test_frozen_runtime_rejects_registry_binding_and_cardinality_attacks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, registry = self._extractor_runtime_manifest(root)
            registry_asset = manifest["assets"][1]
            attacks = []
            wrong_path = copy.deepcopy(manifest)
            wrong_path["assets"][1]["path"] = str(root / "missing.json")
            attacks.append(wrong_path)
            wrong_hash = copy.deepcopy(manifest)
            wrong_hash["assets"][1]["sha256"] = "0" * 64
            attacks.append(wrong_hash)
            wrong_bytes = copy.deepcopy(manifest)
            wrong_bytes["assets"][1]["bytes"] = registry.stat().st_size + 1
            attacks.append(wrong_bytes)
            duplicate = copy.deepcopy(manifest)
            duplicate["assets"].append(copy.deepcopy(registry_asset))
            attacks.append(duplicate)
            for attacked in attacks:
                with self.subTest(attacked=attacked["assets"][-1]):
                    with self.assertRaises(FreezeError):
                        load_frozen_extractor_runtime(attacked)

    def test_execute_rechecks_artifact_and_registry_bytes_after_runtime_load(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, registry = self._extractor_runtime_manifest(root)
            _, runtime = load_frozen_extractor_runtime(manifest)
            artifact = root / "scene.json"
            artifact.write_text("{}", encoding="utf-8")
            response = {
                "platform": "metadrive",
                "terminal_status": "complete",
                "disposition": "generate",
                "artifact": {
                    "path": str(artifact),
                    "sha256": sha256_file(artifact),
                    "bytes": artifact.stat().st_size,
                },
            }
            artifact.write_text('{"tampered":true}', encoding="utf-8")
            with self.assertRaisesRegex(FreezeError, "artifact byte binding"):
                execute_frozen_extractor(runtime, response)

            artifact.write_text("{}", encoding="utf-8")
            response["artifact"] = {
                "path": str(artifact),
                "sha256": sha256_file(artifact),
                "bytes": artifact.stat().st_size,
            }
            registry.write_text('{"registry_id":"changed"}', encoding="utf-8")
            with self.assertRaisesRegex(FreezeError, "registry.*hash mismatch"):
                execute_frozen_extractor(runtime, response)

    def test_nested_registry_bindings_must_equal_the_frozen_asset(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, registry = self._extractor_runtime_manifest(root)
            frozen = manifest["assets"][1]
            valid = {
                key: frozen[key] for key in ("path", "sha256", "bytes")
            }
            self.assertEqual(
                _require_exact_metadrive_registry_binding(
                    valid, frozen, "nested registry"
                )["path"],
                str(registry.resolve()),
            )
            copied = root / "copied-registry.json"
            copied.write_bytes(registry.read_bytes())
            replaced_path = {
                "path": str(copied),
                "sha256": sha256_file(copied),
                "bytes": copied.stat().st_size,
            }
            with self.assertRaisesRegex(FreezeError, "differs from the frozen"):
                _require_exact_metadrive_registry_binding(
                    replaced_path, frozen, "nested registry"
                )
            for field, value in (
                ("sha256", "0" * 64),
                ("bytes", registry.stat().st_size + 1),
            ):
                attacked = copy.deepcopy(valid)
                attacked[field] = value
                with self.subTest(field=field):
                    with self.assertRaises(FreezeError):
                        _require_exact_metadrive_registry_binding(
                            attacked, frozen, "nested registry"
                        )

    def test_extractor_context_parameters_are_keyword_only(self):
        with self.assertRaises(TypeError):
            build_extractor_input({}, "scenic", [])


if __name__ == "__main__":
    unittest.main()
