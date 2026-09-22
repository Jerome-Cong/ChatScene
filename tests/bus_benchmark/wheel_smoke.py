"""Run with an installed wheel's Python -I, outside the checkout.

Inputs are maintainer-owned test fixtures, never bundled in the distribution.
Synthetic receipts exercise transport only; this does not produce human gold.
"""
import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

from bus_benchmark.paths import PACKAGE_ROOT, asset_path
from bus_benchmark.jsonio import read_json, write_json
from bus_benchmark.review_packet import browser_snapshot
from bus_benchmark.review_wire import wire_hash


def main():
    library, oracle = map(lambda x: str(Path(x).resolve()), sys.argv[1:3])
    assert 'site-packages' in PACKAGE_ROOT.parts, PACKAGE_ROOT
    assert not importlib.util.find_spec('ipywidgets')
    assert not importlib.util.find_spec('IPython')
    for name in ('app.js', 'app.css', 'template.html', 'guide_zh.md', 'practice.json', 'revision_proposals.schema.json'):
        assert asset_path('review', name).read_bytes()
    assert (PACKAGE_ROOT/'query_review_workbench.css').read_bytes()
    from bus_benchmark.query_review_workbench import response_from_form
    from bus_benchmark.review_forms import response_from_form as compiler
    assert response_from_form is compiler

    from bus_benchmark.errors import ValidationError
    from bus_benchmark.paths import review_state_path
    try:
        review_state_path(PACKAGE_ROOT/'forbidden-state')
    except ValidationError:
        pass
    else:
        raise AssertionError('installation state was allowed')
    cli = Path(sys.executable).parent/'bus-benchmark'
    def run(*args):
        result = subprocess.run([str(cli), *args], check=True, text=True, capture_output=True)
        return json.loads(result.stdout)
    paths = run('paths')
    run('review','export','--library',library,'--oracle',oracle,'--reviewer','synthetic-wheel-test','--output','packet')
    packet = read_json('packet/assignment.json')
    entries = []
    for item in packet['items']:
        entry = copy.deepcopy(item['initial_draft'])
        entry['receipts'] = [{'action':'explicit_confirm','content_sha256':wire_hash(browser_snapshot(packet['packet_id'],item['task'],item['proposal'],entry,item.get('revision_proposals'),item.get('surface_diff'))), 'covered_units':['atom:'+a['atom_id'] for a in item['task']['oracle_draft']['atoms']]+['support','cpd','additions','notes'], 'reviewer_id':packet['reviewer_id'],'revision':entry['revision']}]
        entry['status'] = 'submitted'
        entries.append(entry)
    core = dict(artifact_type='browser_review_backup',packet_version=packet['packet_version'],packet_id=packet['packet_id'],reviewer_id=packet['reviewer_id'],generation=1,entries=entries,human_gold=False)
    write_json('synthetic-progress.json',{**core,'backup_sha256':wire_hash(core)})
    args = ('--library',library,'--oracle',oracle,'--assignment','packet/assignment.json','--submission','synthetic-progress.json')
    validated = run('review','validate',*args)
    assert validated['subjects_submitted'] == len(entries)
    assert validated['human_gold'] is False
    imported = run('review','import',*args,'--output','imported')
    assert imported['human_gold'] is False
    # A stale confirmed edit must still be rejected by the installed CLI.
    backup=read_json('synthetic-progress.json');backup['entries'][0]['form']['notes']='changed after confirmation'
    backup['backup_sha256']=wire_hash({k:v for k,v in backup.items() if k!='backup_sha256'})
    write_json('synthetic-progress.json',backup)
    rejected=subprocess.run([str(cli),'review','validate',*args],capture_output=True,text=True)
    assert rejected.returncode != 0
    print(json.dumps({'package_root':str(PACKAGE_ROOT),'paths':paths,'subjects':len(entries),'human_gold':False,'stale_confirmation_rejected':True,'notebook_dependencies_installed':False},ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
