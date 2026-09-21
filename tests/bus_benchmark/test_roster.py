import argparse
import tempfile
import unittest
from pathlib import Path

from bus_benchmark.cli import _authorize_frozen_assets
from bus_benchmark.errors import BenchmarkError, ValidationError
from bus_benchmark.jsonio import write_jsonl
from bus_benchmark.roster import build_roster, validate_records_against_roster


class RosterTests(unittest.TestCase):
    def setUp(self):
        self.records = [
            {
                "query_id": "q-precise",
                "intent_group_id": "intent-a",
                "surface_style": "precise",
                "expected_support": "supported",
            },
            {
                "query_id": "q-partial",
                "intent_group_id": "intent-b",
                "surface_style": "partial",
                "expected_support": "unsupported",
            },
        ]
        self.clusters = {"decision_status": "confirmed", "clusters": []}

    def _roster(self, directory):
        library = Path(directory) / "library.jsonl"
        write_jsonl(library, self.records)
        return build_roster(
            self.records,
            library,
            "method-a",
            "carla",
            self.clusters,
        )

    @staticmethod
    def _responses(roster):
        responses = []
        for entry in roster["queries"]:
            for repetition in range(5):
                responses.append(
                    {
                        **entry,
                        "run_id": "{}-{}".format(entry["query_id"], repetition),
                        "method_id": roster["method_id"],
                        "platform": roster["platform"],
                        "repetition": repetition,
                    }
                )
        return responses

    def test_exact_five_run_roster_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            roster = self._roster(directory)
            result = validate_records_against_roster(self._responses(roster), roster)
            self.assertEqual(result["query_count"], 2)
            self.assertEqual(result["record_count"], 10)

    def test_missing_repetition_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            roster = self._roster(directory)
            with self.assertRaises(ValidationError):
                validate_records_against_roster(self._responses(roster)[:-1], roster)

    def test_roster_metadata_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            roster = self._roster(directory)
            responses = self._responses(roster)
            responses[0]["surface_style"] = "vague"
            with self.assertRaises(ValidationError):
                validate_records_against_roster(responses, roster)

    def test_boolean_repetition_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            roster = self._roster(directory)
            responses = self._responses(roster)
            responses[1]["repetition"] = True
            with self.assertRaises(ValidationError):
                validate_records_against_roster(responses, roster)

    def test_missing_run_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            roster = self._roster(directory)
            responses = self._responses(roster)
            responses[0].pop("run_id")
            with self.assertRaises(ValidationError):
                validate_records_against_roster(responses, roster)

    def test_formal_scoring_requires_freeze_manifest(self):
        args = argparse.Namespace(development=False, freeze_manifest=None)
        with self.assertRaises(BenchmarkError):
            _authorize_frozen_assets(args, (("query_roster", "roster.json"),))

    def test_development_scoring_does_not_require_freeze_manifest(self):
        args = argparse.Namespace(development=True, freeze_manifest=None)
        _authorize_frozen_assets(args, (("query_roster", "roster.json"),))


if __name__ == "__main__":
    unittest.main()
