import copy
import math
import unittest
from pathlib import Path

from bus_benchmark.cpd import project_common_atoms, query_blind_target_selector
from bus_benchmark.fixture_extractor_common import (
    _detect_atoms,
    _detect_common,
    _normalize,
)
from bus_benchmark.jsonio import canonical_json_bytes, read_jsonl, sha256_file
from bus_benchmark.native_observer import _metadrive_track
from bus_benchmark.ontology import (
    build_common_ontology,
    deterministic_representative_arguments,
)
from bus_benchmark.oracle import draft_oracle
from tests.bus_benchmark.formal_freeze_fixture import (
    _fixture_fact_for_concept,
    _metadrive_document,
    _physical_template,
    _projection_from_fixture_fact,
)


_ROOT = Path(__file__).resolve().parents[2]
_METADRIVE_TOKEN_REGISTRY = (
    _ROOT
    / "benchmark_configs"
    / "methods"
    / "metadrive_pg_token_registry_draft.json"
)
_METADRIVE_TOKEN_REGISTRY_BINDING = {
    "path": str(_METADRIVE_TOKEN_REGISTRY.resolve()),
    "sha256": sha256_file(_METADRIVE_TOKEN_REGISTRY),
    "bytes": _METADRIVE_TOKEN_REGISTRY.stat().st_size,
}


def _track(kind, positions, *, ego=False, heading=0.0, length=4.5, width=1.8):
    return {
        "id": kind,
        "kind": kind,
        "is_ego": ego,
        "positions": [list(point) for point in positions],
        "heading_rad": heading,
        "length_m": length,
        "width_m": width,
        "blueprint": "",
        "vehicle_model": "xl" if ego else "",
    }


def _canonical_atoms(values):
    return sorted(canonical_json_bytes(value) for value in values)


def _development_ontology():
    return build_common_ontology(
        draft_oracle(
            read_jsonl(
                Path(__file__).resolve().parents[2]
                / "query_lib"
                / "bus_ego_topdown_2d_dev_query_library_v0_2.jsonl"
            )
        )
    )


def _transform(tracks, *, dx=0.0, dy=0.0, angle=0.0, reverse=False, resample=False):
    result = copy.deepcopy(tracks)
    cosine = math.cos(angle)
    sine = math.sin(angle)
    for track in result:
        points = track["positions"]
        if resample and len(points) > 1:
            expanded = [points[0]]
            for left, right in zip(points, points[1:]):
                expanded.append(
                    [(left[0] + right[0]) / 2.0, (left[1] + right[1]) / 2.0]
                )
                expanded.append(right)
            points = expanded
        track["positions"] = [
            [
                cosine * point[0] - sine * point[1] + dx,
                sine * point[0] + cosine * point[1] + dy,
            ]
            for point in points
        ]
        track["heading_rad"] += angle
    return list(reversed(result)) if reverse else result


def _normalized_fixture(platform, fact):
    template = _physical_template(fact)
    if platform == "metadrive":
        document = _metadrive_document(
            template,
            "metamorphic",
            _METADRIVE_TOKEN_REGISTRY_BINDING,
        )
        tracks = {
            document["ego"]["actor_id"]: _metadrive_track(
                document["ego"],
                document["ego"]["reference_trajectory"],
                "VEHICLE",
                document["ego"]["vehicle_model"],
            )
        }
        kind_by_type = {
            "vehicle": "VEHICLE",
            "motorcycle": "VEHICLE",
            "e_bike": "CYCLIST",
            "bicycle": "CYCLIST",
            "pedestrian": "PEDESTRIAN",
        }
        for actor in document["actors"]:
            tracks[actor["actor_id"]] = _metadrive_track(
                actor,
                actor["trajectory"],
                kind_by_type[actor["actor_type"]],
            )
        return _normalize(
            "metadrive",
            {
                "sdc_id": document["ego"]["actor_id"],
                "tracks": tracks,
                "map_features": {},
            },
        )
    tracks = []
    for index, value in enumerate(template["tracks"]):
        tracks.append(
            {
                "id": "o{}".format(index),
                "kind": value["kind"],
                "is_ego": value.get("is_ego", False),
                "positions": copy.deepcopy(value["positions"]),
                "heading_rad": 0.0,
                "length_m": value.get("length_m", 0.5),
                "width_m": value.get("width_m", 0.5),
                "blueprint": value.get("blueprint", ""),
                "vehicle_model": value.get("vehicle_model", ""),
            }
        )
    carriers = set(template["feature_carriers"])
    extra_points = []
    if any(value.startswith("has_crosswalk_zone:") for value in carriers):
        extra_points.extend(((10.0, -4.0), (10.0, 4.0)))
        if "has_crosswalk_zone:present" in carriers:
            extra_points.extend((10.0, value) for value in (-2.0, -0.5, 0.5, 2.0))
    if any(value.startswith("has_nonmotor_lane:") for value in carriers):
        extra_points.extend(((15.0, 3.5), (25.0, 3.5)))
        if "has_nonmotor_lane:present" in carriers:
            extra_points.extend(((18.0, 3.5), (22.0, 3.5)))
    for index, point in enumerate(extra_points, start=len(tracks)):
        tracks.append(
            {
                "id": "o{}".format(index),
                "kind": "Cone",
                "is_ego": False,
                "positions": [list(point)],
                "heading_rad": 0.0,
                "length_m": 0.5,
                "width_m": 0.5,
                "blueprint": "",
                "vehicle_model": "",
            }
        )
    return tracks, []


