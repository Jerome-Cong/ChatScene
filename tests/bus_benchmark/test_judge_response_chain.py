import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bus_benchmark import freeze
from bus_benchmark.errors import FreezeError, ValidationError
from bus_benchmark.jsonio import (
    sha256_file,
    write_jsonl,
)
from bus_benchmark.judge_pipeline import (
    create_judge_request_bundle,
    run_judge_request_bundle,
    validate_judge_run_bundle,
)
from bus_benchmark.provenance import (
    record_sha256,
    validate_frozen_judge_response_chain,
)
class JudgeResponseChainAttackTests(unittest.TestCase):
    def _bundle(self, directory, atom_count=2):
        from tests.bus_benchmark.test_judge_pipeline import JudgePipelineTests

        root = Path(directory)
        fixture = JudgePipelineTests()._bundle(
            root, mode="request_bound", atom_count=atom_count
        )
        request_directory = root / "judge-requests"
        run_directory = root / "judge-run"
        create_judge_request_bundle(
            fixture["responses"],
            fixture["evidence"],
            fixture["manifest"],
            request_directory,
        )
        run_judge_request_bundle(
            request_directory,
            fixture["responses"],
            fixture["evidence"],
            fixture["manifest"],
            run_directory,
        )
        _, raw_records = validate_judge_run_bundle(
            run_directory,
            fixture["responses"],
            fixture["evidence"],
            fixture["manifest"],
        )
        return {
            **fixture,
            "request_directory": request_directory,
            "run_directory": run_directory,
            "raw_records": raw_records,
        }

    def _validate(self, bundle, raw_records=None, evidence_records=None):
        if raw_records is not None:
            write_jsonl(
                bundle["run_directory"] / "judge_responses.jsonl", raw_records
            )
        _, validated_raw = validate_judge_run_bundle(
            bundle["run_directory"],
            bundle["responses"],
            bundle["evidence"] if evidence_records is None else evidence_records,
            bundle["manifest"],
            request_directory=bundle["request_directory"],
        )
        return validate_frozen_judge_response_chain(
            validated_raw,
            bundle["responses"],
            bundle["evidence"] if evidence_records is None else evidence_records,
            bundle["manifest"],
        )

    def test_valid_records_return_only_score_safe_bound_verdicts(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._bundle(directory)
            bindings = self._validate(bundle)
            raw_records = bundle["raw_records"]
            atoms = bundle["atoms"]
            self.assertEqual(set(bindings), {"run-1"})
            self.assertEqual(
                set(bindings["run-1"]), {atom["atom_id"] for atom in atoms}
            )
            raw_by_atom = {record["atom_id"]: record for record in raw_records}
            for atom in atoms:
                raw_record = raw_by_atom[atom["atom_id"]]
                binding = bindings["run-1"][atom["atom_id"]]
                self.assertEqual(
                    binding["judge_response_record_sha256"],
                    record_sha256(raw_record),
                )

    def test_missing_record_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._bundle(directory)
            with self.assertRaises(ValidationError):
                self._validate(bundle, raw_records=bundle["raw_records"][:-1])

    def test_orphan_record_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._bundle(directory)
            orphan = copy.deepcopy(bundle["raw_records"][0])
            orphan["atom_id"] = "f" * 16
            orphan["judge_response_id"] = "e" * 64
            with self.assertRaises(ValidationError):
                self._validate(bundle, raw_records=bundle["raw_records"] + [orphan])

    def test_duplicate_pair_or_response_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._bundle(directory)
            duplicate = copy.deepcopy(bundle["raw_records"][0])
            with self.assertRaises(ValidationError):
                self._validate(bundle, raw_records=bundle["raw_records"] + [duplicate])

    def test_swapped_atom_identity_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._bundle(directory)
            swapped = copy.deepcopy(bundle["raw_records"])
            for field in ("atom_id", "item_id"):
                swapped[0][field], swapped[1][field] = (
                    swapped[1][field],
                    swapped[0][field],
                )
            with self.assertRaises(ValidationError):
                self._validate(bundle, raw_records=swapped)

    def test_raw_response_tampering_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._bundle(directory)
            tampered = copy.deepcopy(bundle["raw_records"])
            tampered[0]["raw_response"] = tampered[1]["raw_response"]
            with self.assertRaises(ValidationError):
                self._validate(bundle, raw_records=tampered)

    def test_raw_payload_and_self_reported_hash_cannot_be_swapped(self):
        """The process-bound run bundle rejects moving payload and hash together."""

        with tempfile.TemporaryDirectory() as directory:
            bundle = self._bundle(directory)
            swapped = copy.deepcopy(bundle["raw_records"])
            for field in ("raw_response", "raw_response_sha256"):
                swapped[0][field], swapped[1][field] = (
                    swapped[1][field],
                    swapped[0][field],
                )
            with self.assertRaises(ValidationError):
                self._validate(bundle, raw_records=swapped)

    def test_judge_cannot_target_a_deterministically_decided_atom(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._bundle(directory, atom_count=1)
            decided_evidence = copy.deepcopy(bundle["evidence"])
            decided_evidence[0]["complete_categories"] = ["event"]
            with self.assertRaises(ValidationError):
                self._validate(bundle, evidence_records=decided_evidence)


class NestedBindingToctouTests(unittest.TestCase):
    def test_nested_file_changed_after_first_validation_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            nested = Path(directory) / "nested-bound-file.txt"
            nested.write_text("AAAA", encoding="utf-8")
            binding = {
                "path": str(nested),
                "sha256": sha256_file(nested),
            }

            def validate_then_replace(_records):
                freeze._verify_file_binding(binding, "nested attack fixture")
                nested.write_text("BBBB", encoding="utf-8")

            with mock.patch.object(
                freeze,
                "_validate_asset_contract_once",
                side_effect=validate_then_replace,
            ):
                with self.assertRaisesRegex(FreezeError, "changed during validation"):
                    freeze._validate_asset_contract([])
            self.assertIsNone(freeze._ACTIVE_NESTED_BINDING_SNAPSHOTS)


if __name__ == "__main__":
    unittest.main()
