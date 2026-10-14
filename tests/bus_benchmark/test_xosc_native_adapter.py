import copy
import json
import unittest
from types import SimpleNamespace

from bus_benchmark import carla_xosc
from bus_benchmark.carla_xosc_worker import _require_native_carla_version
from bus_benchmark.errors import ValidationError
from bus_benchmark.generation import _finalize_ego_artifact
from bus_benchmark.runtime import _valid_carla_source_roles


XML = '''<OpenSCENARIO><RoadNetwork><LogicFile filepath="Town05"/></RoadNetwork><Entities>
<ScenarioObject name="hero"><Vehicle name="vehicle.tesla.model3" vehicleCategory="car"><BoundingBox><Center x="1"/><Dimensions length="4.5" width="1.8" height="1.5"/></BoundingBox><Properties><Property name="type" value="ego_vehicle"/></Properties></Vehicle></ScenarioObject>
<ScenarioObject name="other"><Vehicle name="vehicle.lincoln.mkz_2017"><BoundingBox><Dimensions length="4.5" width="1.8" height="1.5"/></BoundingBox></Vehicle></ScenarioObject>
</Entities></OpenSCENARIO>'''
DOCUMENT = {'artifact_type': carla_xosc.ARTIFACT_TYPE, 'xosc': XML,
            'ego_route': {'start_location': dict(x='1', y='2', z='0.2', h='90'),
                          'target_location': dict(x='11', y='12', z='0.2', h='90')}}


class XoscNativeTests(unittest.TestCase):
    def test_native_release_accepts_identical_build_suffix_and_rejects_mismatch(self):
        for client, server, accepted in (
            ("0.9.13", "0.9.13", True),
            ("0.9.13-2-g0c41f167c-dirty", "0.9.13-2-g0c41f167c-dirty", True),
            ("0.9.13", "0.9.13-other", False),
            ("0.9.15", "0.9.15", False),
            ("0.9.130", "0.9.130", False),
        ):
            native = SimpleNamespace(get_client_version=lambda: client, get_server_version=lambda: server)
            with self.subTest(client=client, server=server):
                if accepted:
                    _require_native_carla_version(native)
                else:
                    with self.assertRaises(ValueError):
                        _require_native_carla_version(native)

    def test_initial_observation_preserves_native_coordinate_convention(self):
        actions = '''<Storyboard><Init><Actions>
<Private entityRef="hero"><PrivateAction><TeleportAction><Position><WorldPosition x="1" y="2" h="1.2"/></Position></TeleportAction></PrivateAction></Private>
<Private entityRef="other"><PrivateAction><TeleportAction><Position><WorldPosition x="4" y="5" h="0.3"/></Position></TeleportAction></PrivateAction></Private>
</Actions></Init></Storyboard>'''
        for description, expected_y, expected_heading in (("CARLA:demo", -2.0, -1.2), ("standard", 2.0, 1.2)):
            text = XML.replace("<OpenSCENARIO>", '<OpenSCENARIO><FileHeader description="' + description + '"/>').replace("</OpenSCENARIO>", actions + "</OpenSCENARIO>")
            document = dict(DOCUMENT, xosc=text)
            before = copy.deepcopy(document)
            observation = carla_xosc.observe(document)
            self.assertEqual(document, before)
            self.assertEqual(observation["objects"][0]["initial_position"], [1.0, expected_y])
            self.assertEqual(observation["objects"][0]["heading_rad"], expected_heading)
            self.assertFalse(observation["objects"][0]["motion_observed"])
            self.assertEqual(len(observation["objects"][0]["trajectory"]), 1)

    def test_normalization_changes_only_three_ego_attribute_values(self):
        raw = json.dumps(DOCUMENT).encode()
        result = _finalize_ego_artifact('carla', raw, artifact_type=carla_xosc.ARTIFACT_TYPE)
        final = carla_xosc.parse(result.final_bytes)
        expected = XML.replace('name="vehicle.tesla.model3"', 'name="vehicle.chevrolet.impala"', 1).replace('length="4.5" width="1.8"', 'length="5.33" width="2.1"', 1)
        self.assertEqual(final['xosc'], expected)
        self.assertEqual(final['ego_route'], DOCUMENT['ego_route'])
        carla_xosc.verify(raw, result.final_bytes)
        self.assertEqual(carla_xosc.finalize(result.final_bytes), result.final_bytes)
        changed = copy.deepcopy(final)
        changed['xosc'] = changed['xosc'].replace('vehicle.lincoln.mkz_2017', 'vehicle.wrong')
        with self.assertRaises(ValueError):
            carla_xosc.verify(raw, json.dumps(changed).encode())

    def test_ambiguous_ego_nonfinite_route_external_catalogs_fail(self):
        for text in ('<OpenSCENARIO><Entities>', XML.replace('name="other"', 'name="hero"'),
                     XML.replace('value="ego_vehicle"', 'value="simulation"'),
                     XML.replace('<Entities>', '<CatalogLocations><VehicleCatalog directory="../other"/></CatalogLocations><Entities>'),
                     '<!DOCTYPE OpenSCENARIO [<!ENTITY external SYSTEM "file:///tmp/private">]>' + XML,
                     XML.replace('<Entities>', '<UserDefinedAction><CustomCommandAction type="unsafe"/></UserDefinedAction><Entities>'),
                     XML.replace('<Entities>', '<Controller><Properties><Property name="module" value="outside_runtime"/></Properties></Controller><Entities>')):
            doc = dict(DOCUMENT, xosc=text)
            with self.assertRaises((ValueError, ValidationError)):
                _finalize_ego_artifact('carla', json.dumps(doc).encode(), artifact_type=carla_xosc.ARTIFACT_TYPE)
        doc = copy.deepcopy(DOCUMENT)
        doc['ego_route']['start_location']['x'] = 'nan'
        with self.assertRaises(ValueError):
            carla_xosc.parse(json.dumps(doc).encode())

    def test_backend_roles_allow_one_native_backend_and_server(self):
        self.assertTrue(_valid_carla_source_roles({'scenic_editable_source', 'carla_server_distribution'}))
        self.assertTrue(_valid_carla_source_roles({'openscenario_source', 'carla_server_distribution'}))
        self.assertFalse(_valid_carla_source_roles({'scenic_editable_source', 'openscenario_source'}))
        self.assertFalse(_valid_carla_source_roles({'scenic_editable_source', 'openscenario_source', 'carla_server_distribution'}))


if __name__ == '__main__':
    unittest.main()