def _transform_scene(
    tracks,
    features,
    *,
    dx=0.0,
    dy=0.0,
    angle=0.0,
    reverse=False,
    resample=False
):
    transformed_tracks = _transform(
        tracks,
        dx=dx,
        dy=dy,
        angle=angle,
        reverse=reverse,
        resample=resample,
    )
    transformed_features = copy.deepcopy(features)
    cosine = math.cos(angle)
    sine = math.sin(angle)
    for feature in transformed_features:
        for field in ("polyline", "polygon"):
            if field not in feature:
                continue
            feature[field] = [
                [
                    cosine * point[0] - sine * point[1] + dx,
                    sine * point[0] + cosine * point[1] + dy,
                ]
                for point in feature[field]
            ]
    if reverse:
        transformed_features.reverse()
    return transformed_tracks, transformed_features


def _scene_variants(tracks, features):
    return (
        _transform_scene(tracks, features, dx=137.0, dy=-41.0),
        _transform_scene(tracks, features, angle=math.pi / 3.0),
        _transform_scene(tracks, features, resample=True),
        _transform_scene(tracks, features, reverse=True),
        _transform_scene(
            tracks,
            features,
            dx=-20.0,
            dy=70.0,
            angle=-math.pi / 4.0,
            reverse=True,
            resample=True,
        ),
    )


