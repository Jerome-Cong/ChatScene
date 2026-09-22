from __future__ import annotations

import argparse
import copy
import difflib
import hashlib
import json
import re
import shutil
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

QUERY_LIB_DIR = Path(__file__).resolve().parent
DEV_V01_NAME = 'bus_ego_topdown_2d_dev_query_library_v0_1.jsonl'
TEST_V01_NAME = 'bus_ego_topdown_2d_query_library_v0_1.jsonl'
DEV_V02_NAME = 'bus_ego_topdown_2d_dev_query_library_v0_2.jsonl'
TEST_V02_NAME = 'bus_ego_topdown_2d_query_library_v0_2.jsonl'
MAPPING_NAME = 'query_id_mapping_v0_1_to_v0_2.json'
POLICY_MANIFEST_NAME = 'policy_diagnostic_manifest_v0_2.jsonl'
VALIDATION_NAME = 'v0_2_validation_report.json'
OVERLAP_NAME = 'dev_vs_test_overlap_report_v0_2.json'
README_NAME = 'README_v0_2.md'
SCRIPT_NAME = 'build_query_library_v0_2.py'
SHA256SUMS_NAME = 'SHA256SUMS.txt'
EXPECTED_V01_SHA256 = {
    'dev_v0_1': '09b9e29c32315b866aaa082055ef01ea905c48beeccb05e227f1932855a4d2df',
    'test_v0_1': '560bf74371309d24294685f5828a6c4591f3bed0a88717f0df9da80ae5455d52',
}
RELEASE_FILES = (
    DEV_V02_NAME,
    TEST_V02_NAME,
    POLICY_MANIFEST_NAME,
    OVERLAP_NAME,
    MAPPING_NAME,
    VALIDATION_NAME,
    README_NAME,
    SCRIPT_NAME,
)

CREATED_DATE = '2026-07-15'
VIEW_SCOPE = {
    'ego_actor': 'bus',
    'native_simulation_environment': True,
    'semantic_geometry_projection': 'ground_plane',
    'height_features_used_for_method_ranking': False,
    'visual_sensor_input': False,
    'weather_conditioning': False,
    'night_lighting_conditioning': False,
    'high_definition_map_dependency': False,
}
STYLE_FAMILY = {
    'precise': 'constraint_case_specification',
    'partial': 'compact_scenario_brief',
    'vague': 'natural_need_statement_or_fragment',
}


