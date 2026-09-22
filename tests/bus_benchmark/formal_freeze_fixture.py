"""Build a complete synthetic formal-freeze bundle over the real locked libraries."""

from bus_benchmark.paths import PACKAGE_ROOT
import copy
import json
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path

from bus_benchmark.cpd import compute_coverage
from bus_benchmark.calibration import (
    build_judge_calibration_roster,
    compute_judge_calibration,
)
from bus_benchmark.constants import SCHEMA_VERSION
from bus_benchmark.freeze import (
    REQUIRED_PROTOCOL,
    _expected_protocol_decision_subjects,
    tracked_source_tree_sha256,
)
from bus_benchmark.jsonio import (
    canonical_json_bytes,
    read_json,
    read_jsonl,
    sha256_bytes,
    sha256_file,
    write_json,
    write_jsonl,
)
from bus_benchmark.oracle import REQUIRED_REVIEW_CHECKS, draft_oracle
from bus_benchmark.schema import supported_schema_paths
from bus_benchmark.ontology import (
    build_common_ontology,
    deterministic_representative_arguments,
)
from bus_benchmark.judge import (
    judge_config_sha256,
    judge_item_id,
    parse_raw_judge_response,
)
from bus_benchmark.judge_pipeline import (
    build_judge_calibration_context_manifest,
    create_judge_calibration_request_bundle,
    run_judge_calibration_request_bundle,
    validate_judge_calibration_run_bundle,
)
from bus_benchmark.metrics import deterministic_atom_verdict
from bus_benchmark.fixture_extractor_common import _detect_common
from bus_benchmark.generation import (
    probe_python_environment,
    python_environment_control_paths,
)
from bus_benchmark.runtime import (
    metadrive_tracked_source_binding,
    runtime_tree_binding,
)
from bus_benchmark.metadrive_pg_contract import (
    PG_BLOCK_SCENE_CONTRACT,
    build_metadrive_capability_audit_subject,
    build_metadrive_fixture_audit_subject,
    build_metadrive_registry_audit_subject,
)
from bus_benchmark.roster import build_roster


ROOT = Path(__file__).resolve().parents[2]
CARLA_PYTHON = ROOT / "chatscene" / "bin" / "python-frozen"
METADRIVE_PYTHON = Path(
    read_json(ROOT / "benchmark_configs/platforms/metadrive_runtime_draft.json")[
        "worker"
    ]["interpreter"]
)
TEST_LIBRARY = ROOT / "query_lib" / "bus_ego_topdown_2d_query_library_v0_2.jsonl"
DEV_LIBRARY = ROOT / "query_lib" / "bus_ego_topdown_2d_dev_query_library_v0_2.jsonl"
TEST_ORACLE_DRAFT = ROOT / "benchmark_artifacts" / "drafts" / "test_oracle_draft.jsonl"
DEV_ORACLE_DRAFT = ROOT / "benchmark_artifacts" / "drafts" / "dev_oracle_draft.jsonl"
CARLA_MAP = (
    ROOT
    / "Scenic"
    / "tests"
    / "formats"
    / "opendrive"
    / "maps"
    / "CARLA"
    / "Town01.xodr"
)
METADRIVE_STEP_SECONDS = 0.1


def _concept_deterministic_arguments(concept):
    if concept["kind"] != "requirement_atom_domain":
        return []
    definition = concept["definition"]
    return deterministic_representative_arguments(
        definition["category"],
        definition["predicate"],
        definition["polarity"],
        definition["argument_domain"],
    )


def _concept_requires_judge(concept):
    return concept["kind"] == "requirement_atom_domain" and not _concept_deterministic_arguments(
        concept
    )


def _runtime_environment(platform, root, observer, runtime_fixture_input=None):
    if platform == "carla":
        interpreter = CARLA_PYTHON.resolve()
        repository = ROOT
        tracked_subpath = "Scenic"
        validation_level = "native_compile_and_five_seed_static_sv"
    else:
        interpreter = METADRIVE_PYTHON.resolve()
        repository = Path("/home/shijie20/CodeSpace/mdsn/metadrive").resolve()
        tracked_subpath = "metadrive"
        validation_level = "native_pgmap_reset_step"
    probe = subprocess.run(
        [
            str(interpreter),
            "-c",
            (
                "import importlib.metadata as m,json,sys;"
                "norm=lambda value:value.lower().replace('_','-');"
                "pairs=sorted((norm(d.metadata['Name']),d.version) for d in m.distributions() if d.metadata.get('Name'));"
                "print(json.dumps({'python_version':sys.version.split()[0],"
                "'distributions':dict(pairs)},sort_keys=True))"
            ),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=20,
        check=True,
    )
    runtime = json.loads(probe.stdout.decode("utf-8"))
    revision = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=True,
    ).stdout.decode("ascii").strip()
    runtime_probe = None
    if platform == "metadrive":
        if runtime_fixture_input is None:
            raise AssertionError("MetaDrive runtime probe requires an audited fixture")
        probe_input = root / "metadrive_runtime_probe_input.json"
        probe_result = root / "metadrive_runtime_probe_result.json"
        write_json(probe_input, runtime_fixture_input)
        token_registry_path = runtime_fixture_input["artifact"]["dependencies"][0][
            "path"
        ]
        native_probe = subprocess.run(
            [
                str(interpreter),
                str(observer),
                "metadrive",
                "--runtime-probe",
                "--token-registry",
                token_registry_path,
            ],
            input=runtime_fixture_input["artifact"]["text"].encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=True,
            env={
                "PATH": os.environ.get("PATH", ""),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "MPLCONFIGDIR": "/tmp",
                "PYTHONHASHSEED": "0",
            },
        )
        write_json(probe_result, json.loads(native_probe.stdout.decode("utf-8")))
        runtime_probe = {
            "observer": {
                "path": str(observer),
                "sha256": sha256_file(observer),
            },
            "input": {
                "path": str(probe_input),
                "sha256": sha256_file(probe_input),
            },
            "result": {
                "path": str(probe_result),
                "sha256": sha256_file(probe_result),
            },
        }
    return {
        "platform": platform,
        "validation_level": validation_level,
        "interpreter": {
            "path": str(interpreter),
            "sha256": sha256_file(interpreter),
        },
        "python_version": runtime["python_version"],
        "distributions": runtime["distributions"],
        "source": {
            "repository_path": str(repository),
            "revision": revision,
            "tracked_subpath": tracked_subpath,
            "tracked_tree_sha256": tracked_source_tree_sha256(
                repository, tracked_subpath
            ),
        },
        "runtime_probe": runtime_probe,
    }


def _confirmed_oracle(library):
    records = draft_oracle(read_jsonl(library))
    for record in records:
        record["decision_status"] = "confirmed"
        for atom in record["atoms"]:
            atom["decision_status"] = "confirmed"
        policy = record["cpd_policy"]
        policy["decision_status"] = "confirmed"
        policy["cross_platform_judgeable"] = bool(policy["eligible"])
    return records


def _fixture_ego(variant):
    values = {
        "approved": (1, "bus_proxy", True, 5.33, 2.1, "vehicle.chevrolet.impala", "xl"),
        "missing": (0, "missing", False, 0.0, 0.0, "", ""),
        "wrong_proxy": (1, "other", False, 5.33, 2.1, "vehicle.wrong", "s"),
        "wrong_footprint": (1, "bus_proxy", True, 4.5, 2.0, "vehicle.chevrolet.impala", "xl"),
    }
    count, role, approved, length, width, blueprint, model = values[variant]
    return {
        "count": count,
        "semantic_role": role,
        "approved_proxy": approved,
        "deterministic": True,
        "length_m": length,
        "width_m": width,
        "blueprint": blueprint,
        "vehicle_model": model,
    }


def _projection_from_fixture_fact(fact):
    atoms = []
    common_atoms = []
    if fact["kind"] == "atom":
        if fact["present"] and fact.get("deterministic") is True:
            atoms.append(
                {
                    "category": fact["category"],
                    "predicate": fact["predicate"],
                    "arguments": fact["arguments"],
                    "polarity": "present",
                    "evidence_source": "deterministic",
                }
            )
    elif fact["kind"] == "cpd":
        if (
            fact["dimension"] == "optional_road_feature_presence"
            and fact.get("present", True)
        ):
            common_atoms.append(
                {"dimension": fact["dimension"], "value": fact["value"]}
            )
    # Synthetic formal-bundle fixtures can carry incidental CPD observations
    # (for example, an actor-role fixture still has an actor distance).  The
    # focused metamorphic tests above own the selector semantics; this bundle
    # helper records the full live query-blind projection so freeze-chain tests
    # do not incorrectly require unrelated common dimensions to disappear.
    template = _physical_template(fact)
    normalized_tracks = []
    for index, track in enumerate(template["tracks"]):
        normalized_tracks.append(
            {
                "id": "fixture-{}".format(index),
                "kind": track["kind"],
                "is_ego": track.get("is_ego", False),
                "positions": copy.deepcopy(track["positions"]),
                "heading_rad": 0.0,
                "length_m": track.get("length_m", 0.5),
                "width_m": track.get("width_m", 0.5),
                "blueprint": track.get("blueprint", ""),
                "vehicle_model": track.get("vehicle_model", ""),
            }
        )
    detected_common = _detect_common("carla", normalized_tracks, [])
    common_atoms = sorted(
        common_atoms + detected_common,
        key=canonical_json_bytes,
    )
    return {
        "ego": _fixture_ego(fact.get("ego_variant", "approved")),
        "atoms": atoms,
        "common_atoms": common_atoms,
        "complete_categories": [],
    }