class ExtractorMetamorphicTests(unittest.TestCase):
    def test_distance_selector_handles_multiple_actor_types_without_query_context(self):
        for platform, vehicle_kind, cyclist_kind in (
            ("carla", "Car", "Bicycle"),
            ("metadrive", "VEHICLE", "CYCLIST"),
        ):
            tracks = [
                _track(vehicle_kind, [[40.0, 0.0]], length=4.5, width=1.8),
                _track("VEHICLE" if platform == "metadrive" else "Car", [[0.0, 0.0]], ego=True, length=5.33, width=2.1),
                _track(cyclist_kind, [[8.0, 3.5]], length=2.0, width=0.8),
            ]
            atoms = _detect_common(platform, tracks, [])
            distance = {
                atom["target_signature"]["actor_class"]: atom["value"]
                for atom in atoms
                if atom["dimension"] == "actor_longitudinal_distance_bin"
            }
            self.assertEqual(distance, {"motor_vehicle": "far", "cyclist": "near"})

    def test_yield_selector_can_select_a_non_ego_vehicle(self):
        for platform, vehicle_kind in (("carla", "Car"), ("metadrive", "VEHICLE")):
            tracks = [
                _track(
                    vehicle_kind,
                    [[0.0, 0.0], [2.0, 0.0], [4.0, 0.0], [6.0, 0.0]],
                    ego=True,
                    length=5.33,
                    width=2.1,
                ),
                _track(
                    vehicle_kind,
                    [[8.0, 0.0], [7.0, 0.0], [6.5, 0.0], [6.4, 0.0]],
                ),
            ]
            atoms = _detect_common(platform, tracks, [])
            yield_atom = next(
                atom
                for atom in atoms
                if atom["dimension"] == "yield_realization_mode"
                and atom["target_signature"]["actor_class"] == "motor_vehicle"
            )
            self.assertEqual(yield_atom["value"], "decelerate")

    def test_merge_selector_accepts_cyclists_and_multiple_gap_actors(self):
        for platform, vehicle_kind, cyclist_kind in (
            ("carla", "Car", "Bicycle"),
            ("metadrive", "VEHICLE", "CYCLIST"),
        ):
            tracks = [
                _track(
                    vehicle_kind,
                    [[0.0, 0.0], [2.0, 0.0], [4.0, 1.5], [6.0, 3.5]],
                    ego=True,
                    length=5.33,
                    width=2.1,
                ),
                _track(vehicle_kind, [[20.0, -3.5]] * 4),
                _track(cyclist_kind, [[10.0, 3.5]] * 4, length=2.0, width=0.8),
            ]
            atoms = _detect_common(platform, tracks, [])
            merge = {
                atom["target_signature"]["actor_class"]: atom["value"]
                for atom in atoms
                if atom["dimension"] == "merge_gap_relation"
            }
            self.assertEqual(
                merge,
                {
                    "motor_vehicle": "behind_gap_actor",
                    "cyclist": "behind_gap_actor",
                },
            )

    def test_unrelated_actor_class_clutter_cannot_change_policy_target(self):
        cases = (
            (
                "actor_longitudinal_distance_bin",
                ["near", "medium", "far"],
                "cyclist",
                "medium",
                "motor_vehicle",
                "near",
            ),
            (
                "yield_realization_mode",
                ["decelerate", "hold"],
                "ego_bus",
                "hold",
                "motor_vehicle",
                "decelerate",
            ),
            (
                "merge_gap_relation",
                ["ahead_of_gap_actor", "behind_gap_actor"],
                "cyclist",
                "ahead_of_gap_actor",
                "motor_vehicle",
                "behind_gap_actor",
            ),
        )
        for dimension, allowed, target_class, target_value, clutter_class, clutter_value in cases:
            definition = {
                dimension: {
                    "allowed_values": allowed,
                    "cardinality": "exactly_one",
                    "target_selector": query_blind_target_selector(
                        dimension, target_class
                    ),
                }
            }
            target = {
                "dimension": dimension,
                "value": target_value,
                "target_signature": {"actor_class": target_class},
            }
            clutter = {
                "dimension": dimension,
                "value": clutter_value,
                "target_signature": {"actor_class": clutter_class},
            }
            self.assertEqual(
                project_common_atoms([target], definition),
                project_common_atoms([clutter, target], definition),
            )

    def test_common_candidates_never_expose_platform_local_track_ids(self):
        tracks = [
            _track(
                "VEHICLE",
                [[0.0, 0.0], [2.0, 0.0], [4.0, 1.5], [6.0, 3.5]],
                ego=True,
                length=5.33,
                width=2.1,
            ),
            _track("CYCLIST", [[10.0, 3.5]] * 4, length=2.0, width=0.8),
        ]
        atoms = _detect_common("metadrive", tracks, [])
        self.assertTrue(atoms)
        self.assertTrue(all(set(atom) <= {"dimension", "value", "target_signature"} for atom in atoms))

    def test_every_deterministic_requirement_representative_is_pose_invariant(self):
        ontology = _development_ontology()
        tested = 0
        expected_count = 0
        for concept in ontology["concepts"]:
            if concept["kind"] != "requirement_atom_domain":
                continue
            definition = concept["definition"]
            category = definition["category"]
            predicate = definition["predicate"]
            polarity = definition["polarity"]
            values = deterministic_representative_arguments(
                category, predicate, polarity, definition["argument_domain"]
            )
            if not values:
                continue
            expected_count += 1
            fact = _fixture_fact_for_concept(concept)
            expected = _canonical_atoms(
                _projection_from_fixture_fact(fact)["atoms"]
            )
            for platform in ("carla", "metadrive"):
                tracks, features = _normalized_fixture(platform, fact)
                baseline = _canonical_atoms(
                    _detect_atoms(platform, tracks, features)
                )
                self.assertEqual(baseline, expected, (platform, predicate))
                for variant_tracks, variant_features in _scene_variants(
                    tracks, features
                ):
                    self.assertEqual(
                        _canonical_atoms(
                            _detect_atoms(
                                platform, variant_tracks, variant_features
                            )
                        ),
                        baseline,
                        (platform, predicate),
                    )
            tested += 1
        self.assertEqual(tested, expected_count)
        self.assertGreater(tested, 0)

    def test_adjacent_vehicle_does_not_self_certify_a_semantic_actor_role(self):
        concept = next(
            value
            for value in _development_ontology()["concepts"]
            if value["concept_id"] == "atom:actor:actor_role_count:present"
        )
        fact = _fixture_fact_for_concept(concept)
        for platform in ("carla", "metadrive"):
            tracks, features = _normalized_fixture(platform, fact)
            self.assertFalse(
                any(
                    atom["predicate"] == "actor_role_count"
                    for atom in _detect_atoms(platform, tracks, features)
                )
            )

    def test_static_ego_crossing_confounder_is_not_a_before_event(self):
        tracks = [
            _track("Car", [[0.0, 0.0]] * 4, ego=True, length=5.33, width=2.1),
            _track(
                "Pedestrian",
                [[8.0, 4.0], [8.0, 0.5], [8.0, 0.0], [8.0, -1.0]],
                length=0.5,
                width=0.5,
            ),
        ]
        self.assertFalse(
            any(atom["predicate"] == "before" for atom in _detect_atoms("carla", tracks, []))
        )

    def test_every_cpd_value_is_pose_invariant(self):
        ontology = build_common_ontology(
            draft_oracle(
                read_jsonl(
                    Path(__file__).resolve().parents[2]
                    / "query_lib"
                    / "bus_ego_topdown_2d_dev_query_library_v0_2.jsonl"
                )
            )
        )
        tested = 0
        expected_count = 0
        for concept in ontology["concepts"]:
            if concept["kind"] != "cpd_dimension":
                continue
            dimension = concept["definition"]
            variants = dimension["variants"]
            allowed_values = {
                canonical_json_bytes(value): value
                for variant in variants
                for value in variant["allowed_values"]
            }
            expected_count += len(allowed_values)
            for encoded in sorted(allowed_values):
                value = allowed_values[encoded]
                fact = {
                    "kind": "cpd",
                    "dimension": dimension["name"],
                    "value": value,
                    "present": True,
                    "ego_variant": "approved",
                }
                expected = _canonical_atoms(
                    _projection_from_fixture_fact(fact)["common_atoms"]
                )
                for platform in ("carla", "metadrive"):
                    tracks, features = _normalized_fixture(platform, fact)
                    baseline = _canonical_atoms(
                        _detect_common(platform, tracks, features)
                    )
                    self.assertEqual(
                        baseline, expected, (platform, dimension["name"], value)
                    )
                    for variant_tracks, variant_features in _scene_variants(
                        tracks, features
                    ):
                        self.assertEqual(
                            _canonical_atoms(
                                _detect_common(
                                    platform, variant_tracks, variant_features
                                )
                            ),
                            baseline,
                            (platform, dimension["name"], value),
                        )
                tested += 1
        self.assertEqual(tested, expected_count)
        self.assertGreater(tested, 0)

    def test_requirement_atoms_ignore_global_pose_sampling_and_entity_order(self):
        tracks = [
            _track(
                "VEHICLE",
                [[0.0, 0.0], [2.0, 0.0], [4.0, 2.5], [6.0, 0.0]],
                ego=True,
                length=5.33,
                width=2.1,
            ),
            _track("CYCLIST", [[5.0, 3.5]], length=2.0, width=1.0),
        ]
        baseline = _canonical_atoms(_detect_atoms("metadrive", tracks, []))
        self.assertTrue(baseline)
        variants = (
            _transform(tracks, dx=137.0, dy=-41.0),
            _transform(tracks, angle=math.pi / 3.0),
            _transform(tracks, resample=True),
            _transform(tracks, reverse=True),
            _transform(
                tracks,
                dx=-20.0,
                dy=70.0,
                angle=-math.pi / 4.0,
                reverse=True,
                resample=True,
            ),
        )
        for variant in variants:
            self.assertEqual(
                _canonical_atoms(_detect_atoms("metadrive", variant, [])), baseline
            )

    def test_common_dimensions_ignore_global_pose_sampling_and_entity_order(self):
        tracks = [
            _track(
                "VEHICLE",
                [[0.0, 0.0], [4.0, 0.0], [6.0, 0.0], [7.0, 0.0]],
                ego=True,
                length=5.33,
                width=2.1,
            ),
            _track("Motorcycle", [[20.0, 6.0]], length=2.0, width=1.0),
            _track(
                "PEDESTRIAN",
                [[6.0, 3.0], [6.0, 1.0], [6.0, 0.0], [6.0, -1.0]],
                length=0.5,
                width=0.5,
            ),
        ]
        baseline = _canonical_atoms(_detect_common("metadrive", tracks, []))
        self.assertEqual(len(baseline), 4)
        variants = (
            _transform(tracks, dx=250.0, dy=90.0),
            _transform(tracks, angle=math.pi / 2.0),
            _transform(tracks, resample=True),
            _transform(tracks, reverse=True),
            _transform(
                tracks,
                dx=12.0,
                dy=-32.0,
                angle=math.pi / 6.0,
                reverse=True,
                resample=True,
            ),
        )
        for variant in variants:
            self.assertEqual(
                _canonical_atoms(_detect_common("metadrive", variant, [])), baseline
            )


if __name__ == "__main__":
    unittest.main()
