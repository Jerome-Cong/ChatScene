import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
QUERY_LIB = ROOT / "query_lib"
BUILD_SCRIPT = QUERY_LIB / "build_query_library_v0_2.py"
DEV_V01 = QUERY_LIB / "bus_ego_topdown_2d_dev_query_library_v0_1.jsonl"
TEST_V01 = QUERY_LIB / "bus_ego_topdown_2d_query_library_v0_1.jsonl"
DEV_V02 = QUERY_LIB / "bus_ego_topdown_2d_dev_query_library_v0_2.jsonl"
TEST_V02 = QUERY_LIB / "bus_ego_topdown_2d_query_library_v0_2.jsonl"
EXPECTED_V01_HASHES = {
    "dev_v0_1": "09b9e29c32315b866aaa082055ef01ea905c48beeccb05e227f1932855a4d2df",
    "test_v0_1": "560bf74371309d24294685f5828a6c4591f3bed0a88717f0df9da80ae5455d52",
}


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _jsonl(path):
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class QueryLibraryV02BuildTest(unittest.TestCase):
    def test_checked_in_sources_and_report_hashes_match(self):
        actual = {"dev_v0_1": _sha256(DEV_V01), "test_v0_1": _sha256(TEST_V01)}
        self.assertEqual(actual, EXPECTED_V01_HASHES)
        self.assertEqual(
            _json(QUERY_LIB / "query_id_mapping_v0_1_to_v0_2.json")["source_sha256"],
            actual,
        )
        self.assertEqual(
            _json(QUERY_LIB / "query_id_mapping_v0_1_to_v0_2.json")["target_sha256"],
            {
                "dev_v0_2": _sha256(DEV_V02),
                "test_v0_2": _sha256(TEST_V02),
            },
        )
        self.assertEqual(
            _json(QUERY_LIB / "v0_2_validation_report.json")["v0_1_source_hashes"],
            actual,
        )

    def test_repository_python_can_reproduce_every_release_byte(self):
        completed = subprocess.run(
            [sys.executable, str(BUILD_SCRIPT), "--check"],
            cwd=str(ROOT),
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            json.loads(completed.stdout),
            {"files_checked": 9, "reproducible": True},
        )
        self.assertNotIn("/mnt/data", BUILD_SCRIPT.read_text(encoding="utf-8"))

    def test_two_temporary_builds_are_byte_identical(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            for output in (first, second):
                subprocess.run(
                    [sys.executable, str(BUILD_SCRIPT), "--output-dir", output],
                    cwd=str(ROOT),
                    check=True,
                    capture_output=True,
                    text=True,
                )
            names = [
                line.split("  ", 1)[1]
                for line in (Path(first) / "SHA256SUMS.txt")
                .read_text(encoding="utf-8")
                .splitlines()
            ] + ["SHA256SUMS.txt"]
            self.assertEqual(len(names), 9)
            for name in names:
                self.assertEqual((Path(first) / name).read_bytes(), (Path(second) / name).read_bytes())

    def test_tampered_v01_source_is_rejected_before_release(self):
        with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as output:
            source_path = Path(source)
            for path in (DEV_V01, TEST_V01):
                (source_path / path.name).write_bytes(path.read_bytes())
            dev_rows = _jsonl(source_path / DEV_V01.name)
            dev_rows[0]["created_date"] = "2099-01-01"
            (source_path / DEV_V01.name).write_text(
                "\n".join(json.dumps(row, ensure_ascii=False) for row in dev_rows) + "\n",
                encoding="utf-8",
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    str(BUILD_SCRIPT),
                    "--source-dir",
                    str(source_path),
                    "--output-dir",
                    output,
                ],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("immutable_v0_1_source_hash_mismatch", completed.stderr)
            self.assertEqual(list(Path(output).iterdir()), [])

    def test_release_target_symlink_is_rejected_without_following_it(self):
        with tempfile.TemporaryDirectory() as output, tempfile.TemporaryDirectory() as external:
            sentinel = Path(external) / "sentinel.md"
            sentinel.write_text("do not overwrite\n", encoding="utf-8")
            (Path(output) / "README_v0_2.md").symlink_to(sentinel)
            completed = subprocess.run(
                [sys.executable, str(BUILD_SCRIPT), "--output-dir", output],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("unsafe release target", completed.stderr)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "do not overwrite\n")

    def test_split_usage_and_structured_rule_hooks_are_explicit(self):
        for path, rows, usage, tuning in (
            (
                DEV_V02,
                48,
                "design_visible_reference_no_post_freeze_tuning",
                True,
            ),
            (TEST_V02, 252, "held_out_test_only_not_for_tuning", False),
        ):
            records = _jsonl(path)
            self.assertEqual(len(records), rows)
            for record in records:
                self.assertEqual(record["locked_test_usage"], usage)
                self.assertIs(record["tuning_allowed"], tuning)
                self.assertEqual(
                    set(record["rule_hooks"]),
                    {
                        "scene_rules",
                        "counterpart_actor_rules",
                        "counterpart_actor_violations",
                    },
                )
                self.assertTrue(
                    all(
                        isinstance(values, list)
                        and all(isinstance(value, str) for value in values)
                        for values in record["rule_hooks"].values()
                    )
                )

    def test_mapping_policy_and_overlap_cover_the_complete_release(self):
        old_dev, old_test = _jsonl(DEV_V01), _jsonl(TEST_V01)
        new_dev, new_test = _jsonl(DEV_V02), _jsonl(TEST_V02)
        mapping = _json(QUERY_LIB / "query_id_mapping_v0_1_to_v0_2.json")
        self.assertEqual(
            mapping["source_files"],
            {
                "development_v0_1": DEV_V01.name,
                "test_v0_1": TEST_V01.name,
            },
        )
        for split, old, new in (
            ("development", old_dev, new_dev),
            ("test", old_test, new_test),
        ):
            query_map = [item for item in mapping["query_mappings"] if item["dataset_split"] == split]
            intent_map = [item for item in mapping["intent_mappings"] if item["dataset_split"] == split]
            self.assertEqual({item["old_query_id"] for item in query_map}, {item["query_id"] for item in old})
            self.assertEqual({item["new_query_id"] for item in query_map}, {item["query_id"] for item in new})
            self.assertEqual(
                {item["old_intent_group_id"] for item in intent_map},
                {item["intent_group_id"] for item in old},
            )
            self.assertEqual(
                {item["new_intent_group_id"] for item in intent_map},
                {item["intent_group_id"] for item in new},
            )

        policy = _jsonl(QUERY_LIB / "policy_diagnostic_manifest_v0_2.jsonl")
        target_intents = {item["intent_group_id"] for item in new_dev + new_test}
        self.assertEqual(len(policy), 100)
        self.assertEqual({item["intent_group_id"] for item in policy}, target_intents)
        self.assertTrue(all(item["generator_visible"] is False for item in policy))

        overlap = _json(QUERY_LIB / "dev_vs_test_overlap_report_v0_2.json")
        self.assertTrue(overlap["pass"])
        self.assertEqual(overlap["comparison_counts"], {"core_intent_pairs": 1344, "same_style_surface_pairs": 4032})
        self.assertEqual(overlap["exact_core_overlaps"], [])
        self.assertEqual(overlap["near_core_flags"], [])
        self.assertEqual(overlap["near_template_flags"], [])
        self.assertEqual(overlap["source_sha256"]["development_v0_2"], _sha256(DEV_V02))
        self.assertEqual(overlap["source_sha256"]["test_v0_2"], _sha256(TEST_V02))

    def test_sha_manifest_is_exact_and_valid(self):
        expected = {
            "bus_ego_topdown_2d_dev_query_library_v0_2.jsonl",
            "bus_ego_topdown_2d_query_library_v0_2.jsonl",
            "policy_diagnostic_manifest_v0_2.jsonl",
            "dev_vs_test_overlap_report_v0_2.json",
            "query_id_mapping_v0_1_to_v0_2.json",
            "v0_2_validation_report.json",
            "README_v0_2.md",
            "build_query_library_v0_2.py",
        }
        entries = {}
        for line in (QUERY_LIB / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines():
            digest, name = line.split("  ", 1)
            entries[name] = digest
        self.assertEqual(set(entries), expected)
        for name, digest in entries.items():
            self.assertEqual(_sha256(QUERY_LIB / name), digest)


if __name__ == "__main__":
    unittest.main()
