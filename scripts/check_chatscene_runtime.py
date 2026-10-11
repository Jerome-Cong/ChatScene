#!/usr/bin/env python3
"""Execute the existing native runner on one diagnostic generation artifact.

The generation chain is explicitly unverified and cannot enter formal scoring.
Only the existing ego finalizer is applied; the generated scene is not repaired.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'benchmark/src'))
from bus_benchmark.generation import finalize_carla_ego_artifact
from bus_benchmark.jsonio import read_json,read_jsonl,write_json,sha256_file
from bus_benchmark.provenance import record_sha256
from bus_benchmark.runtime import validate_platform_runtime_config,execute_runtime_record


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config-dir',type=Path,required=True)
    p.add_argument('--generation-result',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--library',type=Path,default=ROOT/'query_lib/bus_ego_topdown_2d_dev_query_library_v0_2.jsonl')
    args=p.parse_args();result=read_json(args.generation_result)
    if result.get('disposition')!='generate' or not result.get('artifact'):raise ValueError('no successful diagnostic artifact')
    root=args.output.resolve();root.mkdir(parents=True,exist_ok=False)
    raw=Path(result['artifact']).read_bytes();(root/'artifact.raw.scenic').write_bytes(raw)
    finalized=finalize_carla_ego_artifact(raw)
    artifact=root/'artifact.final.scenic';artifact.write_bytes(finalized.final_bytes)
    (root/'ego-finalization.diff').write_bytes(finalized.diff_bytes)
    config=read_json(args.config_dir/'method.json');platform=read_json(args.config_dir/'platform.json');controller=read_json(args.config_dir/'controller.json')
    query=next(r for r in read_jsonl(args.library) if r['query_id']==result['query_id'])
    roster=read_json(args.config_dir/'development-roster.json')
    entry=next(r for r in roster['queries'] if r['query_id']==query['query_id'])
    response={k:(entry[k] if k in entry else query[k]) for k in ('query_id','intent_group_id','statistical_intent_cluster_id','surface_style','expected_support')}
    response.update(schema_version='0.1',method_id=config['method_id'],platform='carla',run_id='development-diagnostic-'+query['query_id'],repetition=0,
        disposition='generate',terminal_status='complete',config_sha256=record_sha256(config),implementation_bundle_sha256=config['implementation_bundle']['bundle_sha256'],
        artifact={'path':str(artifact),'sha256':sha256_file(artifact),'bytes':artifact.stat().st_size})
    chain={'method_id':config['method_id'],'platform':'carla','record_count':1,'manifest_sha256':sha256_file(args.generation_result),
           'implementation_bundle_sha256':config['implementation_bundle']['bundle_sha256'],'verified':False}
    write_json(root/'diagnostic-response.json',response)
    worker=validate_platform_runtime_config(platform,allow_draft=True)
    record=execute_runtime_record(response,root/'runtime',worker,controller,chain,allow_draft_controller=True,allow_draft_runtime=True)
    summary={'kind':'development_diagnostic_not_benchmark','source_kind':result.get('kind'),'generation_chain_verified':False,'query_id':query['query_id'],
             'compile_status':record['compile']['status'],'sv_trial_statuses':[v['status'] for v in record['sv_trials']],
             'ne_status':record['ne']['status'],'ne_error':record['ne'].get('error'),'ne_steps':record['ne'].get('steps'),
             'ne_simulated_seconds':record['ne'].get('simulated_seconds'),'selected_seed':record['selected_seed']}
    write_json(root/'summary.json',summary);print(json.dumps(summary,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
