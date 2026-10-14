import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch
from bus_benchmark import fixture_extractor_common


SOURCE = Path(__file__).resolve().parents[2] / "scripts/public_baseline_extractor.py"
SPEC = importlib.util.spec_from_file_location("public_initial_extractor", SOURCE)
ENTRY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ENTRY)


class PublicExtractorTests(unittest.TestCase):
    def test_native_observer_imports_common_as_package_and_inherits_one_cpu(self):
        payload = {"observation": "synthetic-sentinel"}
        with patch.object(ENTRY.os, "sched_getaffinity", return_value={3, 8}), patch.object(
            ENTRY.os, "sched_setaffinity"
        ) as affinity, patch.object(fixture_extractor_common, "extract", return_value={"projection": "sentinel"}) as extract:
            self.assertEqual(ENTRY.extract_initial_carla(payload), {"projection": "sentinel"})
        affinity.assert_called_once_with(0, {3})
        extract.assert_called_once_with(payload, "carla")


if __name__ == "__main__":
    unittest.main()
