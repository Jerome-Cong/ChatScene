"""Local runtime repair must preserve credential and sandbox boundaries."""
import os
import ast
import importlib.util
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

from bus_benchmark.adapters import AdapterOutcome
from bus_benchmark.adapters.chatscene import ChatSceneLegacyAdapter

ROOT=Path(__file__).resolve().parents[2]

class ChatSceneRuntimeSetupTests(unittest.TestCase):
    def test_method_import_never_overwrites_or_invents_credentials(self):
        code=compile((ROOT/'retrieve/architecture.py').read_text(),'architecture.py','exec')
        fake={name:types.ModuleType(name) for name in ('torch','transformers','openai')}
        for value in (None,'synthetic-test-credential'):
            with patch.dict('sys.modules',fake),patch.dict(os.environ,{},clear=True):
                if value is not None:os.environ['OPENAI_API_KEY']=value
                exec(code,{'__name__':'credential_probe'})
                self.assertEqual(os.environ.get('OPENAI_API_KEY'),value)

    def test_openai_compatible_backend_is_explicit_and_keeps_profile(self):
        code=compile((ROOT/'retrieve/architecture.py').read_text(),'architecture.py','exec')
        fake={name:types.ModuleType(name) for name in ('torch','transformers','openai')}
        completion=types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content='synthetic reply'))])
        client=Mock();client.chat.completions.create.return_value=completion
        fake['openai'].OpenAI=Mock(return_value=client)
        with patch.dict('sys.modules',fake):
            namespace={};exec(code,namespace)
            model=namespace['LLMChat']('DeepSeek-V4.1-Flash',backend='openai',temperature=0.2,timeout=7,max_retries=1)
            self.assertEqual(model.generate([{'role':'user','content':'synthetic query'}]),'synthetic reply')
        fake['openai'].OpenAI.assert_called_once_with(timeout=7,max_retries=1)
        self.assertEqual(client.chat.completions.create.call_args.kwargs['temperature'],0.2)
        self.assertEqual(client.chat.completions.create.call_args.kwargs['model'],'DeepSeek-V4.1-Flash')
        fake['openai'].OpenAI.reset_mock()
        namespace['LLMChat']('gpt-4o')
        fake['openai'].OpenAI.assert_called_once_with()

    def test_preflight_honors_explicit_backend_over_model_name(self):
        from bus_benchmark.generation_preflight import _legacy_execution_checks
        with patch.dict(os.environ,{},clear=True),patch('bus_benchmark.generation_preflight._has_loopback_listener',return_value=False):
            for model,backend,expected in [('gpt2','transformers','not_required_by_configured_model'),('DeepSeek-V4.1-Flash','openai','credential_unavailable')]:
                checks=_legacy_execution_checks({'argv':['--model',model,'--llm-backend',backend]},None)
                check=next(c for c in checks if c['check_id']=='llm_credential_contract')
                self.assertEqual(check['reason_code'],expected)

    def test_selected_port_is_persisted_in_raw_scenic_header(self):
        tree=ast.parse((ROOT/'retrieve/retrieve.py').read_text())
        assignments=[n for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='head' for t in n.targets)]
        for port in (2000,2100):
            namespace={'port_ip':port}
            for statement in assignments:exec(compile(ast.Module(body=[statement],type_ignores=[]),'header.py','exec'),namespace)
            self.assertTrue(namespace['head'].startswith('param port = '+str(port)+'\n'))
            self.assertIn("../maps/{Town}.xodr",namespace['head'])

    def test_ego_finalization_keeps_last_clause_termination(self):
        from bus_benchmark.generation import finalize_carla_ego_artifact,verify_carla_ego_finalization
        base='model scenic.simulators.carla.model\nEGO_MODEL = "vehicle.lincoln.mkz_2017"\nego = Car at 0@0,\n    with blueprint EGO_MODEL'
        for tail,expected in (('\nparam OTHER = 1\n','with width 2.10\nparam'),(',\n    with speed 0\nparam OTHER = 1\n','with width 2.10,\n    with speed')):
            raw=base+tail;result=finalize_carla_ego_artifact(raw.encode()).final_bytes.decode()
            self.assertIn(expected,result)
            verify_carla_ego_finalization(raw,result)

    @unittest.skipUnless(importlib.util.find_spec('scenic') and importlib.util.find_spec('carla'), 'requires the native ChatScene runtime environment')
    def test_native_scenic_behavior_can_start_without_generated_guard_methods(self):
        import scenic
        map_path=ROOT/'safebench/scenario/scenario_data/scenic_data/maps/Town05.xodr'
        scenic.scenarioFromString('param map = '+repr(str(map_path))+'\nmodel scenic.simulators.carla.model\nego = Car at 0@0, with regionContainedIn None\n')
        from bus_benchmark.runtime_worker import _carla_controller_classes
        controller=_carla_controller_classes({})()
        agent=types.SimpleNamespace()
        controller._start(agent)
        self.assertIs(controller._agent,agent)
        self.assertIsNone(controller.checkPreconditions(agent))
        self.assertIsNone(controller.checkInvariants(agent))
        controller._stop()

    def test_relocated_stdlib_is_used_only_when_available(self):
        for portable in (False,True):
            with self.subTest(portable=portable),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);source=root/'source';source.mkdir();(source/'retrieve').mkdir()
                (source/'retrieve/retrieve.py').write_text('print("synthetic test")')
                env=root/'python-env';(env/'bin').mkdir(parents=True)
                (env/'pyvenv.cfg').write_text('include-system-site-packages = false\n')
                (env/'lib/python3.8/site-packages').mkdir(parents=True)
                executable=env/'bin/python-frozen';executable.write_bytes(b'fixture');executable.chmod(0o755)
                sandbox=root/'bwrap';sandbox.write_bytes(b'fixture');sandbox.chmod(0o755)
                if portable:
                    lib=env/'lib/python3.8';(lib/'encodings').mkdir(parents=True);(lib/'lib-dynload').mkdir()
                    (lib/'encodings/__init__.py').write_text('');(lib/'os.py').write_text('')
                adapter=ChatSceneLegacyAdapter(source_root=str(source),snapshot_paths=['retrieve'],entrypoint='retrieve/retrieve.py',
                    python_executable=str(executable),python_environment_root=str(env),sandbox_executable=str(sandbox),argv=[],artifact_path='output.scenic',failure_artifact_path=None,timeout_seconds=5)
                with patch('bus_benchmark.adapters.chatscene._run_process',return_value=AdapterOutcome(stdout=b'',stderr=b'',exit_code=0,timed_out=False)) as run:
                    adapter.generate('synthetic query',root/'run')
                self.assertEqual(run.call_count,1)
                overrides=run.call_args.kwargs['environment_overrides']
                if portable:self.assertEqual(overrides['PYTHONHOME'],'/run/chatscene-benchmark/python-env')
                else:self.assertNotIn('PYTHONHOME',overrides)