def _physical_template(fact):
    ego = _fixture_ego(fact.get("ego_variant", "approved"))
    tracks = []
    if ego["count"] == 1:
        tracks.append(
            {
                "kind": "Car",
                "is_ego": True,
                "positions": [[0.0, 0.0], [2.0, 0.0], [4.0, 0.0], [6.0, 0.0]],
                "length_m": ego["length_m"],
                "width_m": ego["width_m"],
                "blueprint": ego["blueprint"],
                "vehicle_model": ego["vehicle_model"],
            }
        )
    else:
        tracks.append(
            {
                "kind": "Cone",
                "is_ego": True,
                "positions": [[0.0, 0.0]],
                "length_m": 0.5,
                "width_m": 0.5,
                "blueprint": "",
                "vehicle_model": "",
            }
        )
    template = {"tracks": tracks, "feature_carriers": []}
    present = fact.get("present") is True

    if fact["kind"] == "atom":
        predicate = fact["predicate"]
        ego_track = tracks[0]
        if predicate == "actor_role_count":
            tracks.append(
                {
                    "kind": "Car",
                    "positions": [[5.0, 3.5]],
                    "length_m": 4.8 if present else 4.6,
                    "width_m": 1.9,
                }
            )
        elif predicate == "event_spec":
            ego_track["positions"] = (
                [[0.0, 0.0], [1.0, 0.0], [3.0, 0.0], [6.0, 0.0]]
                if present
                else [[0.0, 0.0], [2.0, 0.0], [4.0, 0.0], [6.0, 0.0]]
            )
        elif predicate == "risk_level":
            tracks.append(
                {
                    "kind": "Car",
                    "positions": (
                        [[9.0, 0.0], [7.0, 0.0], [5.0, 0.0], [6.5, 0.0]]
                        if present
                        else [[10.0, 0.0], [8.0, 0.0], [6.0, 0.0], [8.0, 0.0]]
                    ),
                }
            )
        elif predicate == "bus_stop_type":
            tracks.append({"kind": "BusStop", "positions": [[5.0, 5.0]]})
            template["feature_carriers"].append("bus_bay")
        elif predicate == "lane_configuration":
            tracks.append(
                {
                    "kind": "Car",
                    "positions": (
                        [[4.0, 6.0], [4.0, 4.0], [4.0, 2.0], [4.0, 2.0]]
                        if present
                        else [[4.0, 6.0]] * 4
                    ),
                }
            )
        elif predicate == "road_topology":
            tracks.extend(
                {
                    "kind": "Cone",
                    "positions": [[x, 3.5 if present or x != 5.0 else 6.0]],
                }
                for x in (0.0, 5.0, 10.0)
            )
            if present:
                template["feature_carriers"].append("barrier")
        elif predicate == "actor_relative_region":
            tracks.extend(
                [
                    {"kind": "Car", "positions": [[5.0, 3.5]]},
                    {
                        "kind": "Car",
                        "positions": [[10.0, 3.5 if present else 0.0]],
                    },
                ]
            )
        elif predicate == "surface_numeric_constraint":
            tracks.append({"kind": "Car", "positions": [[12.0, 0.0]]})
        elif predicate == "before":
            ego_track["positions"] = (
                [[0.0, 0.0], [2.0, 0.0], [4.0, 0.0], [6.0, 0.0]]
                if present
                else [[0.0, 0.0]] * 4
            )
            tracks.append(
                {
                    "kind": "Pedestrian",
                    "positions": (
                        [[8.0, 4.0], [8.0, 4.0], [8.0, 2.0], [8.0, 0.0]]
                        if present
                        else [[8.0, 4.0]] * 4
                    ),
                }
            )
        elif predicate == "parallel_group":
            ego_track["positions"] = [[0.0, 0.0], [2.0, 0.0], [4.0, 0.5], [6.0, 2.0], [8.0, 3.5]]
            tracks.extend(
                [
                    {"kind": "Motorcycle", "positions": [[19.0, 3.5], [16.0, 3.5], [13.0, 3.5], [11.0, 3.5], [10.0, 3.5]]},
                    {"kind": "Car", "positions": [[30.0, 3.5], [27.0, 3.5], [25.0, 3.5], [24.0, 3.5], [24.0, 3.5]]},
                ]
            )
        elif predicate == "rule_hook":
            tracks.extend(
                [
                    {"kind": "Pedestrian", "positions": [[20.0, 0.5], [20.0, 3.5], [20.0, 4.0], [20.0, 4.0]]},
                    {"kind": "Pedestrian", "positions": [[22.0, 4.0], [22.0, 4.0], [22.0, 3.5], [22.0, 0.5]]},
                ]
            )
        elif predicate == "rule_mode":
            ego_track["positions"] = [[0.0, 0.0], [2.0, 0.0], [4.0, 0.0], [6.0, 0.0]]
        elif predicate in ("conditional_trigger_unresolved", "temporal_structure"):
            tracks.append(
                {"kind": "Car", "positions": [[10.0, 3.5], [8.0, 3.5], [7.0, 3.5], [7.0, 3.5]]}
            )
    elif fact["kind"] == "cpd":
        dimension = fact["dimension"]
        value = fact["value"]
        ego_track = tracks[0]
        if dimension == "actor_longitudinal_distance_bin":
            distance = {"near": 5.0, "medium": 20.0, "far": 40.0}[value]
            if present:
                tracks.append(
                    {
                        "kind": "Motorcycle",
                        "positions": [
                            [distance, 6.0],
                            [distance + 2.0, 6.0],
                            [distance + 4.0, 6.0],
                            [distance + 6.0, 6.0],
                        ],
                    }
                )
        elif dimension == "merge_gap_relation":
            ego_track["positions"] = (
                [[0.0, 0.0], [2.0, 0.0], [4.0, 1.5], [6.0, 3.5]]
                if present
                else [[0.0, 0.0], [2.0, 0.0], [4.0, 0.0], [6.0, 0.0]]
            )
            actor_positions = (
                [[-5.0, 3.5], [-3.0, 3.5], [-1.0, 3.5], [1.0, 3.5]]
                if value == "ahead_of_gap_actor"
                else [[6.0, 3.5], [8.0, 3.5], [10.0, 3.5], [12.0, 3.5]]
            )
            tracks.append({"kind": "Car", "positions": actor_positions})
        elif dimension == "optional_road_feature_presence":
            template["feature_carriers"].append(value)
        elif dimension == "yield_realization_mode":
            tracks.append(
                {
                    "kind": "Pedestrian",
                    "positions": (
                        [
                            [6.0, 1.0],
                            [6.0, 0.5],
                            [6.0, 0.0],
                            [6.0, -1.0],
                        ]
                        if present
                        else [
                            [6.0, 3.0],
                            [8.0, 3.0],
                            [10.0, 3.0],
                            [12.0, 3.0],
                        ]
                    ),
                }
            )
            ego_track["positions"] = (
                (
                    [[0.0, 0.0], [4.0, 0.0], [6.0, 0.0], [7.0, 0.0]]
                    if value == "decelerate"
                    else [[0.0, 0.0]] * 4
                )
                if present
                else [[0.0, 0.0], [2.0, 0.0], [4.0, 0.0], [6.0, 0.0]]
            )
    return template


def _scenic_source(template):
    lines = [
        "param map = localPath({!r})".format(str(CARLA_MAP)),
        "model scenic.simulators.carla.model",
    ]
    if any(len(track["positions"]) > 1 for track in template["tracks"]):
        lines.extend(
            [
                "behavior B(p):",
                "    do FollowTrajectoryBehavior(trajectory=p)",
            ]
        )
    for index, track in enumerate(template["tracks"]):
        points = track["positions"]
        if len(points) > 1:
            encoded = ", ".join("{}@{}".format(*point) for point in points)
            lines.append("P{} = [{}]".format(index, encoded))
            position = "P{}[0]".format(index)
        else:
            position = "{}@{}".format(*points[0])
        name = "ego" if track.get("is_ego") else "O{}".format(index)
        lines.append("{} = {} at {},".format(name, track["kind"], position))
        lines.append("    with regionContainedIn None,")
        if track["kind"] == "Car":
            if track.get("is_ego"):
                lines.append(
                    "    with blueprint {!r},".format(track["blueprint"])
                )
            if "length_m" in track:
                lines.append("    with length {},".format(track["length_m"]))
            if "width_m" in track:
                lines.append("    with width {},".format(track["width_m"]))
        if len(points) > 1:
            lines.append("    with behavior B(P{}),".format(index))
        lines[-1] = lines[-1].rstrip(",")
    carriers = set(template["feature_carriers"])
    if any(value.startswith("has_crosswalk_zone:") for value in carriers):
        for offset, point in enumerate(((10.0, -4.0), (10.0, 4.0))):
            lines.append("X{} = Cone at {}@{}, with regionContainedIn None".format(offset, *point))
        if "has_crosswalk_zone:present" in carriers:
            for offset, y in enumerate((-2.0, -0.5, 0.5, 2.0), start=2):
                lines.append("X{} = Cone at 10@{}, with regionContainedIn None".format(offset, y))
    if any(value.startswith("has_nonmotor_lane:") for value in carriers):
        lines.extend(
            [
                "X0 = Cone at 15@3.5, with regionContainedIn None",
                "X1 = Cone at 25@3.5, with regionContainedIn None",
            ]
        )
        if "has_nonmotor_lane:present" in carriers:
            lines.extend(
                [
                    "X2 = Cone at 18@3.5, with regionContainedIn None",
                    "X3 = Cone at 22@3.5, with regionContainedIn None",
                ]
            )
    return "\n".join(lines) + "\n"


def _metadrive_document(template, case_id, token_registry_binding):
    length = max([len(track["positions"]) for track in template["tracks"]] or [1])
    ego = None
    actors = []

    def states(positions):
        values = [list(position[:2]) for position in positions]
        values.extend([values[-1]] * (length - len(values)))
        result = []
        for index, position in enumerate(values):
            next_position = values[min(index + 1, len(values) - 1)]
            dx = float(next_position[0]) - float(position[0])
            dy = float(next_position[1]) - float(position[1])
            result.append(
                {
                    "step": index,
                    "position_m": [float(position[0]), float(position[1])],
                    "heading_rad": math.atan2(dy, dx) if dx or dy else 0.0,
                    "speed_m_s": math.hypot(dx, dy) / METADRIVE_STEP_SECONDS,
                    "valid": True,
                }
            )
        return result

    for index, track in enumerate(template["tracks"]):
        if track["kind"] in ("BusStop", "Cone"):
            continue
        kind = track["kind"]
        if kind == "Bicycle":
            actor_type, default_length, default_width = "bicycle", 2.0, 1.0
        elif kind == "Pedestrian":
            actor_type, default_length, default_width = "pedestrian", 0.5, 0.5
        elif kind == "Motorcycle":
            actor_type, default_length, default_width = "motorcycle", 2.0, 1.0
        else:
            actor_type, default_length, default_width = "vehicle", 4.5, 1.8
        trajectory = states(track["positions"])
        if track.get("is_ego"):
            ego = {
                "actor_id": "ego",
                "actor_type": "vehicle",
                "vehicle_model": track["vehicle_model"],
                "length_m": track.get("length_m", default_length),
                "width_m": track.get("width_m", default_width),
                "reference_trajectory": trajectory,
            }
        else:
            actors.append(
                {
                    "actor_id": "actor-{}".format(index),
                    "actor_type": actor_type,
                    "length_m": track.get("length_m", default_length),
                    "width_m": track.get("width_m", default_width),
                    "motion_mode": "replay_trajectory",
                    "trajectory": trajectory,
                }
            )
    if ego is None:
        raise ValueError("MetaDrive PG fixture contract requires one ego actor")
    return {
        "schema_version": "0.1",
        "artifact_type": "metadrive_pg_block_scene_v0.1",
        "time_step_s": METADRIVE_STEP_SECONDS,
        "horizon_steps": length,
        "map": {
            "generation_type": "block_sequence",
            "block_sequence": "S",
            "map_seed": int(sha256_bytes(case_id.encode("utf-8"))[:8], 16)
            % (2**31),
            "lane_num": 2,
            "lane_width_m": 3.5,
            "exit_length_m": 50.0,
            "token_registry_id": "metadrive-pg-token-registry-v0.1",
            "token_registry_sha256": token_registry_binding["sha256"],
        },
        "ego": ego,
        "actors": actors,
    }


