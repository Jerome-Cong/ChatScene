#!/usr/bin/env python3
"""Prepare host-bound DEVELOPMENT configs without modifying historical drafts.

Run from the method Python environment after installing its dependencies. Outputs
are machine-local artifacts, not a formal freeze or evidence of model success.
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'benchmark/src'))
from bus_benchmark.generation import (probe_python_environment, python_environment_control_paths,
    generation_harness_paths, implementation_bundle_sha256, validate_implementation_bundle,
    _iter_snapshot_source_files)
from bus_benchmark.jsonio import read_json, write_json, sha256_file
from bus_benchmark.paths import PACKAGE_ROOT
from bus_benchmark.runtime import runtime_tree_binding, validate_platform_runtime_config, _validate_controller_config


def binding(path):
    path = Path(path).absolute()
    if path.is_symlink() or not path.is_file():
        raise ValueError('expected a regular non-symlink file: '+str(path))
    return {'path':str(path),'sha256':sha256_file(path),'bytes':path.stat().st_size}


def environment(executable):
    executable=Path(executable).absolute();root=executable.parent.parent
    return {'schema_version':'0.1','decision_status':'draft','runtime_kind':'python',
            'executable':binding(executable),**probe_python_environment(executable),
            'python_environment_root':str(root),
            'python_environment_control_files':[binding(p) for p in python_environment_control_paths(root)]}


def prepare(server_root, method_python, output, port=2100, api_env=None):
    server_root=Path(server_root).resolve();method_python=Path(method_python).absolute();output=Path(output).resolve()
    if not 1024<=port<=65533:raise ValueError('port must allow the CARLA RPC/streaming ports')
    if output.exists():raise FileExistsError('choose a fresh output directory; existing configs are never overwritten')
    sandbox=Path(shutil.which('bwrap') or '/missing-bwrap').resolve()
    egg=server_root/'PythonAPI/carla/dist/carla-0.9.13-py3.8-linux-x86_64.egg'
    server=server_root/'CarlaUE4/Binaries/Linux/CarlaUE4-Linux-Shipping'
    for file in (method_python,sandbox,egg,server):binding(file)
    output.mkdir(parents=True)
    method_env=environment(method_python)
    harness_python=Path(sys.executable).absolute()
    harness_env=environment(harness_python)
    write_json(output/'method-environment.json',method_env)
    write_json(output/'harness-environment.json',harness_env)
    method=read_json(ROOT/'benchmark_configs/methods/chatscene_carla_draft.json')
    adapter=method['adapter']
    adapter.update(source_root=str(ROOT),python_executable=str(method_python),python_environment_root=str(method_python.parent.parent),sandbox_executable=str(sandbox),python_path_entries=[binding(egg)])
    adapter['argv'] += ['--port_ip',str(port)]
    if api_env is not None:
        from dotenv import dotenv_values
        values=dotenv_values(api_env,interpolate=False)
        model=values.get('OPENAI_MODEL')
        if not model or not model.strip():raise ValueError('API configuration is missing OPENAI_MODEL')
        adapter['argv'][adapter['argv'].index('--model')+1]=model
        temperature=float(values.get('OPENAI_COMPAT_TEMPERATURE') or '0')
        timeout=float(values.get('OPENAI_COMPAT_TIMEOUT_S') or '60')
        retries=int(values.get('OPENAI_COMPAT_MAX_RETRIES') or '2')
        if not 0<=temperature<=2 or not 0<timeout<=600 or not 0<=retries<=10:raise ValueError('invalid API profile bounds')
        adapter['argv'] += ['--llm-backend','openai','--temperature',str(temperature),'--api-timeout',str(timeout),'--api-max-retries',str(retries)]
    model=method_python.parent.parent/'share/models/sentence-t5-large'
    if model.is_dir():
        if not (model/'modules.json').is_file() or not (model/'pytorch_model.bin').is_file():
            raise ValueError('local retrieval model is incomplete; finish the download first')
        adapter['argv'] += ['--embedding-model','/run/chatscene-benchmark/python-env/share/models/sentence-t5-large']
    files={method_python,harness_python,sandbox,egg,*generation_harness_paths(),*_iter_snapshot_source_files(adapter)}
    if model.is_dir():
        files.update(p for p in model.rglob('*') if p.is_file() and '.cache' not in p.relative_to(model).parts)
        source_record=model.parent/'sentence-t5-large-source.json'
        if source_record.is_file():files.add(source_record)
    files.update(Path(b['path']) for env in (method_env,harness_env) for b in env['python_environment_control_files'])
    bundle={'status':'draft','bundle_id':'chatscene-local-development','environment_manifest':binding(output/'method-environment.json'),
            'harness_environment_manifest':binding(output/'harness-environment.json'),'files':[binding(p) for p in sorted(files,key=str)]}
    bundle['bundle_sha256']=implementation_bundle_sha256(bundle);method['implementation_bundle']=bundle
    validate_implementation_bundle(method,formal=False)
    write_json(output/'method.json',method)
    worker=PACKAGE_ROOT/'runtime_worker.py'
    runtime=read_json(ROOT/'benchmark_configs/platforms/carla_runtime_draft.json')
    runtime['carla_endpoint']['port']=port
    runtime['worker'].update(interpreter=str(method_python),interpreter_sha256=sha256_file(method_python),worker=str(worker),worker_sha256=sha256_file(worker),
                             environment={'PYTHONPATH':str(egg),'PYTHON_EGG_CACHE':str(output/'python-eggs')})
    runtime['server_binary']=binding(server)
    maps=ROOT/'safebench/scenario/scenario_data/scenic_data/maps'
    print('Binding CARLA/Scenic trees for the existing runtime validator...',flush=True)
    runtime['runtime_dependencies']={'environment_manifest':binding(output/'method-environment.json'),'python_path_entries':[binding(egg)],
        'source_trees':[runtime_tree_binding(server_root,'carla_server_distribution'),runtime_tree_binding(ROOT/'Scenic/src','scenic_editable_source')],
        'map_catalog':[binding(p) for p in sorted(maps.iterdir(),key=lambda p:p.name) if p.suffix in ('.xodr','.snet')]}
    validate_platform_runtime_config(runtime,allow_draft=True)
    write_json(output/'platform.json',runtime)
    controller=read_json(ROOT/'benchmark_configs/platforms/carla_controller_draft.json')
    controller['implementation'].update(source_path=str(worker),source_sha256=sha256_file(worker))
    _validate_controller_config(controller,platform="carla",allow_draft=True)
    write_json(output/'controller.json',controller)
    roster=read_json(ROOT/'benchmark_artifacts/drafts/chatscene_carla_dev_roster_draft.json')
    library=ROOT/'query_lib/bus_ego_topdown_2d_dev_query_library_v0_2.jsonl'
    roster.update(library_path=str(library),library_sha256=sha256_file(library))
    write_json(output/'development-roster.json',roster)
    report={'status':'development_only','output':str(output),'model':adapter['argv'][adapter['argv'].index('--model')+1],
            'credentials_included':False,'port':port,'formal_freeze':False,
            'remaining_formal_gate':'complete Python runtime-tree closure, model assets, human freeze and Judge admission'}
    write_json(output/'preparation.json',report)
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server-root',type=Path,required=True)
    parser.add_argument('--method-python',type=Path,default=ROOT/'chatscene/bin/python-frozen')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--port',type=int,default=2100)
    parser.add_argument('--api-env',type=Path,help='read only model/profile settings; credentials are never written into configs')
    args=parser.parse_args()
    print(json.dumps(prepare(args.server_root,args.method_python,args.output,args.port,args.api_env),ensure_ascii=False,indent=2))
