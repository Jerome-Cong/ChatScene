#!/usr/bin/env python3
"""One non-scoring development invocation through the real ChatScene adapter.

Credentials are read into memory from an explicit dotenv file and never stored
in the configuration or diagnostic output. This is not a formal repetition.
"""
import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'benchmark/src'))
from dotenv import dotenv_values
from bus_benchmark.adapters.chatscene import adapter_from_method_config
from bus_benchmark.generation import validate_implementation_bundle
from bus_benchmark.jsonio import read_json,read_jsonl,write_json


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',type=Path,required=True)
    p.add_argument('--api-env',type=Path,required=True)
    p.add_argument('--library',type=Path,default=ROOT/'query_lib/bus_ego_topdown_2d_dev_query_library_v0_2.jsonl')
    p.add_argument('--query-id')
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--help-only',action='store_true',help='exercise the real sandbox/imports without calling the model')
    args=p.parse_args();config=read_json(args.config)
    validate_implementation_bundle(config,formal=False)
    values=dotenv_values(args.api_env,interpolate=False)
    for key in ('OPENAI_API_KEY','OPENAI_BASE_URL'):
        if not values.get(key):raise ValueError('missing required API configuration field: '+key)
        os.environ[key]=values[key]
    argv=config['adapter']['argv'];model=argv[argv.index('--model')+1]
    if values.get('OPENAI_MODEL')!=model:raise ValueError('model in configuration differs from the API profile; prepare a new configuration')
    records=read_jsonl(args.library)
    query=next((r for r in records if r['query_id']==args.query_id),None) if args.query_id else records[0]
    if query is None:raise ValueError('query ID is absent from the given public library')
    output=args.output.resolve();output.mkdir(parents=True,exist_ok=False)
    if args.help_only:config['adapter']['argv']=['--help']
    invocation=Path(tempfile.mkdtemp(prefix='chatscene-isolated-generation-'))/'invocation'
    outcome=adapter_from_method_config(config).generate(query['query_text'],invocation)
    def redact(raw):
        value=raw.decode('utf-8',errors='replace') if isinstance(raw,bytes) else str(raw or '')
        for key in ('OPENAI_API_KEY','OPENAI_BASE_URL'):
            if values.get(key):value=value.replace(values[key],'[REDACTED]')
        return value
    (output/'stdout.redacted.txt').write_text(redact(outcome.stdout))
    (output/'stderr.redacted.txt').write_text(redact(outcome.stderr))
    result={'kind':'development_diagnostic_not_benchmark','query_id':query['query_id'],'model':model,
            'help_only':args.help_only,'invocation_dir':str(invocation),'exit_code':outcome.exit_code,'timed_out':outcome.timed_out,
            'disposition':outcome.disposition,'error':redact(outcome.error),
            'artifact':str(outcome.artifact_path) if outcome.artifact_path else None,'human_gold':False}
    write_json(output/'result.json',result);print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if outcome.exit_code==0 and (args.help_only or outcome.disposition=='generate') else 1


if __name__=='__main__':
    raise SystemExit(main())
