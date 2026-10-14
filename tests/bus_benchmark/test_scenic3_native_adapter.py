import tempfile
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from bus_benchmark import native_observer
from bus_benchmark.carla_scenic3 import finalize, verify
from bus_benchmark.errors import ValidationError
from bus_benchmark.jsonio import sha256_file
from bus_benchmark.runtime import _prepare_carla_native_input
from bus_benchmark.scenic_map_declaration import map_declaration


class Scenic3NativeTests(unittest.TestCase):
    def test_initial_observation_does_not_treat_planned_path_as_motion(self):
        actor = SimpleNamespace(
            behavior=SimpleNamespace(_args=([(0, 0), (20, 5)],)),
            position=(1, 2), heading=0.0, length=5.33, width=2.1,
            blueprint="vehicle.chevrolet.impala",
        )
        scene = SimpleNamespace(objects=[actor], egoObject=actor)
        translator = ModuleType("scenic.syntax.translator")
        translator.scenarioFromString = lambda *args, **kwargs: SimpleNamespace(
            generate=lambda **kwargs: (scene, 1)
        )
        modules = {"scenic": ModuleType("scenic"),
                   "scenic.syntax": ModuleType("scenic.syntax"),
                   "scenic.syntax.translator": translator}
        numpy = ModuleType("numpy")
        numpy.random = SimpleNamespace(seed=lambda value: None)
        modules["numpy"] = numpy
        for plan in ([(0, 0), (20, 5)], [object(), object()]):
            with self.subTest(plan=type(plan[0]).__name__):
                actor.behavior._args = (plan,)
                with patch.dict(sys.modules, modules):
                    observation = native_observer.observe("carla", "ego = new Car", scenic_2d=True)
                self.assertEqual(observation["objects"][0]["trajectory"], [[1.0, 2.0]])
                self.assertFalse(observation["objects"][0]["motion_observed"])

    def test_shared_blueprint_is_preserved_for_non_ego_actors(self):
        raw = b'MODEL = "vehicle.tesla.model3"\n\nego = new Car at 1@2, with blueprint MODEL\nother = new Car at 3@4, with blueprint MODEL\n'
        final, changes = finalize(raw)
        self.assertIn(raw.splitlines()[0], final)
        self.assertIn(raw.splitlines()[-1], final)
        self.assertIn(b'with blueprint "vehicle.chevrolet.impala"', final)
        self.assertIn(b'with length 5.33', final)
        self.assertIn(b'with width 2.1', final)
        self.assertEqual(set(changes), {'ego_blueprint', 'ego_length', 'ego_width'})
        verify(raw, final)
        self.assertEqual(finalize(final)[0], final)
        with self.assertRaises(ValidationError):
            verify(raw, final.replace(b'other = new Car at 3@4', b'other = new Car at 9@4'))

    def test_nested_values_and_comments_are_not_clause_boundaries(self):
        raw = b'ego = new Car with blueprint Uniform("a", "b"),\n    with length Range(2, 3),  # native dimension\n    with width 1.8,\n    with behavior FollowLaneBehavior(4)\n'
        final, _ = finalize(raw)
        self.assertIn(b'# native dimension', final)
        self.assertIn(b'with behavior FollowLaneBehavior(4)', final)
        self.assertNotIn(b'Uniform(', final)
        self.assertEqual(finalize(final)[0], final)

    def test_missing_and_duplicate_ego_declarations_fail(self):
        for raw in (b'ego = Car\n', b'ego = new Car\nego = new Car\n',
                    b'ego = new Car with width 1, with width 2\n',
                    b'ego = new Car; other = new Car with blueprint MODEL\n',
                    b'ego = new Car with blueprint MODEL\n    npc = new Car at 3@4, with color "red"\n',
                    b'ego = new Car with blueprint MODEL\n    with width 1.8\n',
                    b'npc = new Car with blueprint MODEL\nego = new Car\nego = npc\n'):
            with self.assertRaises(ValidationError):
                finalize(raw)

    def test_legacy_fstring_map_remains_supported_without_evaluation(self):
        text = "Town = 'Town05'\nparam map = localPath(f'../maps/{Town}.xodr')\nparam carla_map = Town\n"
        self.assertEqual(map_declaration(text), ('Town05', 1))
        with self.assertRaises(ValueError):
            map_declaration(text.replace('{Town}', '{Town.lower()}'))
        with self.assertRaises(ValueError):
            map_declaration(text + "Town = 'Town05'\n")

    def test_literal_map_layout_keeps_original_source_bytes(self):
        text = "param map = localPath('../../maps/Town05.xodr')\nparam carla_map = 'Town05'\nego = new Car\n"
        self.assertEqual(map_declaration(text), ('Town05', 2))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            artifact = root / 'source.scenic'
            artifact.write_text(text)
            source_map = root / 'Town05.xodr'
            source_map.write_text('<OpenDRIVE/>')
            attempt = root / 'attempt'
            attempt.mkdir()
            staged, context = _prepare_carla_native_input(artifact, attempt, {
                'map_catalog': [{'path': str(source_map), 'sha256': sha256_file(source_map), 'bytes': source_map.stat().st_size}]
            })
            self.assertEqual(staged.read_bytes(), artifact.read_bytes())
            self.assertEqual((staged.parent / '../../maps/Town05.xodr').resolve(), Path(context['staged_catalog'][0]['path']))
        with self.assertRaises(ValueError):
            map_declaration(text.replace("carla_map = 'Town05'", "carla_map = 'Town03'"))
        with self.assertRaises(ValueError):
            map_declaration(text.replace('../../maps/', '../../../maps/'))


if __name__ == '__main__':
    unittest.main()
