"""Frozen protocol constants for benchmark version 0.1."""

BENCHMARK_VERSION = "0.1"
SCHEMA_VERSION = "0.1"

SURFACE_STYLES = ("precise", "partial", "vague")
EXPECTED_SUPPORT = ("supported", "unsupported")
ATOM_CATEGORIES = ("actor", "road", "spatial", "event", "temporal", "normative")
REQUIREMENT_LAYERS = ("core_required", "surface_required", "permitted", "forbidden")
DECISION_STATUSES = ("draft", "confirmed", "rejected")

REPETITIONS = 5
SV_SEEDS = (0, 1, 2, 3, 4)
SV_MAX_ITERATIONS = 2000
ROLLOUT_SECONDS = 30.0
CARLA_TIMESTEP_SECONDS = 0.1

EGO_PROXY_LENGTH_M = 5.33
EGO_PROXY_WIDTH_M = 2.10
CARLA_EGO_BLUEPRINT = "vehicle.chevrolet.impala"

VALID_DISPOSITIONS = (
    "generate",
    "reject",
    "clarification",
    "controlled_degradation",
    "failure",
)
VALID_UNSUPPORTED_DISPOSITIONS = (
    "reject",
    "clarification",
    "controlled_degradation",
)