def _native_extractor_fixture_input(
    platform, fact, case_id, metadrive_token_registry=None
):
    template = _physical_template(fact)
    if platform == "carla":
        text = _scenic_source(template)
        artifact_format = "scenic"
    else:
        if metadrive_token_registry is None:
            raise ValueError("MetaDrive fixture requires a frozen token registry")
        text = canonical_json_bytes(
            _metadrive_document(template, case_id, metadrive_token_registry)
        ).decode("utf-8")
        artifact_format = "metadrive_pg_block_scene_v0.1"
    encoded = text.encode("utf-8")
    return {
        "schema_version": SCHEMA_VERSION,
        "platform": platform,
        "artifact_format": artifact_format,
        "terminal_status": "complete",
        "disposition": "generate",
        "artifact": {
            "sha256": sha256_bytes(encoded),
            "bytes": len(encoded),
            "text": text,
            "dependencies": (
                [
                    {
                        "kind": "opendrive",
                        "path": str(CARLA_MAP),
                        "sha256": sha256_file(CARLA_MAP),
                        "bytes": CARLA_MAP.stat().st_size,
                    }
                ]
                if platform == "carla"
                else [
                    {
                        "kind": "metadrive_pg_token_registry",
                        **copy.deepcopy(metadrive_token_registry),
                    }
                ]
            ),
        },
    }


def _fixture_fact_for_concept(concept, *, present=True, ego_variant="approved"):
    definition = concept["definition"]
    if definition["kind"] == "ego_proxy":
        return {"kind": "ego", "ego_variant": ego_variant}
    if definition["kind"] == "requirement_atom_domain":
        representative = _concept_deterministic_arguments(concept)
        return {
            "kind": "atom",
            "category": definition["category"],
            "predicate": definition["predicate"],
            "arguments": copy.deepcopy(
                representative[0]
                if representative
                else definition["argument_domain"][0]
            ),
            "present": present,
            "deterministic": bool(representative),
            "ego_variant": ego_variant,
        }
    if definition["kind"] == "cpd_dimension":
        variant = definition["variants"][0]
        allowed_values = variant.get("allowed_values", [])
        if not allowed_values:
            raise AssertionError("formal CPD fixture lacks an allowed value")
        return {
            "kind": "cpd",
            "dimension": definition["name"],
            "value": copy.deepcopy(allowed_values[0]),
            "present": present,
            "ego_variant": ego_variant,
        }
    raise AssertionError("unknown formal ontology concept kind")


def _cpd_allowed_values(concept):
    encoded = {
        canonical_json_bytes(value).decode("utf-8")
        for variant in concept["definition"]["variants"]
        for value in variant.get("allowed_values", [])
    }
    return [json.loads(value) for value in sorted(encoded)]


def _review_atom_projection(atom):
    return {
        field: copy.deepcopy(atom[field])
        for field in ("category", "predicate", "arguments", "layer", "polarity", "weight")
    }


def _review_atom_hash(atoms):
    return sha256_bytes(
        canonical_json_bytes([_review_atom_projection(atom) for atom in atoms])
    )


def _build_development_reference(
    root,
    platform,
    dev_rows,
    clusters,
    extractor_manifest,
    extractor_manifest_path,
    created_at,
    metadrive_token_registry,
):
    source_id = "benchmark-reference-{}-v0.1".format(platform)
    source_config_path = root / "calibration_source_{}.json".format(platform)
    source_config_fields = {
        "decision_status": "confirmed",
        "source_id": source_id,
        "platform": platform,
        "reference_corpus": True,
        "method_independent": True,
        "query_input_fields": ["query_text"],
        "post_output_semantic_repair": False,
    }
    producer_source = Path(__file__).resolve()
    implementation_bundle_path = (
        root / "calibration_source_{}_implementation_bundle.json".format(platform)
    )
    write_json(
        implementation_bundle_path,
        {
            "schema_version": SCHEMA_VERSION,
            "bundle_id": "{}-implementation-v0.1".format(source_id),
            "platform": platform,
            "source_id": source_id,
            "source_config": copy.deepcopy(source_config_fields),
            "producer_source": {
                "path": str(producer_source),
                "sha256": sha256_file(producer_source),
                "bytes": producer_source.stat().st_size,
            },
            "semantic_extractor_manifest": {
                "path": str(Path(extractor_manifest_path).resolve()),
                "sha256": sha256_file(Path(extractor_manifest_path)),
                "bytes": Path(extractor_manifest_path).stat().st_size,
            },
            "metadrive_token_registry": (
                copy.deepcopy(metadrive_token_registry)
                if platform == "metadrive"
                else None
            ),
        },
    )
    implementation_bundle = {
        "path": str(implementation_bundle_path.resolve()),
        "sha256": sha256_file(implementation_bundle_path),
        "bytes": implementation_bundle_path.stat().st_size,
    }
    write_json(
        source_config_path,
        {**source_config_fields, "implementation_bundle": implementation_bundle},
    )
    roster = build_roster(
        dev_rows,
        DEV_LIBRARY,
        source_id,
        platform,
        clusters,
    )
    roster_path = root / "development_roster_{}.json".format(platform)
    write_json(roster_path, roster)
    entries = {record["query_id"]: record for record in roster["queries"]}
    artifact_directory = root / "development_artifacts_{}".format(platform)
    artifact_directory.mkdir()
    responses = []
    evidence_records = []
    fact = {"kind": "ego", "ego_variant": "approved"}
    projection = _projection_from_fixture_fact(fact)
    suffix = ".scenic" if platform == "carla" else ".json"
    for source in sorted(dev_rows, key=lambda value: value["query_id"]):
        query_id = source["query_id"]
        entry = entries[query_id]
        for repetition in range(5):
            run_id = "{}--{}--{}".format(platform, query_id, repetition)
            extractor_input = _native_extractor_fixture_input(
                platform, fact, run_id, metadrive_token_registry
            )
            artifact_path = artifact_directory / "{}{}".format(run_id, suffix)
            artifact_path.write_text(
                extractor_input["artifact"]["text"], encoding="utf-8"
            )
            stdout_path = artifact_directory / "{}.stdout".format(run_id)
            stderr_path = artifact_directory / "{}.stderr".format(run_id)
            stdout_path.write_text(
                "generated development reference artifact {}\n".format(run_id),
                encoding="utf-8",
            )
            stderr_path.write_bytes(b"")
            raw_envelope = {
                "stdout": {
                    "path": str(stdout_path),
                    "sha256": sha256_file(stdout_path),
                    "bytes": stdout_path.stat().st_size,
                },
                "stderr": {
                    "path": str(stderr_path),
                    "sha256": sha256_file(stderr_path),
                    "bytes": stderr_path.stat().st_size,
                },
                "exit_code": 0,
                "timed_out": False,
            }
            response = {
                "schema_version": SCHEMA_VERSION,
                "run_id": run_id,
                "method_id": source_id,
                "platform": platform,
                "query_id": query_id,
                "intent_group_id": entry["intent_group_id"],
                "statistical_intent_cluster_id": entry[
                    "statistical_intent_cluster_id"
                ],
                "surface_style": entry["surface_style"],
                "repetition": repetition,
                "expected_support": entry["expected_support"],
                "disposition": "generate",
                "request_sha256": sha256_bytes(
                    canonical_json_bytes({"query_text": source["query_text"]})
                ),
                "config_sha256": sha256_file(source_config_path),
                "implementation_bundle_sha256": implementation_bundle["sha256"],
                "terminal_status": "complete",
                "artifact": {
                    "path": str(artifact_path),
                    "sha256": sha256_file(artifact_path),
                    "bytes": artifact_path.stat().st_size,
                },
                "raw_method_response": {
                    **raw_envelope,
                    "envelope_sha256": sha256_bytes(
                        canonical_json_bytes(raw_envelope)
                    ),
                },
                "provenance": {
                    "producer_id": "formal-development-reference-fixture",
                    "producer_version": "0.1",
                    "created_at_utc": created_at,
                },
            }
            responses.append(response)
            evidence_records.append(
                {
                    "schema_version": SCHEMA_VERSION,
                    "run_id": run_id,
                    "method_id": source_id,
                    "platform": platform,
                    "query_id": query_id,
                    "intent_group_id": entry["intent_group_id"],
                    "statistical_intent_cluster_id": entry[
                        "statistical_intent_cluster_id"
                    ],
                    "surface_style": entry["surface_style"],
                    "repetition": repetition,
                    "expected_support": entry["expected_support"],
                    "terminal_status": "complete",
                    **copy.deepcopy(projection),
                    "provenance": {
                        "source_response_sha256": sha256_bytes(
                            canonical_json_bytes(response)
                        ),
                        "source_artifact_sha256": response["artifact"]["sha256"],
                        "extractor_id": extractor_manifest["extractor_id"],
                        "extractor_version": extractor_manifest["extractor_version"],
                        "extractor_config_sha256": sha256_file(
                            extractor_manifest_path
                        ),
                        "created_at_utc": created_at,
                    },
                }
            )
    response_path = root / "development_responses_{}.jsonl".format(platform)
    evidence_path = root / "development_evidence_{}.jsonl".format(platform)
    write_jsonl(response_path, responses)
    write_jsonl(evidence_path, evidence_records)
    return {
        "platform": platform,
        "source_id": source_id,
        "source_config_path": source_config_path,
        "implementation_bundle_path": implementation_bundle_path,
        "roster_path": roster_path,
        "response_path": response_path,
        "evidence_path": evidence_path,
        "responses": responses,
        "evidence": evidence_records,
    }


