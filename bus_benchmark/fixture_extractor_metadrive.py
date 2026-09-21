"""MetaDrive entrypoint for operational freeze fixtures."""

import importlib.util
from pathlib import Path


def _common():
    path = Path(__file__).with_name("fixture_extractor_common.py")
    spec = importlib.util.spec_from_file_location("bus_fixture_common", str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def extract_fixture(payload):
    return _common().extract(payload, "metadrive")