def load_jsonl(path: Path) -> List[Dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def spec(
    phase: str,
    precise: str,
    partial: str,
    vague: str,
    events: List[str],
    temporal: str,
    tags: List[str],
    *,
    scene_rules: List[str] | None = None,
    actor_rules: List[str] | None = None,
    actor_violations: List[str] | None = None,
    risk: str | None = None,
    queue: str | None = None,
    novelty: str | None = None,
    reason: List[str] | None = None,
    field_overrides: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    return {
        'phase': phase,
        'texts': {'precise': precise, 'partial': partial, 'vague': vague},
        'events': events,
        'temporal': temporal,
        'tags': tags,
        'rule_hooks': {
            'scene_rules': scene_rules or [],
            'counterpart_actor_rules': actor_rules or [],
            'counterpart_actor_violations': actor_violations or [],
        },
        'risk': risk,
        'queue': queue,
        'novelty': novelty,
        'reason': reason,
        'field_overrides': field_overrides or {},
    }


DEV: Dict[str, Dict[str, Any]] = {
    'S01': spec(
        'right_turn_through_intersection',
        'At a signalized intersection with a curbside cycle crossing, the ego bus intends to turn right from the right-turn lane. One cyclist is 8 m ahead in the cycle lane and continues straight through the crossing on its permitted phase.',
        'An ego bus intends to turn right at a signalized intersection while a cyclist continues straight through the cycle crossing.',
        'A right-turn bus case with a through cyclist at the cycle crossing.',
        ['cyclist_straight_crossing'], 'single_event',
        ['signalized_intersection', 'right_turn', 'cyclist_through_movement', 'cycle_crossing'],
        scene_rules=['cycle_crossing_priority'], risk='medium',
        novelty='Right-turn and through-cyclist interaction at a signalized cycle crossing; the locked test uses cyclist interactions mainly around bus stops and bus departure paths.'
    ),
    'S02': spec(
        'left_turn_through_intersection',
        'At an unsignalized four-way intersection, the ego bus intends to turn left. One oncoming motorcycle is 25 m away and continues straight at normal speed through the opposing approach.',
        'An ego bus intends to turn left at an unsignalized junction while an oncoming motorcycle continues straight.',
        'A bus-left-turn case with a motorcycle coming the other way.',
        ['oncoming_motorcycle_through'], 'single_event',
        ['unsignalized_intersection', 'left_turn', 'oncoming_motorcycle'],
        scene_rules=['intersection_conflict_zone_definition'], risk='medium',
        novelty='Oncoming-motorcycle interaction during an unsignalized left-turn intent; locked-test motorcycle roles are chiefly rear approach, overtaking, filtering, wrong-way travel, or stop obstruction.'
    ),
    'S03': spec(
        'straight_through_intersection',
        'On a signalized approach, the ego bus intends to continue straight. The signal facing the bus is initially red and later changes to green, while one following car forms a short queue behind the bus during the red phase.',
        'An ego bus approaches a red signal to continue straight, a car queues behind it, and the signal later changes to green.',
        'A red-light bus queue with a later green phase.',
        ['red_signal_phase', 'following_vehicle_queue_formation', 'green_signal_phase'], 'three_stage_chain',
        ['traffic_signal', 'following_vehicle', 'short_queue', 'signal_phase_change'],
        scene_rules=['traffic_signal_sequence_red_then_green'], actor_rules=['following_vehicle_lane_discipline'], risk='low', queue='short_queue',
        novelty='Signal-controlled queue context with a red-to-green phase change; locked-test following-vehicle behavior is tied to bus-stop approach or dwell rather than a traffic-signal cycle.'
    ),
    'S04': spec(
        'straight_through_intersection',
        'At a signalized four-way intersection, the ego bus intends to proceed straight on green. A car from the cross street enters the conflict area against a red signal.',
        'An ego bus proceeds straight on green while a cross-street car enters against red.',
        'A through-bus case with a cross-traffic red-light runner.',
        ['cross_traffic_red_light_violation'], 'single_event',
        ['signalized_intersection', 'cross_traffic', 'red_light_violation', 'high_risk'],
        scene_rules=['signal_phase_conflict_area'], actor_violations=['cross_street_vehicle_red_light_violation'], risk='high',
        novelty='Cross-street red-light violation during the bus straight-through intent; no locked-test core event uses a traffic-signal phase conflict.'
    ),
    'S05': spec(
        'transit_priority_straight_through_intersection',
        'At a signalized intersection with a short queue-jump lane, the ego bus intends to continue straight from the transit lane. The bus receives an early transit signal while two adjacent general-lane cars remain under a red indication.',
        'An ego bus receives an early transit phase while cars in the general lanes remain on red.',
        'A bus-priority signal phase ahead of neighboring cars.',
        ['early_transit_signal_phase', 'general_lane_red_phase', 'adjacent_general_lane_queue_present'], 'parallel_events',
        ['transit_signal_priority', 'queue_jump_lane', 'lane_specific_signal'],
        scene_rules=['transit_signal_priority', 'lane_specific_signal_control'], actor_rules=['general_lane_signal_compliance'], risk='low', queue='short_queue',
        novelty='Lane-specific transit-signal priority; the locked test contains bus-only-lane operation but not an early transit phase relative to adjacent traffic.'
    ),
    'S06': spec(
        'traverse_bottleneck',
        'Road works reduce a two-way road to one alternating lane. The ego bus intends to traverse the narrowed section, while one oncoming car has already entered the bottleneck from the opposite side.',
        'An ego bus intends to traverse a one-lane roadworks bottleneck while an oncoming car is already inside it.',
        'A bus and an oncoming car at a one-lane roadworks bottleneck.',
        ['oncoming_vehicle_inside_bottleneck'], 'single_event',
        ['bottleneck', 'opposing_traffic', 'road_narrowing', 'alternating_priority'],
        scene_rules=['single_lane_alternating_priority', 'occupied_bottleneck'], risk='medium',
        novelty='Alternating-priority roadworks bottleneck, rather than stop blockage, bus-bay exit, or queue bypass.'
    ),
    'S07': spec(
        'straight_cruise',
        'On a midblock segment with a driveway, the ego bus is travelling straight. A car 12 m ahead exits the driveway at low speed and enters the bus lane.',
        'An ego bus travels straight while a car leaves a driveway and enters its lane.',
        'A bus-lane interaction with a car coming out of a driveway.',
        ['vehicle_driveway_egress', 'vehicle_enters_bus_lane'], 'two_stage_chain',
        ['driveway', 'vehicle_egress', 'lane_entry'],
        actor_violations=['driveway_vehicle_enters_without_full_clearance'], risk='medium',
        novelty='Driveway-egress interaction; the nearest locked-test car cut-in occurs inside a bus-stop area and results in a blocked docking path.'
    ),
    'S08': spec(
        'straight_through_intersection',
        'At an unsignalized T-junction, the ego bus intends to continue straight on the priority road. A motorcycle enters from the side road without yielding and reaches the conflict zone.',
        'An ego bus travels on the priority road while a side-road motorcycle enters the T-junction without yielding.',
        'A main-road bus case with a motorcycle entering from a side street.',
        ['motorcycle_side_road_entry', 'motorcycle_yield_violation'], 'two_stage_chain',
        ['t_junction', 'motorcycle', 'priority_violation', 'high_risk'],
        scene_rules=['priority_road_conflict_zone'], actor_violations=['side_road_motorcycle_failure_to_yield'], risk='high',
        novelty='Side-road priority violation at a T-junction; locked-test motorcycle interactions are anchored to stops, queues, passing, or rear approach.'
    ),
    'S09': spec(
        'straight_cruise',
        'On a single shared lane, the ego bus travels behind one cyclist with a marked cycle lane beginning 30 m ahead. The cyclist transitions from the shared lane into that cycle lane.',
        'An ego bus follows a cyclist on a shared lane, and the cyclist later enters a marked cycle lane.',
        'A bus following a cyclist until the bike lane begins.',
        ['cyclist_transitions_to_cycle_lane'], 'single_event',
        ['cyclist_following_context', 'shared_lane', 'cycle_lane_transition'],
        scene_rules=['cycle_lane_entry'], actor_rules=['cyclist_uses_cycle_lane_when_available'], risk='low',
        novelty='A moving lead cyclist transitions from a shared lane into a cycle lane; locked-test cases focus on cyclists passing a stopped bus, crossing, detouring, or swerving.'
    ),
    'S10': spec(
        'straight_cruise',
        'On a divided road with a pedestrian refuge, the ego bus intends to continue straight. One pedestrian reaches the central island, waits there, and later steps from the island into the bus-side carriageway.',
        'An ego bus travels on a divided road while a pedestrian waits on the refuge island and then enters the bus-side carriageway.',
        'A two-stage pedestrian crossing from a central refuge into the bus side of the road.',
        ['pedestrian_reaches_refuge', 'pedestrian_waits_on_median', 'pedestrian_enters_ego_carriageway'], 'three_stage_chain',
        ['pedestrian_refuge', 'two_stage_crossing', 'divided_road'],
        scene_rules=['refuge_island_stage_separation'], actor_rules=['pedestrian_crossing_sequence'], risk='medium',
        novelty='Median-refuge two-stage crossing with delayed entry into the bus-side carriageway; locked-test pedestrian crossings are bus-stop or crosswalk adjacent without this staged topology.'
    ),
    'S11': spec(
        'roundabout_entry',
        'At a single-lane roundabout entry, the ego bus intends to enter the circulating lane. One car approaches in the roundabout from the bus\'s left and passes across the entry conflict area.',
        'An ego bus intends to enter a roundabout while one circulating car approaches from the left.',
        'A bus roundabout-entry case with one circulating car.',
        ['circulating_vehicle_approach', 'circulating_vehicle_passes_entry'], 'two_stage_chain',
        ['roundabout', 'circulating_vehicle', 'entry_conflict'],
        scene_rules=['roundabout_entry_priority'], actor_rules=['circulating_vehicle_lane_following'], risk='low',
        novelty='Roundabout-entry interaction, absent from the locked test\'s bus-stop and adjacent-crossing event inventory.'
    ),
    'S12': spec(
        'straight_through_intersection',
        'At an unsignalized opposed intersection, the ego bus intends to continue straight on the priority road. An opposing bus intends to turn left across its path and remains outside the conflict zone until the through path is clear.',
        'An ego bus goes straight on the priority road while an opposing bus prepares a left turn across its path.',
        'A two-bus junction case with a through bus and an opposing left-turning bus.',
        ['opposing_bus_left_turn_intent', 'opposing_bus_holds_outside_conflict_zone'], 'two_stage_chain',
        ['bus_bus_interaction', 'opposing_left_turn', 'through_priority', 'unsignalized_intersection'],
        scene_rules=['intersection_conflict_zone_clearance'], actor_rules=['opposing_left_turn_bus_yields_to_through_traffic'], risk='low',
        novelty='Opposing buses interact at an intersection; locked-test two-bus cases concern sequential stop service and single-file stop queues.'
    ),
    'U01': spec(
        'straight_through_intersection',
        'Keep the signal facing the ego bus red for the entire scenario, require the bus to cross the stop line and intersection without stopping, and label that crossing fully compliant.',
        'Require the ego bus to cross an intersection on a signal that stays red while calling the crossing compliant.',
        'A fully compliant bus crossing on a light that never leaves red.',
        ['persistent_red_signal', 'full_compliance_label_request'], 'simultaneous_constraint_conflict',
        ['unsupported', 'signal_contradiction', 'persistent_red', 'compliance_conflict'],
        scene_rules=['persistent_red_signal'], risk='not_applicable',
        novelty='Signal-state and compliance-label contradiction; the locked test\'s rule contradiction concerns boarding where no bus stop exists.',
        reason=['persistent_red_conflicts_with_compliant_intersection_crossing', 'rule_state_contradiction']
    ),
    'U02': spec(
        'straight_cruise',
        'The ego bus starts stationary 100 m before a target point and is required to reach that point within 1 s while remaining below 10 km/h and below 1 m/s² acceleration.',
        'Require a stationary bus to cover 100 m in one second while staying below 10 km/h.',
        'A slow bus that must travel 100 m in one second.',
        [], 'not_applicable',
        ['unsupported', 'kinematic_contradiction', 'speed_limit', 'time_constraint'],
        risk='not_applicable',
        novelty='Numerical dynamics contradiction, not used by any locked-test unsupported intent.',
        reason=['distance_time_speed_constraints_infeasible', 'dynamic_constraint_contradiction']
    ),
    'U03': spec(
        'straight_cruise',
        'Use exactly zero non-ego actors, but also require one motorcycle to enter the ego bus lane ahead of the bus.',
        'Require no non-ego actors and, at the same time, a motorcycle entering the bus lane.',
        'An empty-road bus case that still requires a motorcycle in its lane.',
        ['zero_non_ego_actor_constraint', 'motorcycle_required_in_bus_lane'], 'simultaneous_constraint_conflict',
        ['unsupported', 'actor_cardinality', 'motorcycle', 'event_binding_contradiction'],
        risk='not_applicable',
        novelty='Actor-cardinality versus required-event contradiction, absent from the locked test\'s scope, topology, and rule-conflict cases.',
        reason=['required_interaction_actor_forbidden_by_cardinality', 'actor_event_binding_contradiction'],
        field_overrides={'counterpart_actors': [{'type': 'motorcycle', 'count': 0, 'role': 'forbidden_by_zero_actor_constraint_but_required_in_bus_lane'}]}
    ),
    'U04': spec(
        'left_turn_through_intersection',
        'The ego bus is required to have zero position change and zero speed at every timestep while also completing a left turn through an intersection before the scenario ends.',
        'Require the bus to remain stationary throughout while also completing a left turn.',
        'A bus that never moves but must finish a left turn.',
        [], 'not_applicable',
        ['unsupported', 'temporal_contradiction', 'stationary', 'left_turn'],
        risk='not_applicable',
        novelty='Persistent-state versus terminal-motion contradiction, absent from the locked test.',
        reason=['persistent_stationary_state_conflicts_with_completed_turn', 'temporal_state_contradiction']
    ),
}

TEST: Dict[str, Dict[str, Any]] = {
    'A01': spec(
        'approach_stop',
        'The ego bus approaches an inline bus stop and begins its planned deceleration. One car follows in the same lane and also decelerates behind the bus.',
        'An ego bus slows while approaching an inline stop, and a following car slows behind it.',
        'A simple bus-arrival case with a car following behind.',
        ['following_vehicle_deceleration'], 'single_event',
        ['bus_approach', 'following_vehicle', 'inline_bus_stop'],
        actor_rules=['following_vehicle_lane_discipline'], risk='low'
    ),
    'A02': spec(
        'docking',
        'The ego bus approaches a bus-bay stop from the outer motor lane and intends to shift laterally into the bay while reducing speed.',
        'An ego bus approaches a bus bay and intends to move from the outer lane into the bay.',
        'A bus-bay arrival and docking case.',
        [], 'not_applicable',
        ['bus_docking', 'bay_bus_stop'], scene_rules=['bus_bay_available'], risk='low'
    ),
    'A03': spec(
        'dwell_at_stop',
        'The ego bus is fully stopped at an inline bus stop, with passengers beginning to board and alight in the designated stop area.',
        'An ego bus is stopped at an inline stop while passengers board and alight.',
        'A normal bus-stop service case with passengers getting on and off.',
        ['boarding_alighting'], 'single_event',
        ['boarding_alighting', 'inline_bus_stop', 'normal_operation'],
        actor_rules=['passengers_alight_before_boarding', 'passengers_wait_in_stop_area'], risk='normal'
    ),
    'A04': spec(
        'dwell_at_stop',
        'The ego bus is fully stopped inside a bus bay, with passengers positioned at the designated door-side service area for boarding and alighting.',
        'An ego bus is fully stopped in a bus bay with passengers at the door-side service area.',
        'A bus-bay dwell case with passengers ready for service.',
        ['passengers_present_at_door_side_service_area'], 'single_event',
        ['bus_bay', 'door_side_service_area', 'boarding_alighting_context'],
        actor_rules=['passengers_use_designated_service_area'], risk='normal'
    ),
    'A05': spec(
        'departure_merge',
        'The ego bus is stopped at an inline bus stop and intends to merge into the adjacent motor lane. One car approaches from 15 m behind in the target lane and decelerates to yield space for the merge.',
        'An ego bus intends to leave an inline stop while a rear target-lane car slows and yields.',
        'A low-risk bus pull-out with a car behind giving space.',
        ['rear_vehicle_decelerates_and_yields'], 'single_event',
        ['bus_departure', 'merge', 'rear_vehicle_yield', 'inline_bus_stop'],
        actor_rules=['target_lane_vehicle_cooperative_yield'], risk='low'
    ),
    'A06': spec(
        'departure_merge',
        'The ego bus intends to depart from an inline bus stop and enter the adjacent lane. A rear vehicle in the target lane continues toward the merge area and yields too late.',
        'An ego bus intends to leave an inline stop while the rear target-lane vehicle yields late.',
        'A risky bus pull-out with a late-yielding car behind.',
        ['rear_vehicle_late_yield'], 'single_event',
        ['bus_departure', 'merge', 'late_yield', 'high_exposure'], risk='medium'
    ),
    'A07': spec(
        'departure_merge',
        'The ego bus intends to exit a bus bay and merge into the main traffic lane. One vehicle in the target lane decelerates and yields space.',
        'An ego bus intends to leave a bus bay while a target-lane car yields.',
        'A bus-bay exit with a nearby car giving space.',
        ['target_lane_vehicle_yield'], 'single_event',
        ['bus_bay', 'bus_departure', 'target_lane_vehicle_yield'],
        actor_rules=['target_lane_vehicle_cooperative_yield'], risk='low'
    ),
    'A08': spec(
        'departure_merge',
        'The ego bus is ready to leave a bus bay and intends to merge into the target lane. A continuous stream of vehicles occupies the target lane with no acceptable merge gap.',
        'An ego bus intends to leave a bus bay, but continuous target-lane traffic provides no merge gap.',
        'A bus-bay departure case with traffic continuously filling the target lane.',
        ['continuous_target_lane_traffic', 'no_acceptable_merge_gap'], 'two_stage_chain',
        ['bus_bay', 'dense_traffic', 'gap_availability'], risk='low', queue='none'
    ),
    'A09': spec(
        'approach_stop',
        'The ego bus approaches an inline bus stop where another bus is already stopped and occupies the only docking position.',
        'An ego bus approaches a stop whose only docking position is occupied by another bus.',
        'A bus-stop approach with another bus already at the stop.',
        ['preceding_bus_occupies_stop'], 'single_event',
        ['bus_queue_context', 'single_file', 'inline_bus_stop'],
        scene_rules=['single_file_curb_stop_operation'], risk='low', queue='short_queue'
    ),
    'A10': spec(
        'approach_stop',
        'The ego bus approaches a stop as the second bus in sequence. A first bus occupies the single docking position and later departs.',
        'An ego bus approaches a stop behind another bus that occupies the berth and will depart first.',
        'A two-bus stop-service sequence with the ego bus second.',
        ['preceding_bus_service', 'preceding_bus_departure'], 'two_stage_chain',
        ['two_buses', 'sequential_service', 'single_docking_position'],
        scene_rules=['single_file_bus_stop_service'], risk='low', queue='short_queue'
    ),
    'A11': spec(
        'dwell_at_stop',
        'The ego bus is serving a stop within a dedicated bus lane. Regular vehicles travel in the adjacent general lanes and remain outside the bus-only space.',
        'An ego bus serves a stop in a dedicated bus lane while regular vehicles stay in the general lanes.',
        'A bus-only-lane stop with regular cars kept outside the lane.',
        ['general_vehicles_remain_outside_bus_lane'], 'single_event',
        ['bus_only_lane', 'dedicated_lane', 'bus_stop'],
        scene_rules=['dedicated_bus_lane'], actor_rules=['general_vehicle_bus_lane_exclusion'], risk='low'
    ),
    'A12': spec(
        'pass_through_stop_area',
        'The ego bus intends to pass through a bus-stop area without serving the stop, while regular vehicles continue normally in the adjacent traffic lane.',
        'An ego bus passes a bus-stop area without docking while nearby traffic continues normally.',
        'A bus passing a stop without stopping.',
        ['regular_vehicle_flow_continues'], 'single_event',
        ['pass_through_stop_area', 'regular_traffic_flow'], actor_rules=['general_vehicle_lane_discipline'], risk='normal'
    ),
    'B01': spec(
        'approach_stop',
        'The ego bus approaches an inline bus stop whose 30 m no-parking clearance zone is occupied by a parked car in the outer motor lane.',
        'An ego bus approaches a stop where a parked car occupies the bus-stop clearance zone.',
        'A bus-stop approach with a parked car in the way.',
        ['illegal_parking_in_bus_stop_clearance_zone'], 'single_event',
        ['illegal_parking', 'bus_stop_clearance_zone', 'blocked_stop'],
        scene_rules=['bus_stop_no_parking_clearance_zone'], actor_violations=['vehicle_parking_in_bus_stop_clearance_zone'], risk='medium'
    ),
    'B02': spec(
        'docking',
        'The ego bus intends to enter a bus-bay stop, while an illegally parked car occupies the bay and blocks the docking path.',
        'An ego bus approaches a bus bay that is occupied by an illegally parked car.',
        'A bus-bay docking case with a parked car blocking the bay.',
        ['illegal_parking_in_bus_bay'], 'single_event',
        ['bus_bay', 'illegal_parking', 'blocked_docking_path'],
        scene_rules=['bus_stop_no_parking_clearance_zone'], actor_violations=['vehicle_parking_in_bus_bay'], risk='medium'
    ),
    'B03': spec(
        'approach_stop',
        'The ego bus approaches an inline stop whose designated berth is occupied by a temporarily parked car. A group of passengers waits at an adjacent curb location outside the designated stop area.',
        'An ego bus approaches a blocked inline stop while passengers wait at a nearby non-designated curb point.',
        'A blocked bus stop with passengers waiting at the wrong curb location.',
        ['temporary_vehicle_occupies_stop_berth', 'passengers_wait_at_nondesignated_curb'], 'parallel_events',
        ['blocked_stop', 'nondesignated_boarding_location', 'passenger_waiting'],
        scene_rules=['designated_bus_stop_service_area', 'bus_stop_no_parking_clearance_zone'], actor_violations=['temporary_vehicle_stop_in_bus_berth'], risk='medium',
        field_overrides={'counterpart_actors': [
            {'type': 'social_vehicle', 'count': 1, 'role': 'temporarily_parked_obstacle_vehicle'},
            {'type': 'passenger', 'count': 'multiple', 'role': 'waiting_at_nondesignated_curb'}
        ]}
    ),
    'B04': spec(
        'departure_merge',
        'The ego bus intends to depart from a stop beside a non-motor lane. A delivery vehicle blocks that lane, and an e-bike rider detours into the adjacent motor lane along the bus departure path.',
        'An ego bus prepares to depart while a blocked non-motor lane sends an e-bike into the motor lane.',
        'A bus departure case with an e-bike detouring around a delivery vehicle.',
        ['delivery_vehicle_blocks_nonmotor_lane', 'e_bike_detour_into_motor_lane'], 'two_stage_chain',
        ['nonmotor_detour', 'delivery_vehicle', 'departure_path_interaction'],
        scene_rules=['nonmotor_lane_present'], actor_violations=['delivery_vehicle_blocks_nonmotor_lane'], risk='medium'
    ),
    'B05': spec(
        'dwell_at_stop',
        'The ego bus is stopped at a bus stop while improperly parked bicycles and e-bikes obstruct the designated passenger boarding area.',
        'An ego bus is stopped at a stop where parked bicycles obstruct the boarding area.',
        'A bus-stop dwell case with parked bikes blocking passenger space.',
        ['nonmotor_parking_obstructs_boarding_area'], 'single_event',
        ['boarding_area_obstruction', 'nonmotor_parking', 'bus_dwell'],
        scene_rules=['boarding_area_clearance'], actor_violations=['nonmotor_parking_in_boarding_area'], risk='low'
    ),
    'B06': spec(
        'approach_stop',
        'The ego bus is approaching its stopping position, while one passenger steps toward the door-side area before the bus has fully stopped.',
        'An ego bus approaches its stop as a passenger moves toward the door area too early.',
        'A passenger approaching the bus door before the bus is fully stopped.',
        ['passenger_early_boarding_approach'], 'single_event',
        ['passenger_early_approach', 'door_side_area', 'approach_stop'],
        actor_violations=['passenger_enters_door_area_before_full_stop'], risk='low'
    ),
    'B07': spec(
        'dwell_at_stop',
        'The ego bus is stopped at the bus stop and is ready for passenger service. An e-bike passes through the door-side movement zone beside the bus.',
        'An ego bus is stopped for service while an e-bike passes beside the door area.',
        'A bus-door-side interaction with a passing e-bike.',
        ['e_bike_passes_door_side_zone'], 'single_event',
        ['door_side_zone', 'e_bike', 'bus_dwell'], scene_rules=['door_side_service_zone'], risk='medium'
    ),
    'B08': spec(
        'dwell_at_stop',
        'The ego bus is stopped at a curb location where no designated bus stop is present, while passengers are waiting beside the bus to board or alight.',
        'An ego bus is stopped for passenger service at a curb location outside any designated stop.',
        'A bus pickup case at the wrong curb location.',
        ['passengers_present_at_nondesignated_curb'], 'single_event',
        ['out_of_stop_service', 'nondesignated_curb', 'violation_context'],
        scene_rules=['designated_bus_stop_required_for_service'], risk='medium'
    ),
    'B09': spec(
        'dwell_at_stop',
        'The ego bus is stopped with a vehicle queue behind it. One car leaves the queue and enters the adjacent non-motor lane to bypass the bus.',
        'A car uses the non-motor lane to bypass a queue behind a stopped ego bus.',
        'A bus queue with one car cutting around through the bike lane.',
        ['vehicle_queue_present', 'car_illegal_nonmotor_lane_bypass'], 'two_stage_chain',
        ['queue', 'illegal_bypass', 'nonmotor_lane'],
        scene_rules=['restricted_nonmotor_lane'], actor_violations=['queue_bypass_via_nonmotor_lane'], risk='medium', queue='queue'
    ),
    'B10': spec(
        'dwell_at_stop',
        'The ego bus is stopped at a bus stop close to a crosswalk. A car approaches from behind and overtakes the bus through the adjacent lane near the crossing area.',
        'A car overtakes a stopped ego bus near a crosswalk.',
        'A risky car pass beside a stopped bus near a crosswalk.',
        ['car_overtakes_stopped_bus_near_crosswalk'], 'single_event',
        ['crosswalk', 'overtake', 'high_risk'],
        scene_rules=['overtaking_restricted_near_crosswalk'], actor_violations=['vehicle_overtakes_near_crosswalk'], risk='high'
    ),
    'B11': spec(
        'docking',
        'The ego bus intends to dock at a stop while a car is temporarily stopped within the 30 m bus-stop clearance zone and occupies the normal approach path.',
        'An ego bus approaches a stop where a temporarily stopped car occupies the clearance zone.',
        'A bus docking case with a car stopped too close to the stop.',
        ['temporary_vehicle_stop_in_bus_stop_clearance_zone'], 'single_event',
        ['bus_stop_clearance_zone', 'temporary_stop', 'docking_path'],
        scene_rules=['bus_stop_no_parking_clearance_zone'], actor_violations=['temporary_vehicle_stop_in_bus_stop_clearance_zone'], risk='medium'
    ),
    'B12': spec(
        'approach_stop',
        'The ego bus approaches a bus stop where a motorcycle is stationary inside the designated stopping area.',
        'An ego bus approaches a stop that is occupied by a stationary motorcycle.',
        'A bus-stop approach with a motorcycle in the stopping area.',
        ['motorcycle_stopped_in_bus_stop_area'], 'single_event',
        ['motorcycle', 'blocked_stop', 'stop_area_occupancy'],
        scene_rules=['bus_stop_no_parking_clearance_zone'], actor_violations=['motorcycle_stops_in_bus_stop_area'], risk='medium'
    ),
    'C01': spec(
        'dwell_at_stop',
        'The ego bus is fully stopped at a designated stop. Passengers alight before others board, and nearby pedestrians remain within the sidewalk or stop area.',
        'Passengers alight and board at a fully stopped ego bus while pedestrians stay in the stop area.',
        'An orderly passenger-service case at a stopped bus.',
        ['passengers_alight_then_board', 'pedestrians_remain_in_stop_area'], 'two_stage_chain',
        ['boarding_alighting', 'pedestrian', 'compliant_passenger_behavior'],
        actor_rules=['passengers_alight_before_boarding', 'pedestrians_use_sidewalk_or_stop_area'], risk='normal'
    ),
    'C02': spec(
        'dwell_at_stop',
        'The ego bus is fully stopped at a bus stop, and a line of waiting passengers is positioned in the designated boarding area beside the doors.',
        'An ego bus is fully stopped while passengers queue in the boarding area.',
        'A bus-stop scene with passengers waiting in line.',
        ['passenger_queue_at_stop'], 'single_event',
        ['passenger_queue', 'boarding_area', 'bus_stop'], actor_rules=['passengers_wait_in_designated_stop_area'], risk='normal'
    ),
    'C03': spec(
        'pass_through_stop_area',
        'The ego bus intends to continue along the road without serving an out-of-stop pickup. One passenger stands in the motor lane and attempts to hail the bus.',
        'An ego bus passes through while a passenger tries to hail it from the motor lane.',
        'A passenger waving at the bus from the roadway.',
        ['passenger_hails_from_motor_lane'], 'single_event',
        ['passenger_hailing', 'motor_lane', 'violation_context'], actor_violations=['passenger_hails_from_motor_lane'], risk='medium'
    ),
    'C04': spec(
        'approach_stop',
        'The ego bus approaches its stopping position, while a passenger moves toward the door-side area before the bus reaches a full stop.',
        'An ego bus approaches the stop as a passenger comes toward the door too early.',
        'A passenger getting too close to the bus door before the stop is complete.',
        ['passenger_approaches_door_before_full_stop'], 'single_event',
        ['passenger_early_approach', 'door_side_area', 'approach_stop'], actor_violations=['passenger_enters_door_area_before_full_stop'], risk='low'
    ),
    'C05': spec(
        'dwell_at_stop',
        'The ego bus is stopped for passenger service, while a pedestrian walks through the door-side movement zone beside the bus.',
        'An ego bus is stopped while a pedestrian passes through the door-side area.',
        'A stopped-bus case with someone walking beside the doors.',
        ['pedestrian_passes_through_door_side_zone'], 'single_event',
        ['door_side_zone', 'pedestrian', 'bus_dwell'], scene_rules=['door_side_service_zone'], risk='medium'
    ),
    'C06': spec(
        'departure_merge',
        'The ego bus intends to depart from a bus stop located beside a crosswalk. One pedestrian enters the crosswalk ahead of the bus departure path.',
        'An ego bus prepares to leave a stop while a pedestrian crosses at the adjacent crosswalk.',
        'A bus departure case with a pedestrian on the nearby crosswalk.',
        ['pedestrian_crosses_adjacent_crosswalk'], 'single_event',
        ['crosswalk', 'pedestrian_crossing', 'bus_departure_path'], actor_rules=['pedestrian_uses_designated_crosswalk'], risk='low'
    ),
    'C07': spec(
        'straight_cruise',
        'The ego bus intends to continue along an unsignalized road segment. One pedestrian enters and crosses the motor lane ahead.',
        'An ego bus travels on an unsignalized road while a pedestrian crosses its lane.',
        'A simple pedestrian crossing in front of a moving bus.',
        ['pedestrian_crosses_unsignalized_road'], 'single_event',
        ['pedestrian_crossing', 'unsignalized_road'], actor_rules=['pedestrian_crossing_behavior'], risk='low'
    ),
    'C08': spec(
        'departure_merge',
        'The ego bus begins its departure from a stop. A pedestrian suddenly enters the lane directly in front of the bus departure path.',
        'An ego bus starts leaving a stop as a pedestrian suddenly crosses in front.',
        'A high-risk bus pull-out with a sudden pedestrian crossing.',
        ['pedestrian_sudden_crossing_into_departure_path'], 'single_event',
        ['pedestrian_sudden_crossing', 'bus_departure_path', 'high_risk'], actor_violations=['pedestrian_sudden_entry_into_vehicle_path'], risk='high'
    ),
    'C09': spec(
        'straight_cruise',
        'The ego bus intends to continue straight while a pedestrian starts crossing the motor lane and then suddenly reverses direction before clearing it.',
        'A pedestrian begins crossing in front of the ego bus and then turns back.',
        'A pedestrian changing direction in the middle of a bus-lane crossing.',
        ['pedestrian_starts_crossing', 'pedestrian_reverses_direction'], 'two_stage_chain',
        ['pedestrian_reverse', 'crossing_path_change', 'high_risk'], actor_violations=['pedestrian_unexpected_direction_reversal'], risk='high'
    ),
    'C10': spec(
        'dwell_at_stop',
        'The ego bus is stopped at a bus stop. A passenger alights, walks behind the bus, and crosses into the path of a following car, which decelerates.',
        'A passenger gets off the stopped ego bus, crosses behind it, and a following car slows.',
        'A passenger crossing behind a stopped bus with a car approaching from the rear.',
        ['passenger_alights', 'pedestrian_crossing_behind_bus', 'following_vehicle_deceleration'], 'three_stage_chain',
        ['alighting', 'rear_crossing', 'following_vehicle'], actor_rules=['following_vehicle_yields_to_pedestrian'], risk='medium'
    ),
    'C11': spec(
        'approach_stop',
        'The ego bus approaches a bus stop where one pedestrian is standing in the motor lane near the stopping area.',
        'An ego bus approaches a stop with a pedestrian waiting in the roadway.',
        'A bus-stop approach with someone standing in the road.',
        ['pedestrian_waiting_in_motor_lane'], 'single_event',
        ['pedestrian_waiting_on_roadway', 'bus_approach', 'medium_risk'], actor_violations=['pedestrian_waits_in_motor_lane'], risk='medium'
    ),
    'C12': spec(
        'straight_cruise',
        'The ego bus intends to continue along a divided road. One pedestrian climbs over a separation barrier and enters the motor-lane crossing area.',
        'An ego bus travels along a divided road while a pedestrian crosses over the barrier.',
        'A risky barrier-crossing case in front of a bus.',
        ['pedestrian_crosses_separation_barrier'], 'single_event',
        ['barrier_crossing', 'pedestrian', 'high_risk'], scene_rules=['separation_barrier'], actor_violations=['pedestrian_crosses_over_barrier'], risk='high'
    ),
    'C13': spec(
        'dwell_at_stop',
        'The ego bus is stopped at a bus stop beside a non-motor lane. A passenger alights into that lane, and a nearby e-bike decelerates as it approaches the passenger.',
        'A passenger steps from the stopped bus into the non-motor lane and an e-bike slows nearby.',
        'An alighting passenger and an e-bike beside a stopped bus.',
        ['passenger_alights_into_nonmotor_lane', 'e_bike_deceleration'], 'two_stage_chain',
        ['alighting', 'nonmotor_lane', 'e_bike'], actor_rules=['e_bike_yields_to_pedestrian'], risk='medium'
    ),
    'C14': spec(
        'approach_stop',
        'The ego bus approaches a stop where the adjacent sidewalk is blocked. One pedestrian detours from the sidewalk and walks along the road edge beside the bus lane.',
        'An ego bus approaches a stop while a pedestrian uses the road edge because the sidewalk is blocked.',
        'A bus-stop case with a pedestrian detouring along the road edge.',
        ['sidewalk_blockage', 'pedestrian_detour_along_road_edge'], 'two_stage_chain',
        ['sidewalk_blockage', 'pedestrian_detour', 'road_edge'], scene_rules=['temporary_sidewalk_blockage'], actor_rules=['pedestrian_road_edge_detour'], risk='medium'
    ),
    'C15': spec(
        'pass_through_stop_area',
        'The ego bus intends to continue past a bus stop. One pedestrian crosses the motor lane near the stop instead of using a marked crosswalk located nearby.',
        'An ego bus passes a stop while a pedestrian crosses outside the nearby crosswalk.',
        'A pedestrian crossing near a bus stop instead of using the crosswalk.',
        ['pedestrian_crosses_outside_nearby_crosswalk'], 'single_event',
        ['crosswalk_adjacent', 'pedestrian_violation', 'bus_stop_area'], scene_rules=['nearby_designated_crosswalk'], actor_violations=['pedestrian_ignores_nearby_crosswalk'], risk='medium'
    ),
    'C16': spec(
        'dwell_at_stop',
        'The ego bus is fully stopped at a designated stop. Passengers wait in order, those on board alight first, and waiting passengers then begin boarding.',
        'At a fully stopped ego bus, passengers wait in line and alight before boarding begins.',
        'An orderly bus-stop service case with people getting off before others get on.',
        ['passengers_wait_in_order', 'passengers_alight_then_board'], 'two_stage_chain',
        ['orderly_boarding', 'passenger_queue', 'bus_stop'], actor_rules=['passengers_wait_in_order', 'passengers_alight_before_boarding'], risk='low'
    ),
    'D01': spec(
        'dwell_at_stop',
        'The ego bus is stopped at a bus stop beside a non-motor lane. One e-bike travels normally through that lane alongside the bus.',
        'An e-bike passes normally in the non-motor lane beside the stopped ego bus.',
        'A calm e-bike pass beside a stopped bus.',
        ['e_bike_passes_in_nonmotor_lane'], 'single_event',
        ['e_bike', 'nonmotor_lane', 'bus_dwell'], actor_rules=['e_bike_uses_nonmotor_lane'], risk='normal'
    ),
    'D02': spec(
        'docking',
        'The ego bus intends to enter a bus bay across the edge of an adjacent non-motor lane. One e-bike approaches along that lane and decelerates as the two paths converge.',
        'An ego bus approaches a bus bay while an e-bike in the adjacent non-motor lane slows near the docking path.',
        'A bus-bay docking case with an e-bike beside the bay entrance.',
        ['e_bike_decelerates_near_docking_path'], 'single_event',
        ['bus_bay', 'e_bike', 'docking_path_interaction'], actor_rules=['e_bike_uses_nonmotor_lane'], risk='medium'
    ),
    'D03': spec(
        'departure_merge',
        'The ego bus intends to leave an inline stop and cross the edge of an adjacent non-motor lane. One cyclist continues straight through that lane beside the stop.',
        'An ego bus prepares to leave a stop while a cyclist continues straight in the adjacent non-motor lane.',
        'A bus pull-out case with a cyclist passing beside the stop.',
        ['cyclist_passes_in_adjacent_nonmotor_lane'], 'single_event',
        ['cyclist', 'bus_departure_path', 'nonmotor_lane'], actor_rules=['cyclist_uses_nonmotor_lane'], risk='low'
    ),
    'D04': spec(
        'departure_merge',
        'The ego bus intends to depart from a stop beside a non-motor lane. An illegally parked vehicle blocks that lane, an e-bike detours into the adjacent motor lane, and the e-bike later returns to the non-motor lane after passing the blockage.',
        'An ego bus prepares to depart while an e-bike detours from a blocked non-motor lane into the motor lane and later returns.',
        'A bus-stop case with an e-bike going around a parked blockage.',
        ['illegal_parking_blocks_nonmotor_lane', 'e_bike_detour_into_motor_lane', 'e_bike_returns_to_nonmotor_lane'], 'three_stage_chain',
        ['nonmotor_detour', 'e_bike', 'illegal_parking', 'departure_path_interaction'],
        scene_rules=['nonmotor_lane_present'], actor_rules=['e_bike_returns_to_nonmotor_lane_after_blockage'], actor_violations=['vehicle_blocks_nonmotor_lane'], risk='medium'
    ),
    'D05': spec(
        'pass_through_stop_area',
        'The ego bus proceeds slowly through a bus-stop area. One e-bike detours around a roadside blockage and then returns to the non-motor lane.',
        'An ego bus moves slowly through a stop area while an e-bike detours around a blockage and returns to its lane.',
        'An e-bike briefly going around a blockage beside a slowly moving bus.',
        ['e_bike_detours_around_blockage', 'e_bike_returns_to_nonmotor_lane'], 'two_stage_chain',
        ['e_bike', 'detour', 'return_to_lane'], actor_rules=['e_bike_returns_to_nonmotor_lane_after_blockage'], risk='low'
    ),
    'D06': spec(
        'straight_cruise',
        'The ego bus intends to continue straight through a road segment with a marked non-motor crossing. One bicycle crosses the motor lane at low speed through that crossing area.',
        'An ego bus travels straight while a bicycle crosses at a marked non-motor crossing.',
        'A bicycle crossing the bus lane at low speed.',
        ['bicycle_crosses_at_nonmotor_crossing'], 'single_event',
        ['bicycle', 'nonmotor_crossing', 'bus_path'], actor_rules=['bicycle_uses_nonmotor_crossing'], risk='low'
    ),
    'D07': spec(
        'straight_cruise',
        'The ego bus intends to continue straight. An e-bike rides across the motor lane through the bus path without dismounting at the non-motor crossing area.',
        'An e-bike rides across the ego bus lane without dismounting.',
        'A risky e-bike crossing directly through the bus path.',
        ['e_bike_crosses_motor_lane_without_dismounting'], 'single_event',
        ['e_bike', 'crossing_violation', 'bus_path'], actor_violations=['e_bike_crosses_without_dismounting'], risk='medium'
    ),
    'D08': spec(
        'departure_merge',
        'The ego bus is stopped at a bus stop and intends to begin its departure. A cyclist suddenly swerves from the curbside area into the adjacent motor lane to pass the bus.',
        'An ego bus prepares to leave a stop as a cyclist suddenly swerves into the motor lane beside it.',
        'A risky bus pull-out with a cyclist cutting out into the lane.',
        ['cyclist_sudden_swerve_into_motor_lane'], 'single_event',
        ['cyclist', 'sudden_swerve', 'bus_departure_path'], actor_violations=['cyclist_sudden_lane_swerve'], risk='high'
    ),
    'D09': spec(
        'dwell_at_stop',
        'The ego bus is stopped at a bus stop. One cyclist overtakes on the left using a separated path that does not enter the planned bus departure trajectory.',
        'A cyclist safely overtakes the stopped ego bus on the left.',
        'A low-risk cyclist pass beside a stopped bus.',
        ['cyclist_overtakes_stopped_bus_on_left'], 'single_event',
        ['cyclist_overtake', 'separated_path', 'bus_dwell'], actor_rules=['cyclist_overtake_without_path_obstruction'], risk='low'
    ),
    'D10': spec(
        'departure_merge',
        'The ego bus is stopped and intends to depart. A cyclist overtakes the bus on the left and then cuts across the planned departure path in front of the bus.',
        'An ego bus prepares to leave as a cyclist passes and cuts into its path.',
        'A risky cyclist pass followed by a cut-in ahead of the bus.',
        ['cyclist_overtakes_bus', 'cyclist_cuts_into_bus_path'], 'two_stage_chain',
        ['cyclist_cut_in', 'bus_departure_path', 'high_risk'], actor_violations=['cyclist_sudden_cut_in'], risk='high'
    ),
    'D11': spec(
        'departure_merge',
        'The ego bus intends to depart from a bus stop and merge into traffic. One motorcycle approaches quickly from behind along the target-lane side.',
        'An ego bus prepares to pull out while a motorcycle approaches quickly from behind.',
        'A bus pull-out with a fast motorcycle coming from the rear.',
        ['motorcycle_fast_rear_approach'], 'single_event',
        ['motorcycle', 'bus_departure', 'rear_approach'], risk='medium'
    ),
    'D12': spec(
        'departure_merge',
        'The ego bus begins its departure from a stop. At the same time, one motorcycle overtakes on the left and enters the area beside the bus front.',
        'An ego bus starts leaving a stop as a motorcycle overtakes on the left.',
        'A high-risk motorcycle pass as the bus pulls out.',
        ['motorcycle_overtakes_during_bus_departure'], 'single_event',
        ['motorcycle_overtake', 'bus_departure', 'high_risk'], actor_violations=['motorcycle_overtake_in_departure_conflict_area'], risk='high'
    ),
    'D13': spec(
        'dwell_at_stop',
        'The ego bus is stopped within queued traffic. One motorcycle filters through the space between the bus and the adjacent queued vehicles.',
        'A motorcycle filters through a queue beside the stopped ego bus.',
        'A motorcycle squeezing through queued traffic near a bus.',
        ['vehicle_queue_present', 'motorcycle_filters_between_queue_and_bus'], 'parallel_events',
        ['motorcycle_filtering', 'queue', 'bus_dwell'], actor_violations=['motorcycle_queue_filtering'], risk='medium', queue='queue'
    ),
    'D14': spec(
        'departure_merge',
        'The ego bus intends to depart from a stop beside a non-motor lane. An e-bike approaches in the wrong direction through that lane toward the departure path.',
        'An ego bus prepares to leave while a wrong-way e-bike approaches in the adjacent non-motor lane.',
        'A risky bus departure with an e-bike riding the wrong way.',
        ['e_bike_wrong_way_approach_in_nonmotor_lane'], 'single_event',
        ['e_bike', 'wrong_way', 'bus_departure_path'], actor_violations=['e_bike_wrong_way_travel'], risk='high'
    ),
    'D15': spec(
        'departure_merge',
        'The ego bus intends to leave a stop beside a non-motor lane. Multiple e-bikes pass continuously through the lane across the bus departure path.',
        'An ego bus prepares to leave while a continuous stream of e-bikes passes beside the stop.',
        'A bus pull-out with e-bikes continuously passing.',
        ['continuous_e_bike_flow_beside_stop'], 'single_event',
        ['e_bike_flow', 'bus_departure_path', 'nonmotor_lane'], actor_rules=['e_bikes_use_nonmotor_lane'], risk='low'
    ),
    'D16': spec(
        'departure_merge',
        'The ego bus begins to depart from a bus stop. An e-bike travels faster than the normal local flow through the stop-side non-motor lane and reaches the departure conflict area.',
        'An ego bus starts leaving a stop while a fast e-bike passes through the adjacent stop area.',
        'A high-risk bus pull-out with a fast e-bike passing beside it.',
        ['e_bike_fast_passing_through_stop_area'], 'single_event',
        ['fast_e_bike', 'bus_departure_path', 'high_risk'], actor_violations=['e_bike_excessive_speed_near_bus_stop'], risk='high'
    ),
    'E01': spec(
        'approach_stop',
        'The ego bus approaches a bus stop and begins its planned deceleration. A following car also decelerates while maintaining a separated following gap.',
        'An ego bus slows for a stop while the car behind slows and keeps its gap.',
        'A safe bus approach with a following car.',
        ['following_vehicle_safe_deceleration'], 'single_event',
        ['following_vehicle', 'safe_gap', 'bus_approach'], actor_rules=['following_vehicle_safe_gap'], risk='low'
    ),
    'E02': spec(
        'approach_stop',
        'The ego bus approaches a bus stop and begins its planned deceleration. A following car brakes late and rapidly closes the longitudinal gap behind the bus.',
        'An ego bus slows for a stop while the following car brakes late and closes the gap.',
        'A high-risk rear-gap closure behind a slowing bus.',
        ['following_vehicle_late_braking', 'following_vehicle_gap_closure'], 'two_stage_chain',
        ['following_vehicle', 'late_braking', 'rear_gap_closure'], actor_violations=['unsafe_following_gap'], risk='high'
    ),
    'E03': spec(
        'departure_merge',
        'The ego bus intends to exit a bus bay and merge into the target lane. One car in that lane decelerates and yields merge space.',
        'An ego bus prepares to leave a bus bay while a target-lane car yields.',
        'A bus-bay exit with a car giving space.',
        ['target_lane_vehicle_yield'], 'single_event',
        ['bus_bay', 'target_lane_vehicle_yield', 'departure'], actor_rules=['target_lane_vehicle_cooperative_yield'], risk='low'
    ),
    'E04': spec(
        'departure_merge',
        'The ego bus intends to depart from an inline stop and enter the target lane. A car in that lane accelerates toward the merge area instead of creating space.',
        'An ego bus prepares to pull out while a target-lane car accelerates toward the merge point.',
        'A risky bus pull-out with a car trying to pass the merge point first.',
        ['target_lane_vehicle_acceleration_non_yield'], 'single_event',
        ['target_lane_vehicle', 'non_yield', 'bus_departure_path'], risk='high'
    ),
    'E05': spec(
        'dwell_at_stop',
        'The ego bus is stopped with a queue of cars behind it. The queued cars remain in their lanes and do not enter adjacent restricted space.',
        'Cars queue behind a stopped ego bus without weaving or using restricted lanes.',
        'A compliant vehicle queue behind a stopped bus.',
        ['vehicle_queue_present', 'queued_vehicles_remain_in_lane'], 'parallel_events',
        ['queue', 'compliant_vehicle_behavior', 'stopped_bus'], actor_rules=['queued_vehicles_no_weaving', 'queued_vehicles_no_restricted_lane_borrowing'], risk='low', queue='queue'
    ),
    'E06': spec(
        'dwell_at_stop',
        'The ego bus is stopped with a vehicle queue behind it. One car leaves the queue and enters the adjacent non-motor lane to bypass the bus.',
        'A car uses the non-motor lane to bypass a queue behind a stopped ego bus.',
        'A bus queue with one car cutting through the bike lane.',
        ['vehicle_queue_present', 'car_illegal_nonmotor_lane_bypass'], 'two_stage_chain',
        ['illegal_bypass', 'nonmotor_lane', 'queue'], scene_rules=['restricted_nonmotor_lane'], actor_violations=['queue_bypass_via_nonmotor_lane'], risk='medium', queue='queue'
    ),
    'E07': spec(
        'straight_cruise',
        'The ego bus travels along a midblock segment beside roadside parking. One car exits a parking space toward the bus lane and yields to the bus and nearby pedestrians.',
        'An ego bus travels past roadside parking while a car pulls out and yields to the bus and pedestrians.',
        'A parked car pulling out beside a moving bus.',
        ['car_exits_roadside_parking', 'car_yields_to_bus_and_pedestrians'], 'two_stage_chain',
        ['roadside_parking', 'parking_exit', 'yielding_vehicle'], actor_rules=['parking_exit_vehicle_yields_to_road_users'], risk='low'
    ),
    'E08': spec(
        'straight_cruise',
        'The ego bus follows a lead car toward a marked crosswalk. A pedestrian enters the crosswalk, and the lead car stops before the crossing.',
        'An ego bus follows a car that stops for a pedestrian at a crosswalk.',
        'A crosswalk case with a lead car stopping ahead of the bus.',
        ['pedestrian_crossing', 'lead_vehicle_yields_at_crosswalk'], 'two_stage_chain',
        ['crosswalk', 'lead_vehicle', 'pedestrian'], actor_rules=['lead_vehicle_yields_to_pedestrian'], risk='low'
    ),
    'E09': spec(
        'dwell_at_stop',
        'The ego bus is stopped in dense but moving traffic. A following vehicle makes a legal lane change and passes the bus through the adjacent lane.',
        'A car legally changes lanes to pass the stopped ego bus in dense traffic.',
        'A dense but orderly car pass beside a stopped bus.',
        ['following_vehicle_legal_lane_change', 'following_vehicle_passes_stopped_bus'], 'two_stage_chain',
        ['lane_change', 'dense_traffic', 'legal_passing'], actor_rules=['vehicle_legal_lane_change_and_pass'], risk='low'
    ),
    'E10': spec(
        'dwell_at_stop',
        'The ego bus is stationary at a stop. One car passes slowly through the adjacent lane with a separated path and no pedestrian crossing in the pass area.',
        'A car slowly passes the stopped ego bus through the adjacent lane.',
        'A simple low-speed pass beside a stopped bus.',
        ['car_slow_pass_stopped_bus'], 'single_event',
        ['slow_passing', 'stopped_bus', 'separated_path'], actor_rules=['vehicle_passes_with_lane_separation'], risk='low'
    ),
    'E11': spec(
        'docking',
        'The ego bus intends to dock at a bus stop. A car cuts into the stop area ahead and occupies the bus docking path.',
        'An ego bus approaches its stop as a car cuts into the docking area.',
        'A bus-stop docking path blocked by a car cut-in.',
        ['car_cuts_into_bus_stop_area'], 'single_event',
        ['cut_in', 'bus_stop_area', 'docking_path'], actor_violations=['vehicle_enters_bus_stop_docking_area'], risk='medium'
    ),
    'E12': spec(
        'departure_merge',
        'The ego bus intends to leave a bus bay and merge into the target lane. Dense traffic occupies the lane continuously and provides no acceptable merge gap.',
        'An ego bus prepares to leave a bus bay while dense target-lane traffic provides no gap.',
        'A bus-bay exit with the target lane continuously occupied.',
        ['dense_target_lane_traffic', 'no_acceptable_merge_gap'], 'two_stage_chain',
        ['bus_bay', 'dense_traffic', 'gap_availability'], risk='medium'
    ),
    'F01': spec(
        'approach_stop',
        'The ego bus approaches a bus stop blocked by an illegally parked car. A pedestrian from the stop area enters the bus lane, while a following car decelerates behind the bus.',
        'An ego bus approaches a blocked stop as a pedestrian crosses from the stop area and a following car slows.',
        'A blocked-stop chain with a crossing pedestrian and a car behind.',
        ['illegal_parking_blocks_bus_stop', 'pedestrian_crossing_from_stop_area', 'following_vehicle_deceleration'], 'three_stage_chain',
        ['multi_event', 'blocked_stop', 'pedestrian_crossing', 'following_vehicle'], scene_rules=['bus_stop_no_parking_clearance_zone'], actor_violations=['vehicle_parking_in_bus_stop_area'], risk='high', queue='none',
        field_overrides={'counterpart_actors': [
            {'type': 'social_vehicle', 'count': 1, 'role': 'illegally_parked_vehicle'},
            {'type': 'pedestrian', 'count': 1, 'role': 'stop_area_crossing_pedestrian'},
            {'type': 'social_vehicle', 'count': 1, 'role': 'following_vehicle'}
        ]}
    ),
    'F02': spec(
        'departure_merge',
        'The ego bus intends to depart from a stop. A blockage sends an e-bike from the non-motor lane into the adjacent motor lane and into the planned bus merge path.',
        'An ego bus prepares to depart while an e-bike detours around a blockage into the merge path.',
        'A bus pull-out meeting an e-bike that detours around an obstacle.',
        ['nonmotor_lane_blockage', 'e_bike_detour_into_motor_lane', 'e_bike_enters_bus_merge_path'], 'three_stage_chain',
        ['multi_event', 'e_bike_detour', 'bus_departure_path'], scene_rules=['nonmotor_lane_present'], risk='high'
    ),
    'F03': spec(
        'docking',
        'The ego bus begins entering a bus bay. A pedestrian crosses the bay-entry path, while two following vehicles are already queued behind the bus.',
        'An ego bus starts bus-bay docking as a pedestrian crosses the entry path and two vehicles are queued behind.',
        'A bus-bay entry with a crossing pedestrian and a short queue behind.',
        ['pedestrian_crosses_bus_bay_entry', 'following_vehicle_queue_present'], 'parallel_events',
        ['bus_bay', 'pedestrian_crossing', 'following_queue'], actor_rules=['following_vehicles_remain_in_lane'], risk='high', queue='short_queue'
    ),
    'F04': spec(
        'approach_stop',
        'The ego bus approaches a stop where another bus occupies the only docking position. Dense traffic also fills the outer motor lane around the stop.',
        'An ego bus approaches an occupied stop in dense traffic.',
        'A busy bus stop with another bus already in the docking position.',
        ['preceding_bus_occupies_stop', 'dense_outer_lane_traffic'], 'parallel_events',
        ['dense_traffic', 'bus_queue_context', 'occupied_stop'], scene_rules=['single_file_bus_stop_service'], risk='medium', queue='queue'
    ),
    'F05': spec(
        'departure_merge',
        'The ego bus intends to depart from a stop. A blockage sends an e-bike from the non-motor lane into the motor lane, and a car in that lane decelerates to yield to the e-bike.',
        'An ego bus prepares to leave as an e-bike detours into the motor lane and a car yields to it.',
        'A three-agent bus-stop case with an e-bike detour and a yielding car.',
        ['nonmotor_lane_blockage', 'e_bike_detour_into_motor_lane', 'target_lane_car_yields_to_e_bike'], 'three_stage_chain',
        ['three_agent', 'e_bike_detour', 'vehicle_yield', 'bus_departure_context'], actor_rules=['target_lane_car_yields_to_detouring_e_bike'], risk='medium'
    ),
    'F06': spec(
        'pass_through_stop_area',
        'The ego bus intends to pass the stop area without service. A passenger hails from the motor lane, while following vehicles remain in their lanes rather than weaving around the bus.',
        'An ego bus passes a stop as a passenger hails from the roadway and following traffic stays in lane.',
        'A roadway-hailing case with orderly following traffic.',
        ['passenger_hails_from_motor_lane', 'following_vehicles_remain_in_lane'], 'parallel_events',
        ['passenger_hailing', 'pass_through_stop_area', 'following_traffic'], actor_rules=['following_vehicles_no_weaving'], actor_violations=['passenger_hails_from_motor_lane'], risk='medium'
    ),
    'F07': spec(
        'departure_merge',
        'The ego bus intends to exit a bus bay. A motorcycle approaches quickly from behind, while a car in the target lane decelerates too late near the merge area.',
        'An ego bus prepares to leave a bus bay while a fast motorcycle and a late-slowing car approach the merge area.',
        'A high-risk bus-bay exit with pressure from a motorcycle and a car.',
        ['motorcycle_fast_rear_approach', 'target_lane_car_late_deceleration'], 'parallel_events',
        ['bus_bay', 'motorcycle', 'target_lane_car', 'high_risk'], risk='high'
    ),
    'F08': spec(
        'dwell_at_stop',
        'The ego bus is stopped at a stop close to a crosswalk. A pedestrian enters the crossing while a car attempts to overtake the bus through the adjacent lane.',
        'A pedestrian crosses near a stopped ego bus while a car tries to overtake it.',
        'A dangerous crosswalk case with a stopped bus, a pedestrian, and an overtaking car.',
        ['pedestrian_crossing_near_bus_stop', 'car_overtakes_stopped_bus'], 'parallel_events',
        ['crosswalk', 'overtake', 'pedestrian', 'high_risk'], scene_rules=['overtaking_restricted_near_crosswalk'], actor_violations=['vehicle_overtakes_near_crosswalk'], risk='high'
    ),
    'G01': spec(
        'not_applicable',
        'Use a passenger car as the ego actor in a bus-stop scene, with the bus included only as a background vehicle.',
        'Use a passenger car as ego and keep the bus as a background actor.',
        'A bus-stop scene where the ego vehicle is a car, not the bus.',
        ['non_bus_ego_request'], 'single_event',
        ['unsupported', 'non_bus_ego'], risk='not_applicable', reason=['benchmark_scope_requires_ego_bus']
    ),
    'G02': spec(
        'straight_cruise',
        'Keep one cyclist entirely inside the non-motor lane at every timestep while also requiring that cyclist to cross perpendicularly through the motor lane at the same time.',
        'Require one cyclist to stay fully in the non-motor lane and cross the motor lane simultaneously.',
        'A cyclist that must stay in the bike lane while crossing the bus lane at the same time.',
        ['cyclist_required_to_remain_in_nonmotor_lane', 'cyclist_required_to_cross_motor_lane_simultaneously'], 'simultaneous_constraint_conflict',
        ['unsupported', 'spatial_constraint_contradiction', 'cyclist'], risk='not_applicable',
        reason=['mutually_exclusive_cyclist_spatial_constraints', 'simultaneous_lane_occupancy_contradiction'],
        field_overrides={
            'bus_stop_type': 'none',
            'road_topology': 'midblock_with_nonmotor_lane',
            'lane_configuration': {'motor_lanes_same_direction': 1, 'has_nonmotor_lane': True, 'has_crosswalk_zone': False},
            'counterpart_actors': [{'type': 'bicycle', 'count': 1, 'role': 'spatially_contradictory_cyclist'}]
        }
    ),
    'G03': spec(
        'departure_merge',
        'The ego bus intends to leave a bus stop and merge left, but the road has only one motor lane and no adjacent target lane.',
        'Require a bus departure merge on a single-lane road with no adjacent lane.',
        'A bus pull-out that requires a lane where none exists.',
        ['missing_adjacent_target_lane'], 'single_event',
        ['unsupported', 'topology_contradiction', 'missing_lane'], risk='not_applicable', reason=['missing_target_lane_for_merge', 'topology_contradiction']
    ),
    'G04': spec(
        'dwell_at_stop',
        'Place the ego bus at a midblock curb location where no designated bus stop exists, require passenger boarding there, and label the service fully compliant.',
        'Require fully compliant passenger boarding at a location with no bus stop.',
        'A legal bus pickup where there is no designated stop.',
        ['bus_stop_absent', 'passengers_board_at_nondesignated_location', 'full_compliance_label_request'], 'simultaneous_constraint_conflict',
        ['unsupported', 'rule_contradiction', 'out_of_stop_boarding'], scene_rules=['no_designated_bus_stop_present'], risk='not_applicable', reason=['fully_compliant_conflicts_with_out_of_stop_boarding_forbidden']
    ),
    'G05': spec(
        'not_applicable',
        'Use a motorcycle as the ego actor, with the bus represented only as a regular traffic participant.',
        'Use a motorcycle as ego in a bus-related traffic scene.',
        'A scene where the motorcycle is ego instead of the bus.',
        ['non_bus_ego_request'], 'single_event',
        ['unsupported', 'motorcycle_ego'], risk='not_applicable', reason=['benchmark_scope_requires_ego_bus']
    ),
    'G06': spec(
        'straight_cruise',
        'Use exactly one non-ego actor, and require that same actor to be both a pedestrian walking across the road and a motorcycle travelling in the motor lane at the same time.',
        'Require one actor to be both a crossing pedestrian and a moving motorcycle at the same time.',
        'A single actor that must be a pedestrian and a motorcycle simultaneously.',
        ['single_actor_required_as_pedestrian', 'same_actor_required_as_motorcycle'], 'simultaneous_constraint_conflict',
        ['unsupported', 'actor_type_contradiction', 'single_actor'], risk='not_applicable',
        reason=['single_actor_has_mutually_exclusive_types', 'actor_type_constraint_contradiction'],
        field_overrides={
            'bus_stop_type': 'none',
            'road_topology': 'straight_midblock',
            'lane_configuration': {'motor_lanes_same_direction': 1, 'has_nonmotor_lane': True, 'has_crosswalk_zone': True},
            'counterpart_actors': [{'type': 'conflicting_type', 'count': 1, 'role': 'simultaneously_pedestrian_and_motorcycle'}]
        }
    ),
    'G07': spec(
        'approach_stop',
        'Place one bus stop both immediately upstream and immediately downstream of the same intersection at the same coordinates.',
        'Require the same bus stop to be both before and after one intersection at one location.',
        'A bus stop that must be on both sides of the same junction at once.',
        ['bus_stop_required_upstream_of_intersection', 'same_bus_stop_required_downstream_of_intersection'], 'simultaneous_constraint_conflict',
        ['unsupported', 'facility_location_contradiction', 'bus_stop'], risk='not_applicable',
        reason=['bus_stop_has_mutually_exclusive_intersection_positions', 'facility_location_contradiction'],
        field_overrides={
            'bus_stop_type': 'contradictory_near_side_and_far_side_stop',
            'road_topology': 'signalized_intersection_with_contradictory_stop_location',
            'lane_configuration': {'motor_lanes_same_direction': 1, 'has_nonmotor_lane': 'optional', 'has_crosswalk_zone': True},
            'counterpart_actors': []
        }
    ),
    'G08': spec(
        'departure_merge',
        'The ego bus intends to merge from a bus stop into a motor lane, but the road contains no motor lane.',
        'Require the bus to merge into a motor lane on a road that has none.',
        'A bus merge into a lane that does not exist.',
        ['missing_motor_lane'], 'single_event',
        ['unsupported', 'missing_motor_lane', 'topology_contradiction'], risk='not_applicable', reason=['missing_motor_lane', 'topology_contradiction']
    ),
}


UNSUPPORTED_EVENT_OVERRIDES = {
    ('development', 'U01'): (['persistent_red_signal'], 'single_event'),
    ('development', 'U03'): (['motorcycle_enters_bus_lane'], 'single_event'),
    ('test', 'G01'): ([], 'not_applicable'),
    ('test', 'G02'): (['cyclist_crosses_motor_lane'], 'single_event'),
    ('test', 'G03'): ([], 'not_applicable'),
    ('test', 'G04'): (['passengers_present_for_boarding_at_nondesignated_location'], 'single_event'),
    ('test', 'G05'): ([], 'not_applicable'),
    ('test', 'G06'): (['actor_walks_across_road', 'same_actor_travels_in_motor_lane'], 'parallel_events'),
    ('test', 'G07'): ([], 'not_applicable'),
    ('test', 'G08'): ([], 'not_applicable'),
}

UNSUPPORTED_CONSTRAINTS = {
    ('development', 'U01'): ['signal_remains_red_for_entire_scenario', 'ego_bus_crosses_stop_line_without_stopping', 'crossing_labeled_fully_compliant'],
    ('development', 'U02'): ['initial_speed_zero', 'target_distance_m=100', 'time_limit_s=1', 'speed_cap_kmh=10', 'acceleration_cap_mps2=1'],
    ('development', 'U03'): ['non_ego_actor_count=0', 'motorcycle_interaction_required'],
    ('development', 'U04'): ['ego_position_change_zero_all_timesteps', 'ego_speed_zero_all_timesteps', 'left_turn_completion_required'],
    ('test', 'G01'): ['requested_ego_actor=passenger_car', 'benchmark_ego_actor=bus'],
    ('test', 'G02'): ['cyclist_remains_in_nonmotor_lane_all_timesteps', 'cyclist_crosses_motor_lane_simultaneously'],
    ('test', 'G03'): ['adjacent_target_lane_absent', 'left_merge_required'],
    ('test', 'G04'): ['designated_bus_stop_absent', 'passenger_boarding_required', 'behavior_labeled_fully_compliant'],
    ('test', 'G05'): ['requested_ego_actor=motorcycle', 'benchmark_ego_actor=bus'],
    ('test', 'G06'): ['non_ego_actor_count=1', 'same_actor_type=pedestrian', 'same_actor_type=motorcycle'],
    ('test', 'G07'): ['same_bus_stop_upstream_of_intersection', 'same_bus_stop_downstream_of_intersection', 'same_coordinates_required'],
    ('test', 'G08'): ['motor_lane_count=0', 'merge_into_motor_lane_required'],
}

CONTROLLED_PHASES = {
    'approach_stop', 'docking', 'dwell_at_stop', 'departure_merge', 'pass_through_stop_area',
    'straight_cruise', 'straight_through_intersection', 'right_turn_through_intersection',
    'left_turn_through_intersection', 'roundabout_entry', 'traverse_bottleneck',
    'transit_priority_straight_through_intersection', 'not_applicable',
}

SPECIAL_SEMANTIC_CHANGES = {
    'G02': 'Unsupported intent redefined from weather/camera/visual-occlusion dependence to a scene-level simultaneous spatial-constraint contradiction.',
    'G06': 'Unsupported intent redefined from LiDAR/occlusion dependence to a scene-level single-actor type contradiction.',
    'G07': 'Unsupported intent redefined from HD-map/visual-sign dependence to a scene-level bus-stop location contradiction.',
    'F01': 'Policy-dependent out-of-stop stopping and alighting outcome replaced by a blocked-stop approach with an external pedestrian crossing and following-vehicle response.',
    'F03': 'Policy-dependent docking-abort outcome removed; the pedestrian crossing and following queue are represented as external scene conditions.',
}

EGO_EVENT_PATTERN = re.compile(
    r'^(?:ego(?:_bus)?|bus)_',
    re.I,
)
STATIC_BUS_SCENE_RULE_PREFIXES = ('bus_bay_', 'bus_lane_', 'bus_stop_')
PLATFORM_PATTERN = re.compile(
    r'top[- ]down\s*2d|bird[’\'\- ]?s[- ]eye|overhead\s+(?:view|rendering)|camera[- ]view|lidar|point\s*cloud|visual\s+occlusion|night[- ]time|rainy\s+night|weather\s+conditioning|hd\s*map|visual\s+appearance',
    re.I,
)
TOP_DOWN_QUERY_PATTERN = re.compile(r'top[- ]down\s*2d|bird[’\'\- ]?s[- ]eye|overhead\s+(?:view|rendering)', re.I)
REACTION_TEXT_PATTERN = re.compile(
    r'(?:forc(?:e|es|ing)|caus(?:e|es|ing)|requir(?:e|es|ing))\s+(?:the\s+)?(?:ego\s+)?bus\s+to\s+(?:brake|wait|yield|stop|avoid|abort|swerve|slow\s+down|decelerate)|'
    r'(?:the\s+)?(?:ego\s+)?bus\s+(?:must|should|has\s+to|needs?\s+to|will)\s+(?:brake|wait|yield|stop|avoid|abort|swerve|slow\s+down|decelerate)|'
    r'make(?:s|ing)?\s+(?:the\s+)?(?:ego\s+)?bus\s+(?:brake|wait|yield|stop|avoid|abort|swerve|slow\s+down|decelerate)|'
    r'(?:the\s+)?(?:ego\s+)?bus\s+(?:brakes?|braking|yields?|yielding|waits?|waiting|avoids?|avoiding|aborts?|aborting|swerves?|swerving|slows?\s+down|slowing\s+down|decelerates?|decelerating|reacts?\s+by)\b',
    re.I,
)
REACTION_TAG_PATTERN = re.compile(r'near_miss|collision_avoid|hard_brak|bus_brak|bus_yield|bus_wait|abort_dock|gap_restoration|bus_acceleration|state_dependent_yield', re.I)


def flatten_rule_hooks(rule_hooks: Dict[str, List[str]]) -> List[str]:
    out: List[str] = []
    for key in ('scene_rules', 'counterpart_actor_rules', 'counterpart_actor_violations'):
        out.extend(rule_hooks.get(key, []))
    return out


def build_split(rows: List[Dict[str, Any]], specs: Dict[str, Dict[str, Any]], split: str) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    by_group: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_group[row['group_label']].append(row)
    if set(by_group) != set(specs):
        raise ValueError(f'{split}: spec labels mismatch: missing={sorted(set(by_group)-set(specs))}, extra={sorted(set(specs)-set(by_group))}')

    output: List[Dict[str, Any]] = []
    intent_maps: List[Dict[str, Any]] = []
    query_maps: List[Dict[str, Any]] = []
    policy_rows: List[Dict[str, Any]] = []

    for label in sorted(by_group):
        group_rows = by_group[label]
        s = copy.deepcopy(specs[label])
        if (split, label) in UNSUPPORTED_EVENT_OVERRIDES:
            s['events'], s['temporal'] = UNSUPPORTED_EVENT_OVERRIDES[(split, label)]
        unsupported_constraints = list(UNSUPPORTED_CONSTRAINTS.get((split, label), []))
        old_precise = next(r for r in group_rows if r['surface_style'] == 'precise')
        old_intent = old_precise['intent_group_id']
        if split == 'development':
            new_intent = old_intent.replace('BSG_DEV_v0_1_', 'BSG_DEV_v0_2_')
            new_library = 'BSG_DEV_v0_2'
        else:
            new_intent = old_intent.replace('BSG_v0_1_', 'BSG_v0_2_')
            new_library = 'BSG_v0_2'

        new_core = {
            'operation': s['phase'],
            'topology': s['field_overrides'].get('road_topology', old_precise.get('road_topology')),
            'actor_roles': [
                f"{a.get('type')}:{a.get('role')}"
                for a in s['field_overrides'].get('counterpart_actors', old_precise.get('counterpart_actors', []))
            ],
            'events': s['events'],
            'temporal_structure': s['temporal'],
            'support': old_precise['expected_support'],
        }
        old_core = old_precise.get('core_event_signature', {
            'operation': old_precise.get('bus_operation_phase'),
            'topology': old_precise.get('road_topology'),
            'actor_roles': [f"{a.get('type')}:{a.get('role')}" for a in old_precise.get('counterpart_actors', [])],
            'events': old_precise.get('interaction_events', []),
            'temporal_structure': old_precise.get('event_temporal_structure'),
            'support': old_precise.get('expected_support'),
        })

        changes = [
            'query_text_and_canonical_query_limited_to_initial_intent_context_counterpart_behavior_and_interaction_events',
            'ego_reaction_and_terminal_outcome_removed_from_generator_oracle',
            'interaction_events_externalized',
            'rule_hooks_rescoped_to_scene_and_counterpart_actors',
        ]
        if any(PLATFORM_PATTERN.search(r.get('query_text', '')) for r in group_rows) or PLATFORM_PATTERN.search(old_precise.get('canonical_query', '')):
            changes.append('platform_or_observation_instruction_removed_from_query_language')
        if old_precise.get('risk_level') == 'near_miss' or any('near_miss' in str(x) for x in old_precise.get('interaction_events', [])):
            if old_precise.get('expected_support') == 'unsupported':
                changes.append('policy_dependent_near_miss_outcome_removed_and_contradiction_reexpressed_as_actor_event_binding')
            else:
                changes.append('policy_dependent_near_miss_outcome_replaced_by_high_risk_external_exposure')
        if label in SPECIAL_SEMANTIC_CHANGES:
            changes.append(SPECIAL_SEMANTIC_CHANGES[label])

        intent_maps.append({
            'dataset_split': split,
            'group_label': label,
            'old_intent_group_id': old_intent,
            'new_intent_group_id': new_intent,
            'expected_support': old_precise['expected_support'],
            'old_core_event_signature': old_core,
            'new_core_event_signature': new_core,
            'unsupported_constraints_v0_2': unsupported_constraints,
            'change_summary': changes,
        })

        visible_rules = flatten_rule_hooks(s['rule_hooks'])
        old_events = list(old_precise.get('interaction_events', []))
        old_rules = list(old_precise.get('rule_hooks', []))
        removed_events = [e for e in old_events if e not in s['events']]
        policy_rows.append({
            'policy_diagnostic_id': f"PD_{new_intent}",
            'intent_group_id': new_intent,
            'dataset_split': split,
            'generator_visible': False,
            'diagnostic_use_only': True,
            'bus_operation_phase': s['phase'],
            'v0_1_interaction_labels_removed_from_generator_oracle': removed_events,
            'v0_1_rule_hooks_removed_or_rescoped': [r for r in old_rules if r not in visible_rules],
            'note': 'This manifest is not an input to scenario generators and does not prescribe a unique ego reaction. It is retained only for downstream policy-diagnostic design and migration auditing.',
        })

        for old in sorted(group_rows, key=lambda r: ('precise', 'partial', 'vague').index(r['surface_style'])):
            style = old['surface_style']
            new = copy.deepcopy(old)
            if split == 'development':
                new_query_id = old['query_id'].replace('BSG_DEV_v0_1_', 'BSG_DEV_v0_2_')
            else:
                new_query_id = old['query_id'].replace('BSG_v0_1_', 'BSG_v0_2_')

            new['query_id'] = new_query_id
            new['intent_group_id'] = new_intent
            new['dataset_split'] = split
            new['query_library_version'] = new_library
            new['metadata_schema_version'] = '0.2'
            new['active_workflow_status'] = 'active'
            new['supersedes_query_id'] = old['query_id']
            new['supersedes_intent_group_id'] = old_intent
            new['tuning_allowed'] = split == 'development'
            new['locked_test_usage'] = (
                'design_visible_reference_no_post_freeze_tuning'
                if split == 'development'
                else 'held_out_test_only_not_for_tuning'
            )
            if split == 'development':
                new['source_locked_test_library'] = 'BSG_v0_2'
            else:
                new.pop('source_locked_test_library', None)

            new['query_text'] = s['texts'][style]
            new['canonical_query'] = s['texts']['precise']
            new['surface_template_family'] = STYLE_FAMILY[style]
            new['view_mode'] = 'top_down_2d'
            new['benchmark_scope'] = copy.deepcopy(VIEW_SCOPE)
            new['bus_operation_phase'] = s['phase']
            new['interaction_events'] = list(s['events'])
            new['event_temporal_structure'] = s['temporal']
            new['unsupported_constraints'] = unsupported_constraints
            new['rule_hooks'] = copy.deepcopy(s['rule_hooks'])
            new['evaluation_tags'] = list(s['tags'])
            new['risk_level'] = s['risk'] if s['risk'] is not None else old.get('risk_level')
            new['queue_state'] = s['queue'] if s['queue'] is not None else old.get('queue_state')
            new['core_event_signature'] = copy.deepcopy(new_core)
            new['policy_diagnostic_ref'] = f"PD_{new_intent}"
            new['revised_date'] = CREATED_DATE
            new['source_inventory'] = f"{old.get('source_inventory', 'unspecified')} | v0.2 field-responsibility migration"

            if s['novelty'] is not None:
                new['locked_test_novelty_note'] = s['novelty']
            elif split != 'development':
                new.pop('locked_test_novelty_note', None)

            if s['reason'] is not None:
                new['expected_reason_if_unsupported'] = list(s['reason'])
            if old['expected_support'] == 'unsupported':
                new['acceptable_response'] = ['reject', 'ask_for_clarification', 'controlled_degradation']

            for field, value in s['field_overrides'].items():
                new[field] = copy.deepcopy(value)

            query_maps.append({
                'dataset_split': split,
                'group_label': label,
                'surface_style': style,
                'old_query_id': old['query_id'],
                'new_query_id': new_query_id,
                'old_intent_group_id': old_intent,
                'new_intent_group_id': new_intent,
            })
            output.append(new)

    return output, intent_maps, query_maps, policy_rows


def write_jsonl(path: Path, rows: List[Dict[str, Any]]) -> None:
    with path.open('w', encoding='utf-8') as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n')


def signature_key(row: Dict[str, Any]) -> tuple:
    c = row['core_event_signature']
    return (
        c.get('operation'), c.get('topology'), tuple(c.get('actor_roles', [])),
        tuple(c.get('events', [])), c.get('temporal_structure'), c.get('support'),
    )


def _core_features(row: Dict[str, Any]) -> set:
    core = row['core_event_signature']
    features = {
        'operation:{}'.format(core.get('operation')),
        'topology:{}'.format(core.get('topology')),
        'temporal:{}'.format(core.get('temporal_structure')),
        'support:{}'.format(core.get('support')),
    }
    features.update('actor:{}'.format(value) for value in core.get('actor_roles', []))
    features.update('event:{}'.format(value) for value in core.get('events', []))
    return features


def _jaccard(left: set, right: set) -> float:
    union = left | right
    return 0.0 if not union else len(left & right) / len(union)


def _surface_text(text: str) -> str:
    return ' '.join(re.findall(r'[a-z]+', text.lower()))


def build_overlap_report(
    dev_rows: List[Dict[str, Any]],
    test_rows: List[Dict[str, Any]],
    dev_sha256: str,
    test_sha256: str,
) -> Dict[str, Any]:
    near_core_threshold = 0.68
    template_threshold = 0.82
    errors: List[Dict[str, Any]] = []
    dev_intents = {row['intent_group_id'] for row in dev_rows}
    test_intents = {row['intent_group_id'] for row in test_rows}
    if len(dev_rows) != 48 or len(dev_intents) != 16:
        errors.append({'code': 'development_cardinality', 'rows': len(dev_rows), 'intents': len(dev_intents)})
    if len(test_rows) != 252 or len(test_intents) != 84:
        errors.append({'code': 'test_cardinality', 'rows': len(test_rows), 'intents': len(test_intents)})

    dev_precise = sorted(
        (row for row in dev_rows if row['surface_style'] == 'precise'),
        key=lambda row: row['intent_group_id'],
    )
    test_precise = sorted(
        (row for row in test_rows if row['surface_style'] == 'precise'),
        key=lambda row: row['intent_group_id'],
    )
    exact_core: List[Dict[str, Any]] = []
    near_core_pairs: List[Dict[str, Any]] = []
    for dev in dev_precise:
        for test in test_precise:
            if signature_key(dev) == signature_key(test):
                exact_core.append({
                    'development_intent_group_id': dev['intent_group_id'],
                    'test_intent_group_id': test['intent_group_id'],
                })
            score = _jaccard(
                _core_features(dev),
                _core_features(test),
            )
            near_core_pairs.append({
                'development_intent_group_id': dev['intent_group_id'],
                'test_intent_group_id': test['intent_group_id'],
                'score': round(score, 6),
            })

    surface_pairs: List[Dict[str, Any]] = []
    for dev in sorted(dev_rows, key=lambda row: row['query_id']):
        for test in sorted(test_rows, key=lambda row: row['query_id']):
            if dev['surface_style'] != test['surface_style']:
                continue
            score = difflib.SequenceMatcher(
                None,
                _surface_text(dev['query_text']),
                _surface_text(test['query_text']),
                autojunk=False,
            ).ratio()
            surface_pairs.append({
                'development_query_id': dev['query_id'],
                'test_query_id': test['query_id'],
                'surface_style': dev['surface_style'],
                'score': round(score, 6),
            })

    core_ranked = sorted(
        near_core_pairs,
        key=lambda item: (-item['score'], item['development_intent_group_id'], item['test_intent_group_id']),
    )
    surface_ranked = sorted(
        surface_pairs,
        key=lambda item: (-item['score'], item['development_query_id'], item['test_query_id']),
    )
    near_core_flags = [item for item in core_ranked if item['score'] >= near_core_threshold]
    near_template_flags = [item for item in surface_ranked if item['score'] >= template_threshold]
    return {
        'report_version': 'v0_2',
        'source_sha256': {
            'development_v0_2': dev_sha256,
            'test_v0_2': test_sha256,
        },
        'test_rows': len(test_rows),
        'test_intents': len(test_intents),
        'dev_rows': len(dev_rows),
        'dev_intents': len(dev_intents),
        'comparison_counts': {
            'core_intent_pairs': len(near_core_pairs),
            'same_style_surface_pairs': len(surface_pairs),
        },
        'validation_errors': errors,
        'thresholds': {'near_core': near_core_threshold, 'template': template_threshold},
        'methods': {
            'near_core': 'jaccard_over_typed_atomic_core_event_signature_features',
            'template': 'sequence_matcher_over_lowercase_alpha_query_text_same_surface_style',
        },
        'exact_core_overlaps': exact_core,
        'near_core_flags': near_core_flags,
        'near_template_flags': near_template_flags,
        'nearest_core_pairs_for_review': core_ranked[:10],
        'nearest_template_pairs_for_review': surface_ranked[:10],
        'pass': not errors and not exact_core and not near_core_flags and not near_template_flags,
        'interpretation': (
            'A clean pass requires no exact/near core-event match and no '
            'high-scaffold surface-template match. The nearest pairs are '
            'retained for manual threshold-adjacent review before freezing.'
        ),
    }


def validate(
    dev_rows: List[Dict[str, Any]],
    test_rows: List[Dict[str, Any]],
    mapping: Dict[str, Any],
    old_hashes: Dict[str, str],
    target_hashes: Dict[str, str],
    dev_v01: Path,
    test_v01: Path,
) -> Dict[str, Any]:
    issues: List[Dict[str, Any]] = []

    def add(code: str, detail: Any) -> None:
        issues.append({'code': code, 'detail': detail})

    def check_split(name: str, rows: List[Dict[str, Any]], expected_rows: int, expected_intents: int, supported_intents: int, unsupported_intents: int) -> Dict[str, Any]:
        groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for r in rows:
            groups[r['intent_group_id']].append(r)
        support_by_intent = Counter(next(iter(v))['expected_support'] for v in groups.values())
        styles = Counter(r['surface_style'] for r in rows)
        if len(rows) != expected_rows: add(f'{name}_row_count', len(rows))
        if len(groups) != expected_intents: add(f'{name}_intent_count', len(groups))
        if support_by_intent['supported'] != supported_intents: add(f'{name}_supported_intents', support_by_intent)
        if support_by_intent['unsupported'] != unsupported_intents: add(f'{name}_unsupported_intents', support_by_intent)
        for gid, rs in groups.items():
            if sorted(r['surface_style'] for r in rs) != ['partial', 'precise', 'vague']:
                add(f'{name}_style_triplet', gid)
            precise = next(r for r in rs if r['surface_style'] == 'precise')
            if any(r['canonical_query'] != precise['query_text'] for r in rs):
                add(f'{name}_canonical_mismatch', gid)
            if len({json.dumps(r['core_event_signature'], sort_keys=True) for r in rs}) != 1:
                add(f'{name}_core_signature_mismatch', gid)
        for r in rows:
            if r.get('view_mode') != 'top_down_2d': add(f'{name}_view_mode', r['query_id'])
            if r.get('benchmark_scope') != VIEW_SCOPE: add(f'{name}_benchmark_scope', r['query_id'])
            if PLATFORM_PATTERN.search(r.get('query_text', '')) or PLATFORM_PATTERN.search(r.get('canonical_query', '')):
                add(f'{name}_platform_instruction', r['query_id'])
            if REACTION_TEXT_PATTERN.search(r.get('query_text', '')):
                add(f'{name}_reaction_language', r['query_id'])
            if r.get('bus_operation_phase') not in CONTROLLED_PHASES:
                add(f'{name}_phase_vocab', [r['query_id'], r.get('bus_operation_phase')])
            for e in r.get('interaction_events', []):
                if EGO_EVENT_PATTERN.search(e):
                    add(f'{name}_ego_event_in_interaction_events', [r['query_id'], e])
            if not isinstance(r.get('rule_hooks'), dict) or set(r['rule_hooks']) != {'scene_rules', 'counterpart_actor_rules', 'counterpart_actor_violations'}:
                add(f'{name}_rule_hook_schema', r['query_id'])
            elif any(
                not isinstance(values, list)
                or any(not isinstance(value, str) for value in values)
                for values in r['rule_hooks'].values()
            ):
                add(f'{name}_rule_hook_value_schema', r['query_id'])
            else:
                for scope, values in r['rule_hooks'].items():
                    for value in values:
                        normalized = value.strip().lower()
                        if normalized.startswith(('ego_', 'ego_bus_')):
                            add(f'{name}_ego_rule_hook_subject', [r['query_id'], scope, value])
                        elif normalized.startswith('bus_') and not (
                            scope == 'scene_rules'
                            and normalized.startswith(STATIC_BUS_SCENE_RULE_PREFIXES)
                        ):
                            add(f'{name}_bus_reaction_rule_hook', [r['query_id'], scope, value])
            if any(REACTION_TAG_PATTERN.search(t) for t in r.get('evaluation_tags', [])):
                add(f'{name}_reaction_tag', [r['query_id'], r.get('evaluation_tags')])
            if r.get('risk_level') == 'near_miss':
                add(f'{name}_near_miss_risk_outcome', r['query_id'])
            if r.get('expected_support') == 'supported' and r.get('unsupported_constraints'):
                add(f'{name}_supported_has_unsupported_constraints', r['query_id'])
            if r.get('expected_support') == 'unsupported' and not r.get('unsupported_constraints'):
                add(f'{name}_unsupported_missing_constraints', r['query_id'])
            if r.get('event_temporal_structure') == 'not_applicable' and r.get('interaction_events'):
                add(f'{name}_events_temporal_mismatch', r['query_id'])
            if r.get('event_temporal_structure') != 'not_applicable' and not r.get('interaction_events'):
                add(f'{name}_events_temporal_mismatch', r['query_id'])
            if name == 'development' and r.get('tuning_allowed') is not True:
                add(f'{name}_tuning_flag', r['query_id'])
            if name == 'test' and r.get('tuning_allowed') is not False:
                add(f'{name}_tuning_flag', r['query_id'])
            expected_usage = (
                'design_visible_reference_no_post_freeze_tuning'
                if name == 'development'
                else 'held_out_test_only_not_for_tuning'
            )
            if r.get('locked_test_usage') != expected_usage:
                add(f'{name}_locked_test_usage', [r['query_id'], r.get('locked_test_usage')])
        return {
            'rows': len(rows),
            'intents': len(groups),
            'surface_styles': dict(styles),
            'support_by_intent': dict(support_by_intent),
            'support_by_query': dict(Counter(r['expected_support'] for r in rows)),
        }

    dev_summary = check_split('development', dev_rows, 48, 16, 12, 4)
    test_summary = check_split('test', test_rows, 252, 84, 76, 8)

    dev_precise = [r for r in dev_rows if r['surface_style'] == 'precise']
    test_precise = [r for r in test_rows if r['surface_style'] == 'precise']
    dev_sigs = {signature_key(r): r['intent_group_id'] for r in dev_precise}
    test_sigs = {signature_key(r): r['intent_group_id'] for r in test_precise}
    exact_overlap = sorted(set(dev_sigs) & set(test_sigs), key=str)
    if exact_overlap:
        add('dev_test_exact_core_signature_overlap', [
            {'dev': dev_sigs[k], 'test': test_sigs[k], 'signature': k} for k in exact_overlap
        ])

    mapping_intents = mapping['intent_mappings']
    mapping_queries = mapping['query_mappings']
    if len(mapping_intents) != 100: add('intent_mapping_count', len(mapping_intents))
    if len(mapping_queries) != 300: add('query_mapping_count', len(mapping_queries))
    if len({m['old_intent_group_id'] for m in mapping_intents}) != 100 or len({m['new_intent_group_id'] for m in mapping_intents}) != 100:
        add('intent_mapping_not_one_to_one', None)
    if len({m['old_query_id'] for m in mapping_queries}) != 300 or len({m['new_query_id'] for m in mapping_queries}) != 300:
        add('query_mapping_not_one_to_one', None)

    source_dev_rows = load_jsonl(dev_v01)
    source_test_rows = load_jsonl(test_v01)
    expected_query_sets = {
        'development': (
            {row['query_id'] for row in source_dev_rows},
            {row['query_id'] for row in dev_rows},
        ),
        'test': (
            {row['query_id'] for row in source_test_rows},
            {row['query_id'] for row in test_rows},
        ),
    }
    expected_intent_sets = {
        'development': (
            {row['intent_group_id'] for row in source_dev_rows},
            {row['intent_group_id'] for row in dev_rows},
        ),
        'test': (
            {row['intent_group_id'] for row in source_test_rows},
            {row['intent_group_id'] for row in test_rows},
        ),
    }
    for split in ('development', 'test'):
        split_queries = [item for item in mapping_queries if item.get('dataset_split') == split]
        old_queries, new_queries = expected_query_sets[split]
        if {item.get('old_query_id') for item in split_queries} != old_queries:
            add(f'{split}_mapping_old_query_coverage', None)
        if {item.get('new_query_id') for item in split_queries} != new_queries:
            add(f'{split}_mapping_new_query_coverage', None)
        split_intents = [item for item in mapping_intents if item.get('dataset_split') == split]
        old_intents, new_intents = expected_intent_sets[split]
        if {item.get('old_intent_group_id') for item in split_intents} != old_intents:
            add(f'{split}_mapping_old_intent_coverage', None)
        if {item.get('new_intent_group_id') for item in split_intents} != new_intents:
            add(f'{split}_mapping_new_intent_coverage', None)

    current_hashes = {'dev_v0_1': sha256(dev_v01), 'test_v0_1': sha256(test_v01)}
    if current_hashes != EXPECTED_V01_SHA256:
        add('v0_1_source_hash_changed', {
            'expected': EXPECTED_V01_SHA256,
            'actual': current_hashes,
        })
    if old_hashes != EXPECTED_V01_SHA256:
        add('mapping_v0_1_source_hash_mismatch', {
            'expected': EXPECTED_V01_SHA256,
            'actual': old_hashes,
        })
    if mapping.get('source_sha256') != EXPECTED_V01_SHA256:
        add('mapping_source_sha256_mismatch', mapping.get('source_sha256'))
    if mapping.get('target_sha256') != target_hashes:
        add('mapping_target_sha256_mismatch', {
            'expected': target_hashes,
            'actual': mapping.get('target_sha256'),
        })

    source_rows = source_dev_rows + source_test_rows
    source_top_down_query_text_occurrences = sum(
        bool(TOP_DOWN_QUERY_PATTERN.search(r.get('query_text', '')))
        for r in source_rows
    )
    target_rows = dev_rows + test_rows
    target_top_down_query_text_occurrences = sum(
        bool(TOP_DOWN_QUERY_PATTERN.search(r.get('query_text', '')))
        for r in target_rows
    )
    target_platform_occurrences = sum(
        bool(PLATFORM_PATTERN.search(r.get('query_text', '')) or PLATFORM_PATTERN.search(r.get('canonical_query', '')))
        for r in target_rows
    )
    target_reaction_language_occurrences = sum(
        bool(REACTION_TEXT_PATTERN.search(r.get('query_text', '')) or REACTION_TEXT_PATTERN.search(r.get('canonical_query', '')))
        for r in target_rows
    )

    return {
        'validation_pass': not issues,
        'created_date': CREATED_DATE,
        'v0_1_source_hashes': current_hashes,
        'source_query_text_top_down_2d_occurrences': source_top_down_query_text_occurrences,
        'target_query_text_top_down_2d_occurrences': target_top_down_query_text_occurrences,
        'target_query_and_canonical_platform_instruction_occurrences': target_platform_occurrences,
        'target_ego_reaction_language_occurrences': target_reaction_language_occurrences,
        'development': dev_summary,
        'test': test_summary,
        'mapping': {'intent_mappings': len(mapping_intents), 'query_mappings': len(mapping_queries)},
        'view_mode_definition': 'Scenarios execute in the native simulation environment; semantic geometry metrics use ground-plane projection, and height features are excluded from method ranking.',
        'exact_dev_test_core_signature_overlap_count': len(exact_overlap),
        'issues': issues,
    }


def _readme_text() -> str:
    return (
        '# Bus-Ego Query Library v0.2\n\n'
        '## Active release files\n\n'
        '- `bus_ego_topdown_2d_dev_query_library_v0_2.jsonl`: 48 development queries, 16 intents.\n'
        '- `bus_ego_topdown_2d_query_library_v0_2.jsonl`: 252 locked test queries, 84 intents.\n'
        '- `query_id_mapping_v0_1_to_v0_2.json`: one-to-one intent and query migration map.\n'
        '- `policy_diagnostic_manifest_v0_2.jsonl`: generator-invisible migration audit.\n'
        '- `v0_2_validation_report.json`: structural, source, mapping, and semantic-scope checks.\n'
        '- `dev_vs_test_overlap_report_v0_2.json`: reproducible overlap audit.\n'
        '- `build_query_library_v0_2.py`: project-relative deterministic builder and checker.\n'
        '- `SHA256SUMS.txt`: hashes for every active release file above.\n\n'
        'The checked-in v0.1 libraries are immutable migration sources. Publishing '
        'v0.2 does not silently migrate a consumer: active workflows must explicitly '
        'bind the v0.2 filenames and hashes.\n\n'
        '## Split usage\n\n'
        '- Development rows use `tuning_allowed=true` and '
        '`locked_test_usage=design_visible_reference_no_post_freeze_tuning`.\n'
        '- Test rows use `tuning_allowed=false` and '
        '`locked_test_usage=held_out_test_only_not_for_tuning`.\n\n'
        '## Field responsibilities\n\n'
        '- `bus_operation_phase` is the sole structured source of the ego bus initial intent.\n'
        '- `query_text` and `canonical_query` contain only initial intent, context, counterpart actors, and external interaction events.\n'
        '- `interaction_events` contains only counterpart-actor actions, environment/signal events, and policy-independent triggers.\n'
        '- `event_temporal_structure` orders only those external events.\n'
        '- `unsupported_constraints` stores scope, topology, type, kinematic, or logical conflicts.\n'
        '- `rule_hooks` remains a structured object with `scene_rules`, '
        '`counterpart_actor_rules`, and `counterpart_actor_violations`.\n'
        '- `risk_level` describes external exposure/criticality, not a realized ego-policy outcome.\n\n'
        '## Meaning of `view_mode`\n\n'
        '`"view_mode": "top_down_2d"` means native simulation with ground-plane '
        'semantic geometry; height features are excluded from method ranking.\n\n'
        '## Reproduction\n\n'
        'Run with the repository Python 3.8 environment:\n\n'
        '```bash\n'
        './chatscene/bin/python query_lib/build_query_library_v0_2.py --check\n'
        '```\n'
    )


def _write_sha256sums(output_dir: Path) -> Path:
    path = output_dir / SHA256SUMS_NAME
    lines = ['{}  {}'.format(sha256(output_dir / name), name) for name in RELEASE_FILES]
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return path


def build_release(source_dir: Path, output_dir: Path) -> Dict[str, Any]:
    source_dir = source_dir.resolve()
    output_dir = output_dir.resolve()
    dev_v01 = source_dir / DEV_V01_NAME
    test_v01 = source_dir / TEST_V01_NAME
    for source in (dev_v01, test_v01):
        if not source.is_file():
            raise SystemExit('missing checked-in v0.1 source: {}'.format(source))
    old_hashes = {'dev_v0_1': sha256(dev_v01), 'test_v0_1': sha256(test_v01)}
    if old_hashes != EXPECTED_V01_SHA256:
        raise SystemExit(json.dumps({
            'error': 'immutable_v0_1_source_hash_mismatch',
            'expected': EXPECTED_V01_SHA256,
            'actual': old_hashes,
        }, indent=2, sort_keys=True))
    output_dir.mkdir(parents=True, exist_ok=True)
    for name in RELEASE_FILES + (SHA256SUMS_NAME,):
        target = output_dir / name
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise SystemExit('unsafe release target: {}'.format(target))
    dev_v02 = output_dir / DEV_V02_NAME
    test_v02 = output_dir / TEST_V02_NAME
    mapping_path = output_dir / MAPPING_NAME
    policy_path = output_dir / POLICY_MANIFEST_NAME
    validation_path = output_dir / VALIDATION_NAME
    overlap_path = output_dir / OVERLAP_NAME
    readme_path = output_dir / README_NAME
    script_path = output_dir / SCRIPT_NAME

    dev_old = load_jsonl(dev_v01)
    test_old = load_jsonl(test_v01)
    dev_new, dev_intent_maps, dev_query_maps, dev_policy = build_split(dev_old, DEV, 'development')
    test_new, test_intent_maps, test_query_maps, test_policy = build_split(test_old, TEST, 'test')

    write_jsonl(dev_v02, dev_new)
    write_jsonl(test_v02, test_new)
    target_hashes = {
        'dev_v0_2': sha256(dev_v02),
        'test_v0_2': sha256(test_v02),
    }
    policy_rows = dev_policy + test_policy
    write_jsonl(policy_path, policy_rows)

    mapping = {
        'mapping_version': 'v0_1_to_v0_2',
        'created_date': CREATED_DATE,
        'source_files': {
            'development_v0_1': DEV_V01_NAME,
            'test_v0_1': TEST_V01_NAME,
        },
        'source_sha256': old_hashes,
        'target_files': {
            'development_v0_2': DEV_V02_NAME,
            'test_v0_2': TEST_V02_NAME,
        },
        'target_sha256': target_hashes,
        'mapping_guarantee': 'Each v0.1 intent and query maps to exactly one v0.2 intent and query. v0.1 remains immutable and historical.',
        'intent_mappings': dev_intent_maps + test_intent_maps,
        'query_mappings': dev_query_maps + test_query_maps,
    }
    mapping_path.write_text(json.dumps(mapping, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    report = validate(
        dev_new,
        test_new,
        mapping,
        old_hashes,
        target_hashes,
        dev_v01,
        test_v01,
    )
    target_intents = {row['intent_group_id'] for row in dev_new + test_new}
    policy_intents = [row['intent_group_id'] for row in policy_rows]
    if len(policy_intents) != len(set(policy_intents)) or set(policy_intents) != target_intents:
        report['issues'].append({'code': 'policy_manifest_intent_coverage', 'detail': None})
    overlap = build_overlap_report(dev_new, test_new, sha256(dev_v02), sha256(test_v02))
    if not overlap['pass']:
        report['issues'].append({'code': 'dev_test_overlap_report_failed', 'detail': None})
    report['overlap'] = {
        'pass': overlap['pass'],
        'exact_core_overlaps': len(overlap['exact_core_overlaps']),
        'near_core_flags': len(overlap['near_core_flags']),
        'near_template_flags': len(overlap['near_template_flags']),
    }
    report['validation_pass'] = not report['issues']
    validation_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    overlap_path.write_text(json.dumps(overlap, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    readme_path.write_text(_readme_text(), encoding='utf-8')
    source_script = Path(__file__).resolve()
    if source_script != script_path.resolve():
        shutil.copyfile(str(source_script), str(script_path))
    sha_path = _write_sha256sums(output_dir)

    if not report['validation_pass']:
        raise SystemExit(json.dumps(report['issues'], indent=2))
    return {
        'dev_v0_2': str(dev_v02),
        'test_v0_2': str(test_v02),
        'mapping': str(mapping_path),
        'policy_manifest': str(policy_path),
        'validation': str(validation_path),
        'overlap': str(overlap_path),
        'readme': str(readme_path),
        'sha256sums': str(sha_path),
        'validation_pass': report['validation_pass'],
    }


def check_release(source_dir: Path) -> Dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix='query-library-v0-2-check-') as temporary:
        generated = Path(temporary)
        build_release(source_dir, generated)
        mismatches = []
        for name in RELEASE_FILES + (SHA256SUMS_NAME,):
            expected = source_dir / name
            actual = generated / name
            if not expected.is_file():
                mismatches.append({'file': name, 'reason': 'missing_checked_in_file'})
            elif expected.read_bytes() != actual.read_bytes():
                mismatches.append({
                    'file': name,
                    'reason': 'byte_mismatch',
                    'expected_sha256': sha256(expected),
                    'actual_sha256': sha256(actual),
                })
        if mismatches:
            raise SystemExit(json.dumps({'reproducible': False, 'mismatches': mismatches}, indent=2))
    return {'reproducible': True, 'files_checked': len(RELEASE_FILES) + 1}


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Build or verify query library v0.2')
    parser.add_argument(
        '--source-dir',
        type=Path,
        default=QUERY_LIB_DIR,
        help='directory containing the immutable checked-in v0.1 sources',
    )
    parser.add_argument(
        '--output-dir',
        type=Path,
        help='output directory; defaults to --source-dir',
    )
    parser.add_argument(
        '--check',
        action='store_true',
        help='rebuild in a temporary directory and compare every release byte',
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)
    source_dir = args.source_dir.resolve()
    if args.check:
        if args.output_dir is not None:
            raise SystemExit('--check cannot be combined with --output-dir')
        result = check_release(source_dir)
    else:
        result = build_release(source_dir, (args.output_dir or source_dir).resolve())
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