def build_formal_bundle(directory):
    root = Path(directory)
    paths = {}
    test_rows = read_jsonl(TEST_LIBRARY)
    dev_rows = read_jsonl(DEV_LIBRARY)
    source_records = test_rows + dev_rows
    test_draft_records = read_jsonl(TEST_ORACLE_DRAFT)
    dev_draft_records = read_jsonl(DEV_ORACLE_DRAFT)
    draft_records = test_draft_records + dev_draft_records
    draft_by_id = {record["query_id"]: record for record in draft_records}
    runtime_token_registry = read_json(
        ROOT
        / "benchmark_configs"
        / "methods"
        / "metadrive_pg_token_registry_draft.json"
    )
    runtime_token_registry["decision_status"] = "frozen"
    paths["metadrive_pg_token_registry"] = root / "metadrive_pg_token_registry.json"
    write_json(paths["metadrive_pg_token_registry"], runtime_token_registry)
    runtime_token_binding = {
        "path": str(paths["metadrive_pg_token_registry"].resolve()),
        "sha256": sha256_file(paths["metadrive_pg_token_registry"]),
        "bytes": paths["metadrive_pg_token_registry"].stat().st_size,
    }

    test_oracle = _confirmed_oracle(TEST_LIBRARY)
    dev_oracle = _confirmed_oracle(DEV_LIBRARY)
    development_cpd_dimensions = {
        dimension["name"]
        for record in dev_oracle
        for dimension in record["cpd_policy"]["dimensions"]
    }
    for record in test_oracle:
        policy = record["cpd_policy"]
        if any(
            dimension["name"] not in development_cpd_dimensions
            for dimension in policy["dimensions"]
        ):
            policy["eligible"] = False
            policy["cross_platform_judgeable"] = False
    paths["requirement_oracle_test"] = root / "test_oracle.jsonl"
    paths["requirement_oracle_dev"] = root / "dev_oracle.jsonl"
    write_jsonl(paths["requirement_oracle_test"], test_oracle)
    write_jsonl(paths["requirement_oracle_dev"], dev_oracle)

    cpd = compute_coverage(test_oracle)
    paths["cpd_policy"] = root / "cpd.json"
    write_json(paths["cpd_policy"], cpd)

    clusters = read_json(ROOT / "benchmark_artifacts" / "drafts" / "statistical_clusters_draft.json")
    clusters["decision_status"] = "confirmed"
    for cluster in clusters["clusters"]:
        cluster["decision_status"] = "confirmed"
    paths["statistical_clusters"] = root / "clusters.json"
    write_json(paths["statistical_clusters"], clusters)

    all_oracles = test_oracle + dev_oracle
    query_source_bindings = {
        "test": {
            "library_sha256": sha256_file(TEST_LIBRARY),
            "library_hash_scope": "file_bytes",
            "oracle_sha256": sha256_file(TEST_ORACLE_DRAFT),
            "oracle_hash_scope": "file_bytes",
            "record_count": len(test_rows),
        },
        "dev": {
            "library_sha256": sha256_file(DEV_LIBRARY),
            "library_hash_scope": "file_bytes",
            "oracle_sha256": sha256_file(DEV_ORACLE_DRAFT),
            "oracle_hash_scope": "file_bytes",
            "record_count": len(dev_rows),
        },
    }
    source_by_id = {record["query_id"]: record for record in source_records}
    reviews = []
    for record in all_oracles:
        draft = draft_by_id[record["query_id"]]
        review_payload = {
            "status": "complete",
            "reviewer_id": "reviewer",
            "required_check_status": {
                check: "complete" for check in REQUIRED_REVIEW_CHECKS
            },
            "draft_atom_reviews": [
                {
                    "draft_atom_id": atom["atom_id"],
                    "verdict": "accept",
                    "replacement_atom_ids": [],
                    "operation_group_id": None,
                    "reason": "machine draft accepted unchanged",
                    "before_sha256": _review_atom_hash([atom]),
                    "after_sha256": _review_atom_hash([atom]),
                    "changed_fields": [],
                }
                for atom in draft["atoms"]
            ],
            "added_atom_reviews": [],
            "decision": copy.deepcopy(record),
        }
        gold = {
            "schema_version": SCHEMA_VERSION,
            "workflow_type": "query_oracle_human_gold",
            "query_id": record["query_id"],
            "subject_sha256": sha256_bytes(
                canonical_json_bytes(
                    {
                        "query_record": source_by_id[record["query_id"]],
                        "oracle_draft": draft,
                    }
                )
            ),
            "reviewer_id": "reviewer",
            "review_status": "complete",
            "canonical_source_revalidated": True,
            "source_binding": copy.deepcopy(
                query_source_bindings[
                    "test"
                    if record["query_id"] in {row["query_id"] for row in test_rows}
                    else "dev"
                ]
            ),
            "review_payload": review_payload,
            "decision_status": "confirmed",
        }
        gold["gold_record_id"] = sha256_bytes(canonical_json_bytes(gold))
        reviews.append(gold)
    paths["human_query_gold"] = root / "human_query_gold.jsonl"
    write_jsonl(paths["human_query_gold"], reviews)

    ontology = build_common_ontology(dev_oracle)
    paths["common_ontology_registry"] = root / "common_ontology.json"
    write_json(paths["common_ontology_registry"], ontology)
    for platform in ("carla", "metadrive"):
        role = "platform_capability_{}".format(platform)
        paths[role] = root / "{}.json".format(role)
        role = "platform_config_{}".format(platform)
        paths[role] = root / "{}.json".format(role)
        runtime_worker = PACKAGE_ROOT / "runtime_worker.py"
        if platform == "carla":
            runtime_interpreter = CARLA_PYTHON.resolve()
            runtime_environment = {
                "PYTHON_EGG_CACHE": "/tmp/python-eggs",
                "PYTHONPATH": str(
                    ROOT
                    / "CARLA_0.9.13"
                    / "PythonAPI"
                    / "carla"
                    / "dist"
                    / "carla-0.9.13-py3.8-linux-x86_64.egg"
                ),
            }
            runtime_timeout = 180
            dependency_root = root / "formal-carla-runtime-dependencies"
            server_root = dependency_root / "server-distribution"
            server_binary = (
                server_root
                / "CarlaUE4"
                / "Binaries"
                / "Linux"
                / "CarlaUE4-Linux-Shipping"
            )
            server_binary.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile("/bin/true", server_binary)
            server_binary.chmod(0o755)
            runtime_environment_observation = probe_python_environment(
                runtime_interpreter
            )
            runtime_environment_root = runtime_interpreter.parent.parent.resolve()
            runtime_environment_manifest = dependency_root / "environment.json"
            write_json(
                runtime_environment_manifest,
                {
                    "schema_version": "0.1",
                    "decision_status": "frozen",
                    "runtime_kind": "python",
                    "executable": {
                        "path": str(runtime_interpreter.resolve()),
                        "sha256": sha256_file(runtime_interpreter),
                        "bytes": runtime_interpreter.stat().st_size,
                    },
                    "python_environment_root": str(runtime_environment_root),
                    "python_environment_control_files": [
                        {
                            "path": str(path),
                            "sha256": sha256_file(path),
                            "bytes": path.stat().st_size,
                        }
                        for path in python_environment_control_paths(
                            runtime_environment_root
                        )
                    ],
                    **runtime_environment_observation,
                },
            )
            carla_egg = Path(runtime_environment["PYTHONPATH"])
            runtime_dependencies = {
                "environment_manifest": {
                    "path": str(runtime_environment_manifest.resolve()),
                    "sha256": sha256_file(runtime_environment_manifest),
                    "bytes": runtime_environment_manifest.stat().st_size,
                },
                "python_path_entries": [
                    {
                        "path": str(carla_egg.resolve()),
                        "sha256": sha256_file(carla_egg),
                        "bytes": carla_egg.stat().st_size,
                    }
                ],
                "source_trees": [
                    runtime_tree_binding(
                        server_root.resolve(), "carla_server_distribution"
                    ),
                    runtime_tree_binding(
                        (ROOT / "Scenic" / "src").resolve(),
                        "scenic_editable_source",
                    ),
                ],
                "map_catalog": [
                    {
                        "path": str(CARLA_MAP.resolve()),
                        "sha256": sha256_file(CARLA_MAP),
                        "bytes": CARLA_MAP.stat().st_size,
                    }
                ],
            }
            runtime_platform_fields = {
                "carla_endpoint": {
                    "host": "127.0.0.1",
                    "port": 2000,
                    "timeout_seconds": 10.0,
                },
                "server_binary": {
                    "path": str(server_binary.resolve()),
                    "sha256": sha256_file(server_binary),
                    "bytes": server_binary.stat().st_size,
                },
                "runtime_dependencies": runtime_dependencies,
            }
        else:
            runtime_interpreter = METADRIVE_PYTHON.resolve()
            runtime_environment = {}
            runtime_timeout = 120
            dependency_root = root / "formal-metadrive-runtime-dependencies"
            dependency_root.mkdir(parents=True, exist_ok=True)
            runtime_environment_manifest = dependency_root / "environment.json"
            write_json(
                runtime_environment_manifest,
                {
                    "schema_version": "0.1",
                    "decision_status": "frozen",
                    "runtime_kind": "python",
                    "executable": {
                        "path": str(runtime_interpreter),
                        "sha256": sha256_file(runtime_interpreter),
                        "bytes": runtime_interpreter.stat().st_size,
                    },
                    **probe_python_environment(runtime_interpreter),
                },
            )
            metadrive_module = Path(
                "/home/shijie20/CodeSpace/mdsn/metadrive/metadrive/__init__.py"
            ).resolve()
            runtime_dependencies = {
                "environment_manifest": {
                    "path": str(runtime_environment_manifest.resolve()),
                    "sha256": sha256_file(runtime_environment_manifest),
                    "bytes": runtime_environment_manifest.stat().st_size,
                },
                "metadrive_module": {
                    "path": str(metadrive_module),
                    "sha256": sha256_file(metadrive_module),
                    "bytes": metadrive_module.stat().st_size,
                },
                "source_tree": metadrive_tracked_source_binding(
                    Path("/home/shijie20/CodeSpace/mdsn/metadrive")
                ),
            }
            runtime_platform_fields = {
                "token_registry": copy.deepcopy(runtime_token_binding),
                "runtime_dependencies": runtime_dependencies,
            }
        write_json(
            paths[role],
            {
                "schema_version": "0.1",
                "platform": platform,
                "status": "frozen",
                "sv_seeds": [0, 1, 2, 3, 4],
                "sv_max_iterations": 2000,
                "rollout_seconds": 30.0,
                **runtime_platform_fields,
                "worker": {
                    "interpreter": str(runtime_interpreter.resolve()),
                    "interpreter_sha256": sha256_file(runtime_interpreter),
                    "worker": str(runtime_worker.resolve()),
                    "worker_sha256": sha256_file(runtime_worker),
                    "timeout_seconds": runtime_timeout,
                    "environment": runtime_environment,
                },
            },
        )
        role = "controller_config_{}".format(platform)
        paths[role] = root / "{}.json".format(role)
        controller_source = PACKAGE_ROOT / "runtime_worker.py"
        if platform == "carla":
            implementation_class = "FixedIDMPIDBehavior"
            proxy_id = "vehicle.chevrolet.impala"
            parameters = {
                "dt_seconds": 0.1,
                "target_speed_m_s": 8.3333333333,
                "lookahead_seconds": 1.0,
                "lateral_kp": 1.2,
                "lateral_ki": 0.0,
                "lateral_kd": 0.2,
                "max_front_wheel_rate_rad_s": 0.419,
                "max_front_wheel_angle_rad": 0.603,
                "lane_half_width_m": 2.1,
                "idm_delta": 4.0,
                "max_acceleration_m_s2": 0.7,
                "comfortable_deceleration_m_s2": 2.0,
                "max_deceleration_m_s2": 4.0,
                "max_longitudinal_jerk_m_s3": 4.0,
                "minimum_gap_m": 2.0,
                "time_headway_seconds": 1.5,
            }
            action_contract = {
                "lateral": "normalized steering [-1,1]",
                "longitudinal": "separate throttle/brake [0,1]",
                "braking_preserved": True,
            }
        else:
            implementation_class = "FrozenBusIDMPolicy"
            proxy_id = "xl-topdown-proxy"
            parameters = {
                "target_speed_km_h": 30.0,
                "acceleration_factor": 0.7,
                "deceleration_factor": -4.0,
                "dt_seconds": 0.1,
            }
            action_contract = {
                "lateral": "normalized steering [-1,1]",
                "longitudinal": (
                    "normalized throttle/brake [-1,1]; IDM factors are dimensionless "
                    "and not SI acceleration bounds"
                ),
                "braking_preserved": True,
            }
        write_json(
            paths[role],
            {
                "schema_version": "0.1",
                "platform": platform,
                "status": "frozen",
                "controller_id": "{}-fixed-query-blind-v0.1".format(platform),
                "query_blind": True,
                "reads_query_metadata": False,
                "reads_evaluation_metadata": False,
                "ego_proxy": {
                    "length_m": 5.33,
                    "width_m": 2.10,
                    "proxy_id": proxy_id,
                },
                "implementation": {
                    "module": "bus_benchmark.runtime_worker",
                    "class": implementation_class,
                    "source_path": str(controller_source.resolve()),
                    "source_sha256": sha256_file(controller_source),
                },
                "parameters": parameters,
                "action_contract": action_contract,
            },
        )

    extractor_runtime = root / "extractor_runtime"
    extractor_runtime.mkdir()
    source_directory = PACKAGE_ROOT
    for filename in (
        "fixture_extractor_carla.py",
        "fixture_extractor_metadrive.py",
        "fixture_extractor_common.py",
        "native_observer.py",
        "metadrive_pg.py",
    ):
        shutil.copy2(source_directory / filename, extractor_runtime / filename)
    extractor_sources = {
        "carla": extractor_runtime / "fixture_extractor_carla.py",
        "metadrive": extractor_runtime / "fixture_extractor_metadrive.py",
    }
    extractor_runtime_files = [
        extractor_sources["carla"],
        extractor_sources["metadrive"],
        extractor_runtime / "fixture_extractor_common.py",
        extractor_runtime / "native_observer.py",
        extractor_runtime / "metadrive_pg.py",
    ]
    ontology_by_id = {
        concept["concept_id"]: concept for concept in ontology["concepts"]
    }
    deterministic_cases = []
    for platform in ("carla", "metadrive"):
        for concept in ontology["concepts"]:
            concept_id = concept["concept_id"]
            positive_facts = [_fixture_fact_for_concept(concept)]
            if concept["kind"] == "cpd_dimension":
                positive_facts = []
                for value in _cpd_allowed_values(concept):
                    fact = _fixture_fact_for_concept(concept)
                    fact["value"] = value
                    positive_facts.append(fact)
            for positive_index, fact in enumerate(positive_facts):
                if concept["kind"] == "ego_proxy":
                    test_kind = "ego_approved"
                elif _concept_requires_judge(concept):
                    test_kind = "judge_required_positive"
                else:
                    test_kind = "positive"
                suffix = (
                    "{}-{}".format(test_kind, positive_index)
                    if len(positive_facts) > 1
                    else test_kind
                )
                case_id = "{}-{}-{}".format(
                    platform, concept_id.replace(":", "-"), suffix
                )
                negative_test_kind = (
                    "judge_required_negative"
                    if _concept_requires_judge(concept)
                    else "negative"
                )
                deterministic_cases.append(
                    {
                        "case_id": case_id,
                        "platform": platform,
                        "concept_ids": [concept_id],
                        "test_kind": test_kind,
                        "input": _native_extractor_fixture_input(
                            platform, fact, case_id, runtime_token_binding
                        ),
                        "expected": _projection_from_fixture_fact(fact),
                    }
                )
            if concept["kind"] in ("requirement_atom_domain", "cpd_dimension"):
                negative_fact = _fixture_fact_for_concept(concept, present=False)
                negative_id = "{}-{}-negative".format(
                    platform, concept_id.replace(":", "-")
                )
                deterministic_cases.append(
                    {
                        "case_id": negative_id,
                        "platform": platform,
                        "concept_ids": [concept_id],
                        "test_kind": negative_test_kind,
                        "input": _native_extractor_fixture_input(
                            platform, negative_fact, negative_id, runtime_token_binding
                        ),
                        "expected": _projection_from_fixture_fact(negative_fact),
                    }
                )

        ego_concept = ontology_by_id["ego:bus_proxy"]
        for variant in ("missing", "wrong_proxy", "wrong_footprint"):
            if platform == "metadrive" and variant == "missing":
                # The strict PG artifact schema rejects a missing ego before the
                # semantic extractor. That rejection is covered by contract tests.
                continue
            fact = _fixture_fact_for_concept(
                ego_concept, ego_variant=variant
            )
            case_id = "{}-ego-{}".format(platform, variant.replace("_", "-"))
            expected = _projection_from_fixture_fact(fact)
            if platform == "carla" and variant == "wrong_proxy":
                # The CARLA extractor normalizes a non-proxy vehicle model to
                # its platform-independent "small" label.
                expected["ego"]["vehicle_model"] = "small"
            deterministic_cases.append(
                {
                    "case_id": case_id,
                    "platform": platform,
                    "concept_ids": ["ego:bus_proxy"],
                    "test_kind": "ego_{}".format(variant),
                    "input": _native_extractor_fixture_input(
                        platform, fact, case_id, runtime_token_binding
                    ),
                    "expected": expected,
                }
            )
    paths["semantic_fixture_corpus"] = root / "semantic_fixture_corpus.json"
    write_json(
        paths["semantic_fixture_corpus"],
        {
            "decision_status": "confirmed",
            "benchmark_owned": True,
            "input_contract": "raw_platform_artifact_v0.1",
            "cases": deterministic_cases,
        },
    )
    deterministic_results = root / "deterministic_results.json"
    write_json(
        deterministic_results,
        {
            "decision_status": "confirmed",
            "cases": [
                {"case_id": case["case_id"], "actual": case["expected"]}
                for case in deterministic_cases
            ],
        },
    )
    equivalence_cases = []
    atom_by_category = {}
    for concept in ontology["concepts"]:
        definition = concept["definition"]
        if definition["kind"] == "requirement_atom_domain":
            atom_by_category.setdefault(definition["category"], concept)
    for category, concept in sorted(atom_by_category.items()):
        concept_id = concept["concept_id"]
        positive_fact = _fixture_fact_for_concept(concept)
        negative_fact = _fixture_fact_for_concept(concept, present=False)
        positive_projection = _projection_from_fixture_fact(positive_fact)
        negative_projection = _projection_from_fixture_fact(negative_fact)
        equivalent_id = "cross-{}-equivalent".format(category)
        equivalence_cases.append(
            {
                "case_id": equivalent_id,
                "concept_id": concept_id,
                "semantic_category": category,
                "changed_concept_ids": [],
                "expected_equal": True,
                "left_platform": "carla",
                "right_platform": "metadrive",
                "left_input": _native_extractor_fixture_input(
                    "carla", positive_fact, equivalent_id, runtime_token_binding
                ),
                "right_input": _native_extractor_fixture_input(
                    "metadrive", positive_fact, equivalent_id, runtime_token_binding
                ),
                "expected_left": positive_projection,
                "expected_right": positive_projection,
            }
        )
        difference_id = "cross-{}-difference".format(category)
        if _concept_requires_judge(concept):
            continue
        equivalence_cases.append(
            {
                "case_id": difference_id,
                "concept_id": concept_id,
                "semantic_category": category,
                "changed_concept_ids": [concept_id],
                "expected_equal": False,
                "left_platform": "carla",
                "right_platform": "metadrive",
                "left_input": _native_extractor_fixture_input(
                    "carla", positive_fact, difference_id, runtime_token_binding
                ),
                "right_input": _native_extractor_fixture_input(
                    "metadrive", negative_fact, difference_id, runtime_token_binding
                ),
                "expected_left": positive_projection,
                "expected_right": negative_projection,
            }
        )
    for concept in ontology["concepts"]:
        if concept["kind"] != "cpd_dimension":
            continue
        for value_index, value in enumerate(_cpd_allowed_values(concept)):
            fact = _fixture_fact_for_concept(concept)
            fact["value"] = value
            projection = _projection_from_fixture_fact(fact)
            case_id = "cross-cpd-{}-{}".format(
                concept["definition"]["name"], value_index
            )
            equivalence_cases.append(
                {
                    "case_id": case_id,
                    "concept_id": concept["concept_id"],
                    "semantic_category": "cpd",
                    "changed_concept_ids": [],
                    "expected_equal": True,
                    "left_platform": "carla",
                    "right_platform": "metadrive",
                    "left_input": _native_extractor_fixture_input(
                        "carla", fact, case_id, runtime_token_binding
                    ),
                    "right_input": _native_extractor_fixture_input(
                        "metadrive", fact, case_id, runtime_token_binding
                    ),
                    "expected_left": projection,
                    "expected_right": projection,
                }
            )
    paths["cross_platform_fixture_corpus"] = root / "cross_platform_fixture_corpus.json"
    write_json(
        paths["cross_platform_fixture_corpus"],
        {
            "decision_status": "confirmed",
            "benchmark_owned": True,
            "input_contract": "raw_platform_artifact_v0.1",
            "cases": equivalence_cases,
        },
    )
    equivalence_results = root / "equivalence_results.json"
    write_json(
        equivalence_results,
        {
            "decision_status": "confirmed",
            "cases": [
                {
                    "case_id": case["case_id"],
                    "actual_left": case["expected_left"],
                    "actual_right": case["expected_right"],
                }
                for case in equivalence_cases
            ],
        },
    )

    capability_fixture_ids = {
        platform: {concept_id: set() for concept_id in ontology_by_id}
        for platform in ("carla", "metadrive")
    }
    for case in deterministic_cases:
        for concept_id in case["concept_ids"]:
            capability_fixture_ids[case["platform"]][concept_id].add(
                case["case_id"]
            )
    for case in equivalence_cases:
        for platform in ("carla", "metadrive"):
            capability_fixture_ids[platform][case["concept_id"]].add(
                case["case_id"]
            )
    for platform in ("carla", "metadrive"):
        role = "platform_capability_{}".format(platform)
        write_json(
            paths[role],
            {
                "platform": platform,
                "decision_status": "confirmed",
                "method_independent": True,
                "ontology_id": ontology["ontology_id"],
                "ontology_sha256": sha256_file(
                    paths["common_ontology_registry"]
                ),
                "concepts": [
                    {
                        "concept_id": concept["concept_id"],
                        "definition_sha256": concept["definition_sha256"],
                        "status": (
                            "native"
                            if concept["kind"] == "ego_proxy"
                            else "judge_required"
                            if _concept_requires_judge(concept)
                            else "constructible"
                        ),
                        "evidence_mode": "native_observation"
                        if concept["kind"] == "ego_proxy"
                        else "judge"
                        if _concept_requires_judge(concept)
                        else "constructed_native",
                        "rationale": (
                            "all argument values are intentionally routed to the frozen judge"
                            if _concept_requires_judge(concept)
                            else "only the registered representative argument is deterministic; all other argument values route to the frozen judge"
                            if concept["kind"] == "requirement_atom_domain"
                            else "covered by audited native objects, geometry, trajectories, and the frozen extractor"
                        ),
                        "coverage_scope": (
                            "registered_values_only"
                            if concept["kind"] == "requirement_atom_domain"
                            else "full_definition"
                        ),
                        "uncovered_route": (
                            "judge"
                            if concept["kind"] == "requirement_atom_domain"
                            else "not_applicable"
                        ),
                        "deterministic_argument_values": (
                            deterministic_representative_arguments(
                                concept["definition"]["category"],
                                concept["definition"]["predicate"],
                                concept["definition"]["polarity"],
                                concept["definition"]["argument_domain"],
                            )
                            if concept["kind"] == "requirement_atom_domain"
                            else []
                        ),
                        "fixture_case_ids": sorted(
                            capability_fixture_ids[platform][concept["concept_id"]]
                        ),
                        "evidence_binding": {
                            "kind": "extractor",
                            "sha256": sha256_file(extractor_sources[platform]),
                        },
                    }
                    for concept in ontology["concepts"]
                ],
            },
        )

    token_module = {
        "TollGate": "tollgate",
        "Bidirection": "bidirection",
        "Curve": "curve",
        "OutFork": "fork",
        "Roundabout": "roundabout",
        "ParkingLot": "parking_lot",
        "OutRampOnStraight": "ramp",
        "Straight": "straight",
        "StdTInterSection": "std_t_intersection",
        "StdInterSectionWithUTurn": "std_intersection",
        "StdInterSection": "std_intersection",
        "Split": "bottleneck",
        "InFork": "fork",
        "InRampOnStraight": "ramp",
        "Merge": "bottleneck",
    }
    parameter_contracts = {
        "Straight": [
            {"name": "length", "value_type": "number", "required": True, "minimum": 40.0, "maximum": 80.0}
        ],
        "Bidirection": [
            {"name": "length", "value_type": "number", "required": True, "minimum": 40.0, "maximum": 80.0}
        ],
        "Curve": [
            {"name": "length", "value_type": "number", "required": True, "minimum": 40.0, "maximum": 80.0},
            {"name": "radius", "value_type": "number", "required": True, "minimum": 25.0, "maximum": 60.0},
            {"name": "angle", "value_type": "number", "required": True, "minimum": 45.0, "maximum": 135.0},
            {"name": "dir", "value_type": "integer", "required": True, "allowed_values": [0, 1]},
        ],
    }
    source_by_name = {
        Path(binding["path"]).name: binding["sha256"]
        for binding in runtime_token_registry["source_files"]
    }
    block_tokens = []
    for token in runtime_token_registry["tokens"]:
        class_name = token["class_name"]
        module = token_module[class_name]
        block_tokens.append(
            {
                "token": token["token"],
                "block_id": token["token"],
                "class_name": class_name,
                "pg_block_class": "metadrive.component.pgblock.{}.{}".format(
                    module, class_name
                ),
                "implementation_file_sha256": source_by_name[
                    "{}.py".format(module)
                ],
                "parameter_contract": copy.deepcopy(
                    parameter_contracts.get(class_name, [])
                ),
                "map_parameter_constraints": copy.deepcopy(
                    token["map_parameter_constraints"]
                ),
                "road_semantics": ["generated_motor_road"],
            }
        )
    fixture_bindings = []
    metadrive_cases = [
        (case, case["concept_ids"], case["input"])
        for case in deterministic_cases
        if case["platform"] == "metadrive"
    ] + [
        (case, [case["concept_id"]], case["right_input"])
        for case in equivalence_cases
    ]
    for case, concept_ids, native_input in metadrive_cases:
        pg_artifact = json.loads(native_input["artifact"]["text"])
        fixture_bindings.append(
            {
                "case_id": case["case_id"],
                "concept_ids": concept_ids,
                "native_artifact_sha256": native_input["artifact"]["sha256"],
                "block_sequence": pg_artifact["map"]["block_sequence"],
                "map_parameters": {
                    "map_seed": pg_artifact["map"]["map_seed"],
                    "lane_num": pg_artifact["map"]["lane_num"],
                    "lane_width_m": pg_artifact["map"]["lane_width_m"],
                    "exit_length_m": pg_artifact["map"]["exit_length_m"],
                },
                "road_semantics": sorted(concept_ids),
                "semantic_regions": pg_artifact.get("semantic_regions", []),
                "semantic_regions_topology_effect": False,
            }
        )
    metadrive_block_registry = {
        "schema_version": "0.1",
        "decision_status": "confirmed",
        "platform": "metadrive",
        "input_contract": PG_BLOCK_SCENE_CONTRACT,
        "generator_class": "metadrive.component.map.pg_map.PGMap",
        "road_source_contract": "builtin_sequence_block_tokens_only",
        "token_registry_id": runtime_token_registry["registry_id"],
        "token_registry": runtime_token_binding,
        "implementation_files": copy.deepcopy(
            runtime_token_registry["source_files"]
        ),
        "global_parameters": [
            {"name": "generation_type", "value": "block_sequence"},
            {"name": "lane_num", "value": 2},
            {"name": "lane_width_m", "value": 3.5},
            {"name": "exit_length_m", "value": 50.0},
        ],
        "tokens": block_tokens,
        "fixture_bindings": fixture_bindings,
    }
    paths["metadrive_pg_block_registry"] = root / "metadrive_pg_block_registry.json"
    write_json(paths["metadrive_pg_block_registry"], metadrive_block_registry)

    platform_reviews = []

    def append_platform_review(
        review_kind,
        subject_id,
        platform,
        subject_sha256,
        audit_subject,
        definition_sha256,
        metadrive_registry_sha256,
        metadrive_token_registry_sha256,
        input_contract,
    ):
        binding = {
            "platform": platform,
            "subject_sha256": subject_sha256,
            "audit_subject_sha256": sha256_bytes(
                canonical_json_bytes(audit_subject)
            ),
            "ontology_definition_sha256": definition_sha256,
            "metadrive_pg_registry_sha256": metadrive_registry_sha256,
            "metadrive_pg_token_registry_sha256": metadrive_token_registry_sha256,
            "input_contract": input_contract,
        }
        label = {"verdict": "approve", "reason": "formal contract fixture"}
        gold = {
            "schema_version": SCHEMA_VERSION,
            "workflow_type": "platform_fixture_capability_human_gold",
            "review_kind": review_kind,
            "subject_id": subject_id,
            **binding,
            "reviewer": {**label, "reviewer_id": "reviewer"},
            "review_status": "complete",
            "decision_status": "confirmed",
        }
        gold["gold_record_id"] = sha256_bytes(canonical_json_bytes(gold))
        platform_reviews.append(gold)

    metadrive_registry_sha256 = sha256_file(paths["metadrive_pg_block_registry"])
    metadrive_token_registry_sha256 = sha256_file(
        paths["metadrive_pg_token_registry"]
    )
    append_platform_review(
        "registry_implementation",
        "metadrive:pg_block_registry",
        "metadrive",
        metadrive_registry_sha256,
        build_metadrive_registry_audit_subject(
            metadrive_block_registry,
            metadrive_registry_sha256,
            runtime_token_registry,
            runtime_token_binding,
        ),
        sha256_file(paths["common_ontology_registry"]),
        metadrive_registry_sha256,
        metadrive_token_registry_sha256,
        PG_BLOCK_SCENE_CONTRACT,
    )
    for case in deterministic_cases:
        concept_id = case["concept_ids"][0]
        platform = case["platform"]
        audit_subject = (
            build_metadrive_fixture_audit_subject(
                case,
                metadrive_block_registry,
                metadrive_registry_sha256,
                cross_platform=False,
            )
            if platform == "metadrive"
            else case
        )
        append_platform_review(
            "fixture_case",
            case["case_id"],
            platform,
            sha256_bytes(canonical_json_bytes(case)),
            audit_subject,
            ontology_by_id[concept_id]["definition_sha256"],
            metadrive_registry_sha256 if platform == "metadrive" else None,
            metadrive_token_registry_sha256 if platform == "metadrive" else None,
            PG_BLOCK_SCENE_CONTRACT
            if platform == "metadrive"
            else "raw_platform_artifact_v0.1",
        )
    for case in equivalence_cases:
        append_platform_review(
            "fixture_case",
            case["case_id"],
            "cross_platform",
            sha256_bytes(canonical_json_bytes(case)),
            build_metadrive_fixture_audit_subject(
                case,
                metadrive_block_registry,
                metadrive_registry_sha256,
                cross_platform=True,
            ),
            ontology_by_id[case["concept_id"]]["definition_sha256"],
            metadrive_registry_sha256,
            metadrive_token_registry_sha256,
            PG_BLOCK_SCENE_CONTRACT,
        )
    for platform in ("carla", "metadrive"):
        capability = read_json(paths["platform_capability_{}".format(platform)])
        for concept in capability["concepts"]:
            audit_subject = (
                build_metadrive_capability_audit_subject(
                    concept,
                    metadrive_block_registry,
                    metadrive_registry_sha256,
                )
                if platform == "metadrive"
                else concept
            )
            append_platform_review(
                "capability_status",
                "{}:{}".format(platform, concept["concept_id"]),
                platform,
                sha256_bytes(canonical_json_bytes(concept)),
                audit_subject,
                ontology_by_id[concept["concept_id"]]["definition_sha256"],
                metadrive_registry_sha256 if platform == "metadrive" else None,
                metadrive_token_registry_sha256 if platform == "metadrive" else None,
                PG_BLOCK_SCENE_CONTRACT
                if platform == "metadrive"
                else "raw_platform_artifact_v0.1",
            )
    paths["platform_fixture_review"] = root / "platform_fixture_reviews.jsonl"
    write_jsonl(paths["platform_fixture_review"], platform_reviews)

    evaluator_sources = sorted((PACKAGE_ROOT).glob("*.py"))
    evaluator_schemas = list(supported_schema_paths())
    metadrive_runtime_fixture = next(
        case["input"]
        for case in deterministic_cases
        if case["platform"] == "metadrive" and case["test_kind"] == "ego_approved"
    )
    extractor_manifest = {
        "decision_status": "confirmed",
        "extractor_id": "oracle-blind-semantic-extractor",
        "extractor_version": "0.2",
        "input_contract": "artifact_observation_v0.1",
        "output_contract": "semantic_projection_v0.1",
        "deterministic_fixture_accuracy": 1.0,
        "cross_platform_equivalence_passed": True,
        "runtime_environments": [
            _runtime_environment(
                platform,
                root,
                extractor_runtime / "native_observer.py",
                metadrive_runtime_fixture if platform == "metadrive" else None,
            )
            for platform in ("carla", "metadrive")
        ],
        "extractor_files": [
            {"path": str(path), "sha256": sha256_file(path)}
            for path in extractor_runtime_files
        ],
        "entrypoints": [
            {
                "platform": platform,
                "path": str(path),
                "sha256": sha256_file(path),
                "callable": "extract_fixture",
            }
            for platform, path in extractor_sources.items()
        ],
        "metric_files": [
            {"path": str(source), "sha256": sha256_file(source)}
            for source in evaluator_sources
        ],
        "schema_files": [
            {"path": str(source), "sha256": sha256_file(source)}
            for source in evaluator_schemas
        ],
        "deterministic_fixture_results": {
            "path": str(deterministic_results),
            "sha256": sha256_file(deterministic_results),
        },
        "cross_platform_equivalence_results": {
            "path": str(equivalence_results),
            "sha256": sha256_file(equivalence_results),
        },
    }
    paths["semantic_extractor_manifest"] = root / "extractor_manifest.json"
    write_json(paths["semantic_extractor_manifest"], extractor_manifest)

    paths["method_config"] = root / "method.json"
    write_json(
        paths["method_config"],
        {
            "status": "frozen",
            "method_id": "method-a",
            "platform": "carla",
            "query_input_fields": ["query_text"],
            "post_output_semantic_repair": False,
        },
    )

    dev_rows = read_jsonl(DEV_LIBRARY)
    dev_oracle_by_id = {record["query_id"]: record for record in dev_oracle}
    created_at = "2026-07-14T00:00:00+00:00"
    development_sources = [
        _build_development_reference(
            root,
            platform,
            dev_rows,
            clusters,
            extractor_manifest,
            paths["semantic_extractor_manifest"],
            created_at,
            runtime_token_binding,
        )
        for platform in ("carla", "metadrive")
    ]
    paths["judge_calibration_source_config"] = development_sources[0][
        "source_config_path"
    ]
    paths["judge_calibration_source_config_metadrive"] = development_sources[1][
        "source_config_path"
    ]
    paths["development_query_roster"] = development_sources[0]["roster_path"]
    paths["development_query_roster_metadrive"] = development_sources[1][
        "roster_path"
    ]
    paths["development_response_records"] = development_sources[0]["response_path"]
    paths["development_response_records_metadrive"] = development_sources[1][
        "response_path"
    ]
    paths["development_evidence_records"] = development_sources[0]["evidence_path"]
    paths["development_evidence_records_metadrive"] = development_sources[1][
        "evidence_path"
    ]
    response_by_run = {
        record["run_id"]: record
        for source in development_sources
        for record in source["responses"]
    }
    evidence_by_run = {
        record["run_id"]: record
        for source in development_sources
        for record in source["evidence"]
    }
    candidates = []
    for source in development_sources:
        for query_id, oracle in sorted(dev_oracle_by_id.items()):
            if oracle["expected_support"] != "supported":
                continue
            for repetition in range(5):
                run_id = "{}--{}--{}".format(source["platform"], query_id, repetition)
                response = response_by_run[run_id]
                evidence = evidence_by_run[run_id]
                response_sha256 = sha256_bytes(canonical_json_bytes(response))
                evidence_sha256 = sha256_bytes(canonical_json_bytes(evidence))
                for atom in oracle["atoms"]:
                    if atom["layer"] not in (
                        "core_required",
                        "surface_required",
                        "forbidden",
                    ):
                        continue
                    if deterministic_atom_verdict(atom, evidence) != "unknown":
                        continue
                    candidates.append(
                        {
                            "item_id": judge_item_id(
                                response_sha256, evidence_sha256, atom["atom_id"]
                            ),
                            "platform": source["platform"],
                            "source_id": source["source_id"],
                            "query_id": query_id,
                            "run_id": run_id,
                            "repetition": repetition,
                            "atom_id": atom["atom_id"],
                            "category": atom["category"],
                            "surface_style": oracle["surface_style"],
                            "source_response_sha256": response_sha256,
                            "source_evidence_sha256": evidence_sha256,
                        }
                    )
    calibration_roster = build_judge_calibration_roster(
        candidates, seed="bus-judge-calibration-v0.1"
    )
    paths["judge_calibration_roster"] = root / "judge_calibration_roster.json"
    write_json(paths["judge_calibration_roster"], calibration_roster)

    prompt = root / "judge_prompt.txt"
    prompt.write_text(
        "Find evidence first, then provide a concise rationale and one semantic-atom verdict as strict JSON.\n",
        encoding="utf-8",
    )
    output_schema_path = ROOT / "benchmark_configs" / "schemas" / "judge_output.schema.json"
    model_id = "fixture-judge-v1"
    inference_config = {
        "temperature": 0.0,
        "top_p": 1.0,
        "max_output_tokens": 512,
        "response_format": "json_schema",
    }
    runner_source = root / "fake_judge_runner.py"
    runner_source.write_text(
        """import hashlib
import json
import sys

request = json.load(sys.stdin)
content = request["messages"][1]["content"]
atom_id = content["oracle_atom"]["atom_id"]
identity = {
    "atom_id": atom_id,
    "source_evidence_sha256": content["source_evidence_sha256"],
    "source_response_sha256": content["source_response_sha256"],
}
item_id = hashlib.sha256(
    json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
).hexdigest()
verdict = ("satisfied", "violated", "unknown")[int(item_id[:8], 16) % 3]
json.dump(
    {
        "evidence": ["frozen fixture runner observed atom " + atom_id],
        "rationale": "deterministic formal fixture runner",
        "verdict": verdict,
        "confidence": 1.0,
    },
    sys.stdout,
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
)
""",
        encoding="utf-8",
    )
    # The judge loader seals and executes the bound binary without the
    # neighbouring ``pyvenv.cfg``.  Bind the base interpreter for this
    # stdlib-only fixture so the environment measured here is the same one
    # observed after sealing; a venv executable would inherit packages only
    # during this first, path-based probe.
    runner_executable = (
        Path(sys.base_prefix)
        / "bin"
        / "python{}.{}".format(sys.version_info.major, sys.version_info.minor)
    ).resolve()
    if not runner_executable.is_file():
        raise RuntimeError("formal judge fixture requires the base Python executable")
    runner_probe = probe_python_environment(runner_executable)
    runner_environment = root / "judge_runner_environment.json"
    write_json(
        runner_environment,
        {
            "schema_version": SCHEMA_VERSION,
            "decision_status": "frozen",
            "runtime_kind": "python",
            "executable": {
                "path": str(runner_executable),
                "sha256": sha256_file(runner_executable),
                "bytes": runner_executable.stat().st_size,
            },
            "runtime_version": runner_probe["runtime_version"],
            "locked_dependencies": runner_probe["locked_dependencies"],
        },
    )
    paths["judge_runner_manifest"] = root / "judge_runner_manifest.json"
    write_json(
        paths["judge_runner_manifest"],
        {
            "schema_version": SCHEMA_VERSION,
            "decision_status": "confirmed",
            "runner_id": "formal-judge-fixture",
            "runner_version": "0.1",
            "input_contract": "canonical_judge_request_stdin_v0.1",
            "output_contract": "judge_output_stdout_v0.1",
            "execution_order": "item_id_ascending",
            "max_attempts": 1,
            "environment_manifest": {
                "path": str(runner_environment.resolve()),
                "sha256": sha256_file(runner_environment),
                "bytes": runner_environment.stat().st_size,
            },
            "source_files": [
                {
                    "path": str(runner_source.resolve()),
                    "sha256": sha256_file(runner_source),
                    "bytes": runner_source.stat().st_size,
                }
            ],
            "command": {
                "argv": [str(runner_executable), str(runner_source.resolve())],
                "timeout_seconds": 30.0,
                "max_request_bytes": 16777216,
                "max_stdout_bytes": 1048576,
                "max_stderr_bytes": 1048576,
                "credential_env_names": [],
            },
        },
    )
    runner_manifest_sha256 = sha256_file(paths["judge_runner_manifest"])
    config_sha256 = judge_config_sha256(
        model_id=model_id,
        prompt_sha256=sha256_file(prompt),
        inference_config=inference_config,
        output_schema_sha256=sha256_file(output_schema_path),
        request_contract="judge_atom_v0.1",
        runner_manifest_sha256=runner_manifest_sha256,
    )
    calibration_rows = []
    for roster_item in calibration_roster["items"]:
        verdict = ("satisfied", "violated", "unknown")[
            int(roster_item["item_id"][:8], 16) % 3
        ]
        gold = {
            "schema_version": SCHEMA_VERSION,
            "workflow_type": "judge_calibration_human_gold",
            **copy.deepcopy(roster_item),
            "reviewer_id": "reviewer",
            "gold_verdict": verdict,
            "gold_evidence": ["formal synthetic human-gold evidence"],
            "gold_rationale": "formal contract fixture only",
            "review_status": "complete",
            "canonical_source_revalidated": True,
            "reviewer_blind_to_judge_prediction": True,
            "decision_status": "confirmed",
        }
        gold["gold_record_id"] = sha256_bytes(canonical_json_bytes(gold))
        calibration_rows.append(gold)
    calibration = root / "judge_calibration.jsonl"
    write_jsonl(calibration, calibration_rows)
    paths["judge_calibration_context_manifest"] = (
        root / "judge_calibration_context_manifest.json"
    )
    calibration_context = build_judge_calibration_context_manifest(
        query_library_path=DEV_LIBRARY,
        requirement_oracle_path=paths["requirement_oracle_dev"],
        calibration_roster_path=paths["judge_calibration_roster"],
        calibration_human_gold_path=calibration,
        prompt_path=prompt,
        output_schema_path=output_schema_path,
        runner_manifest_path=paths["judge_runner_manifest"],
        development_sources=[
            {
                "platform": source["platform"],
                "source_config": source["source_config_path"],
                "query_roster": source["roster_path"],
                "response_records": source["response_path"],
                "evidence_records": source["evidence_path"],
            }
            for source in development_sources
        ],
        model_id=model_id,
        inference_config=inference_config,
    )
    write_json(paths["judge_calibration_context_manifest"], calibration_context)
    calibration_request_directory = root / "judge_calibration_requests"
    create_judge_calibration_request_bundle(
        paths["judge_calibration_context_manifest"], calibration_request_directory
    )
    paths["judge_calibration_request_bundle_manifest"] = (
        calibration_request_directory / "request_manifest.json"
    )
    calibration_run_directory = root / "judge_calibration_run"
    run_judge_calibration_request_bundle(
        calibration_request_directory,
        paths["judge_calibration_context_manifest"],
        calibration_run_directory,
    )
    paths["judge_calibration_run_manifest"] = (
        calibration_run_directory / "judge_run_manifest.json"
    )
    _, raw_judge_records = validate_judge_calibration_run_bundle(
        calibration_run_directory,
        paths["judge_calibration_context_manifest"],
        request_directory=calibration_request_directory,
    )
    predictions_by_item = {
        record["item_id"]: parse_raw_judge_response(record["raw_response"])[
            "verdict"
        ]
        for record in raw_judge_records
    }
    calibration_metrics = compute_judge_calibration(
        calibration_rows, predictions_by_item
    )
    paths["judge_rubric"] = root / "judge.json"
    write_json(
        paths["judge_rubric"],
        {
            "decision_status": "confirmed",
            "request_contract": "judge_atom_v0.1",
            "model_id": model_id,
            "inference_config": inference_config,
            "prompt": {
                "path": str(prompt),
                "sha256": sha256_file(prompt),
                "bytes": prompt.stat().st_size,
            },
            "output_schema": {
                "path": str(output_schema_path),
                "sha256": sha256_file(output_schema_path),
                "bytes": output_schema_path.stat().st_size,
            },
            "runner_manifest_sha256": runner_manifest_sha256,
            "judge_config_sha256": config_sha256,
            "calibration": {
                "path": str(calibration),
                "sha256": sha256_file(calibration),
                "bytes": calibration.stat().st_size,
            },
            "calibration_roster": {
                "path": str(paths["judge_calibration_roster"]),
                "sha256": sha256_file(paths["judge_calibration_roster"]),
                "bytes": paths["judge_calibration_roster"].stat().st_size,
            },
            "calibration_context": {
                "path": str(paths["judge_calibration_context_manifest"]),
                "sha256": sha256_file(paths["judge_calibration_context_manifest"]),
                "bytes": paths["judge_calibration_context_manifest"].stat().st_size,
            },
            "calibration_request_manifest": {
                "path": str(paths["judge_calibration_request_bundle_manifest"]),
                "sha256": sha256_file(
                    paths["judge_calibration_request_bundle_manifest"]
                ),
                "bytes": paths[
                    "judge_calibration_request_bundle_manifest"
                ].stat().st_size,
            },
            "calibration_run_manifest": {
                "path": str(paths["judge_calibration_run_manifest"]),
                "sha256": sha256_file(paths["judge_calibration_run_manifest"]),
                "bytes": paths["judge_calibration_run_manifest"].stat().st_size,
            },
            "calibration_record_count": calibration_metrics["record_count"],
            "macro_f1": calibration_metrics["macro_f1"],
            "cohen_kappa": calibration_metrics["cohen_kappa"],
            "gold_support_by_verdict": calibration_metrics[
                "gold_support_by_verdict"
            ],
            "platform_metrics": {
                platform: {
                    "record_count": calibration_metrics["by_platform"][platform][
                        "record_count"
                    ],
                    "macro_f1": calibration_metrics["by_platform"][platform][
                        "macro_f1"
                    ],
                    "cohen_kappa": calibration_metrics["by_platform"][platform][
                        "cohen_kappa"
                    ],
                    "gold_support_by_verdict": calibration_metrics["by_platform"][
                        platform
                    ]["gold_support_by_verdict"],
                }
                for platform in ("carla", "metadrive")
            },
        },
    )

    uqh_config = root / "uqh_assessment_config.json"
    write_json(
        uqh_config,
        {
            "protocol_version": "0.1",
            "eligible_dispositions": ["reject", "clarification"],
            "require_exact_oracle_reason_coverage": True,
            "controlled_degradation_credit": False,
        },
    )
    uqh_source = root / "uqh_assessor_source.txt"
    uqh_source.write_text(
        "Inspect only bound raw method bytes; cite byte ranges before returning a verdict.\n",
        encoding="utf-8",
    )
    uqh_public_key = root / "uqh_assessor_public_key.pem"
    uqh_public_key.write_text(
        """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAlD6oZyJpvRzMnnouUHwi
nvYSu7gpdIaAtqnvME024PMsrkT+KYOgP1ItTZ8VotiGYQoEKIXWH1GradshSawt
HMvVkrEhxJxMHPbik2BaJeBndxG9DfQl1jBtVDZHuVPhSGnZ4Nv+f85+SbLwqOPj
jlQYYmjRDCWL0v44R8s1fxm+21dvxJGPErm6j9BtFAEqFvMzki/zpKeASvsEKOXs
BHIgllDVXQ7Pipf6JjcxyC1ES+B387SPPu9QQzmO5rBoCr1yOHu3GgwuZEjW9ZXQ
5CuRXFIHAKYERExXbiXcBQfy3Ny65S+vH84+cyaj+AgvP8yzXVRVIazzWSDSvFA6
QwIDAQAB
-----END PUBLIC KEY-----
""",
        encoding="utf-8",
    )
    paths["uqh_assessor_registry"] = root / "uqh_assessor_registry.json"
    write_json(
        paths["uqh_assessor_registry"],
        {
            "schema_version": SCHEMA_VERSION,
            "status": "frozen",
            "protocol_version": "0.1",
            "assessment_binding_contract": "uqh_assessment_binding_v0.1",
            "controlled_degradation_credit": False,
            "assessors": [
                {
                    "assessor_id": "formal-independent-uqh-judge",
                    "assessor_version": "0.1",
                    "assessment_source": "judge",
                    "independent_from_producer_ids": ["immutable-generation-runner"],
                    "assessment_config": {
                        "path": str(uqh_config),
                        "sha256": sha256_file(uqh_config),
                        "bytes": uqh_config.stat().st_size,
                    },
                    "assessor_source": {
                        "path": str(uqh_source),
                        "sha256": sha256_file(uqh_source),
                        "bytes": uqh_source.stat().st_size,
                    },
                    "attestation_algorithm": "rsa_pss_sha256",
                    "attestation_public_key": {
                        "path": str(uqh_public_key),
                        "sha256": sha256_file(uqh_public_key),
                        "bytes": uqh_public_key.stat().st_size,
                    },
                    "key_custody": "external_to_method_runner",
                }
            ],
        },
    )

    paths["ego_proxy_config"] = root / "ego.json"
    write_json(
        paths["ego_proxy_config"],
        {
            "status": "frozen",
            "length_m": 5.33,
            "width_m": 2.1,
            "carla_blueprint": "vehicle.chevrolet.impala",
            "metadrive_vehicle_model": "xl",
        },
    )

    roster = build_roster(
        read_jsonl(TEST_LIBRARY),
        TEST_LIBRARY,
        "method-a",
        "carla",
        clusters,
    )
    paths["query_roster"] = root / "roster.json"
    write_json(paths["query_roster"], roster)
    paths["method_registry"] = root / "registry.json"
    write_json(
        paths["method_registry"],
        {
            "status": "frozen",
            "methods": [
                {
                    "method_id": "method-a",
                    "platform": "carla",
                    "config_sha256": sha256_file(paths["method_config"]),
                    "roster_sha256": sha256_file(paths["query_roster"]),
                }
            ],
        },
    )

    role_aliases = {
        "judge_calibration_source_config_metadrive": "judge_calibration_source_config",
        "development_query_roster_metadrive": "development_query_roster",
        "development_response_records_metadrive": "development_response_records",
        "development_evidence_records_metadrive": "development_evidence_records",
    }
    role_sha256 = {
        "query_library_test": [sha256_file(TEST_LIBRARY)],
        "query_library_dev": [sha256_file(DEV_LIBRARY)],
    }
    for role, path in paths.items():
        frozen_role = role_aliases.get(role, role)
        role_sha256.setdefault(frozen_role, []).append(sha256_file(path))
    protocol_subjects = _expected_protocol_decision_subjects(
        role_sha256,
        read_json(paths["semantic_extractor_manifest"]),
    )
    protocol_decisions = []
    for decision_id, subject in sorted(protocol_subjects.items()):
        label = {
            "verdict": "approve",
            "reason": "synthetic formal-contract approval",
        }
        gold = {
            "decision_id": decision_id,
            "subject": subject,
            "subject_sha256": sha256_bytes(canonical_json_bytes(subject)),
            "reviewer": {**label, "reviewer_id": "reviewer"},
            "review_status": "complete",
            "decision_status": "confirmed",
        }
        gold["gold_record_id"] = sha256_bytes(canonical_json_bytes(gold))
        protocol_decisions.append(gold)
    paths["protocol_decision_review"] = root / "protocol_decision_review.json"
    write_json(
        paths["protocol_decision_review"],
        {
            "schema_version": SCHEMA_VERSION,
            "workflow_type": "protocol_decision_human_gold_collection",
            "review_policy": "one_complete_human_gold_per_subject",
            "review_status": "complete",
            "decision_status": "confirmed",
            "decisions": protocol_decisions,
        },
    )

    assets = [
        {"role": "query_library_test", "path": str(TEST_LIBRARY)},
        {"role": "query_library_dev", "path": str(DEV_LIBRARY)},
    ]
    assets.extend(
        {"role": role_aliases.get(role, role), "path": str(path)}
        for role, path in paths.items()
    )
    return {"assets": assets, "protocol": dict(REQUIRED_PROTOCOL)}, paths
