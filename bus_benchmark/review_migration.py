"""Maintainer-owned, source-aware migration with explicit carried confirmations.

A migration authorization lives in the trusted assignment, never in a browser
upload. Original files are archived read-only. It does not authenticate people.
"""
import copy
import re
import tempfile
from pathlib import Path

from .errors import ValidationError
from .human_workflow import validate_query_review_task_response
from .jsonio import canonical_json_bytes, read_json, sha256_bytes, strict_json_object_bytes, write_json
from .paths import review_state_path
from .review_forms import response_from_form
from .review_history import draft_content, semantic_binding, validate_historical_browser, validate_legacy_checkpoint
from .review_model import _attested_form, content_hash, confirmation_projection, _assert_confirmable
from .review_wire import wire_hash, ensure_browser_form


_LEDGER_KEYS={'version','source_kind','source_artifact_sha256','source_packet_id','subjects','removed_subject_ids','migration_id'}
_SUBJECT_KEYS={'outcome','reason','old_subject_sha256','new_subject_sha256','old_semantic_binding','new_semantic_binding','old_dictionary_sha256','new_dictionary_sha256','changed_paths','origin_confirmation_sha256','draft_content_sha256','previous_query_text','previous_atoms','previous_form'}


def _changed_paths(before,after,prefix=''):
    if isinstance(before,dict) and isinstance(after,dict):
        result=[]
        for key in sorted(set(before)|set(after)):
            path=prefix+'/'+key.replace('~','~0').replace('/','~1')
            result.extend([path] if key not in before or key not in after else _changed_paths(before[key],after[key],path))
        return result
    if isinstance(before,list) and isinstance(after,list):
        result=[]
        for i in range(max(len(before),len(after))):
            result.extend([prefix+'/'+str(i)] if i>=len(before) or i>=len(after) else _changed_paths(before[i],after[i],prefix+'/'+str(i)))
        return result
    return [] if type(before) is type(after) and before==after else [prefix or '/']


def validate_migration_ledger(packet, ledger):
    if not isinstance(ledger,dict) or set(ledger)!=_LEDGER_KEYS or ledger['version']!='1' or ledger['source_kind'] not in ('browser','legacy_checkpoint'):
        raise ValidationError('unsupported migration ledger')
    if ledger['migration_id']!=wire_hash({k:v for k,v in ledger.items() if k!='migration_id'}):
        raise ValidationError('migration ledger content differs')
    if not isinstance(ledger['source_artifact_sha256'],str) or re.fullmatch(r'[0-9a-f]{64}',ledger['source_artifact_sha256']) is None or not isinstance(ledger['source_packet_id'],str):
        raise ValidationError('migration source provenance is malformed')
    items={i['task']['subject_id']:i for i in packet['items']}
    dictionary_sha=content_hash(packet['dictionary'])
    if not isinstance(ledger['subjects'],dict) or set(ledger['subjects'])!=set(items) or not isinstance(ledger['removed_subject_ids'],list):
        raise ValidationError('migration ledger subject coverage differs')
    if any(not isinstance(qid,str) for qid in ledger['removed_subject_ids']) or len(set(ledger['removed_subject_ids']))!=len(ledger['removed_subject_ids']) or set(ledger['removed_subject_ids'])&set(items):
        raise ValidationError('migration removed-subject inventory is inconsistent')
    for qid,record in ledger['subjects'].items():
        item=items[qid]
        if not isinstance(record,dict) or set(record)!=_SUBJECT_KEYS or record['outcome'] not in ('carried_confirmation','preserved_draft','requires_review','new_subject') or record['new_subject_sha256']!=item['task']['subject_sha256'] or record['new_semantic_binding']!=semantic_binding(item['proposal']['task_binding']):
            raise ValidationError('migration subject binding differs')
        if not isinstance(record['draft_content_sha256'],str) or re.fullmatch(r'[0-9a-f]{64}',record['draft_content_sha256']) is None:
            raise ValidationError('migration draft digest is missing')
        if not isinstance(record['reason'],str) or not isinstance(record['previous_atoms'],list) or record['previous_query_text'] is not None and not isinstance(record['previous_query_text'],str) or record['previous_form'] is not None and not isinstance(record['previous_form'],dict):
            raise ValidationError('migration historical display data is malformed')
        if record['new_dictionary_sha256']!=dictionary_sha or not isinstance(record['changed_paths'],list) or any(not isinstance(path,str) for path in record['changed_paths']):
            raise ValidationError('migration dictionary/change inventory differs')
        if record['outcome']=='carried_confirmation':
            if ledger['source_kind']!='browser' or record['old_subject_sha256']!=record['new_subject_sha256'] or record['old_semantic_binding']!=record['new_semantic_binding'] or record['old_dictionary_sha256']!=record['new_dictionary_sha256'] or record['changed_paths']:
                raise ValidationError('changed source or guidance cannot carry confirmation')
            if record['previous_query_text']!=item['task']['query_text'] or record['previous_atoms']!=item['task']['oracle_draft']['atoms']:
                raise ValidationError('carried confirmation refers to changed visible source')
            if not isinstance(record['origin_confirmation_sha256'],str) or re.fullmatch(r'[0-9a-f]{64}',record['origin_confirmation_sha256']) is None:
                raise ValidationError('carried confirmation origin is missing')


def validate_receipt_origin(packet, task, draft, receipt, *, ledger_validated=False):
    basic={'action','content_sha256','covered_units','reviewer_id','revision'}
    if not isinstance(receipt,dict):raise ValidationError('malformed confirmation receipt')
    if receipt.get('action')=='explicit_confirm':
        if set(receipt)!=basic:raise ValidationError('explicit receipt has unknown fields')
        return
    if receipt.get('action')!='carried_confirmation' or set(receipt)!=basic|{'migration_id','origin_confirmation_sha256'}:
        raise ValidationError('unknown confirmation origin')
    ledger=packet.get('migration')
    if not ledger:raise ValidationError('browser cannot invent a migration authorization')
    if not ledger_validated:
        validate_migration_ledger(packet,ledger)
    record=ledger['subjects'][task['subject_id']]
    if record['outcome']!='carried_confirmation' or receipt['migration_id']!=ledger['migration_id'] or receipt['origin_confirmation_sha256']!=record['origin_confirmation_sha256'] or wire_hash(draft_content(draft))!=record['draft_content_sha256']:
        raise ValidationError('carried confirmation differs from the trusted migration')
    if canonical_json_bytes(draft['form'])!=canonical_json_bytes(record['previous_form']):
        raise ValidationError('carried confirmation cannot change the original reviewed form')
    expected={'atom:'+a['atom_id'] for a in task['oracle_draft']['atoms']}|{'support','cpd','additions','notes'}
    if not isinstance(receipt['covered_units'],list) or set(receipt['covered_units'])!=expected or len(receipt['covered_units'])!=len(expected):
        raise ValidationError('carried confirmation must cover the entire unchanged subject')


def _snapshot_sources(values, directory):
    inputs={};archives={}
    for name,value in values.items():
        if isinstance(value,(str,Path)):
            payload=Path(value).read_bytes();path=directory/(name+'.jsonl');path.write_bytes(payload)
            inputs[name]=path;archives[name+'.jsonl']=payload
        else:
            records=list(value);payload=canonical_json_bytes(records)
            inputs[name]=copy.deepcopy(records);archives[name+'.records.json']=payload
    return inputs,archives


def _prior_lineage(assignment_path, assignment):
    if 'migration' not in assignment:return {}
    root=Path(assignment_path).resolve().parent.parent
    report_path=root/'migration_report.json';archive=root/'originals'
    if not report_path.is_file() or not archive.is_dir() or archive.is_symlink():
        raise ValidationError('prior migration archive is missing; provide the complete prior migration directory')
    report=read_json(report_path)
    if report.get('migration_id')!=assignment['migration']['migration_id']:
        raise ValidationError('prior migration report differs from its assignment')
    files=report.get('input_files')
    if not isinstance(files,dict) or files.get('original_submission.json')!=assignment['migration']['source_artifact_sha256'] or not isinstance(report.get('lineage_files',{}),dict):
        raise ValidationError('prior migration archive manifest is incomplete')
    if assignment['migration']['source_kind']=='browser' and 'original_assignment.json' not in files:
        raise ValidationError('prior browser assignment archive is missing')
    for stem in ('old_library','old_oracle','new_library','new_oracle'):
        if sum(name in files for name in (stem+'.jsonl',stem+'.records.json'))!=1:
            raise ValidationError('prior migration source archive is incomplete')
    blobs={}
    for name,digest in report.get('input_files',{}).items():
        if not re.fullmatch(r'(?:original_(?:assignment|submission)\.json|(?:old|new)_(?:library|oracle)(?:\.jsonl|\.records\.json))',name):
            raise ValidationError('prior migration archive path is not allowed')
        path=archive/name
        if path.is_symlink() or not path.is_file():raise ValidationError('prior migration original is missing or symlinked')
        payload=path.read_bytes()
        if sha256_bytes(payload)!=digest:raise ValidationError('prior migration original differs from its recorded bytes')
        blobs[digest]=payload
    for digest,name in report.get('lineage_files',{}).items():
        if not re.fullmatch(r'[0-9a-f]{64}',digest) or name!=digest+'.blob':raise ValidationError('prior lineage reference is invalid')
        path=archive/'lineage'/name
        if (archive/'lineage').is_symlink() or path.is_symlink() or not path.is_file():raise ValidationError('prior lineage file is unavailable')
        payload=path.read_bytes()
        if sha256_bytes(payload)!=digest:raise ValidationError('prior lineage bytes differ')
        blobs[digest]=payload
    return blobs


def _form_compatible(task,form):
    if not isinstance(form,dict) or set(form)!={'required_check_decisions','atom_decisions','cpd_decision','added_atoms_json','notes'}:
        return False
    if not isinstance(form['atom_decisions'],list) or [d.get('atom_id') for d in form['atom_decisions'] if isinstance(d,dict)]!=[a['atom_id'] for a in task['oracle_draft']['atoms']]:
        return False
    try:
        if not isinstance(form['notes'],str):return False
        ensure_browser_form(form)
    except (KeyError,TypeError,ValueError,ValidationError):
        return False
    return True


def _migrate_records(old_records,new_packet,source_kind,source_packet_id,source_sha):
    entries=[];subjects={};new_dictionary_sha=content_hash(new_packet['dictionary'])
    for item in new_packet['items']:
        task,proposal=item['task'],item['proposal'];qid=task['subject_id'];entry=copy.deepcopy(item['initial_draft'])
        old=old_records.get(qid);outcome='new_subject';reason='new_subject';old_sha=None;old_binding=None;origin=None
        previous_query=None;previous_atoms=[];previous_form=None;old_dictionary_sha=None;changed_paths=[]
        if old is not None:
            old_sha=old['task']['subject_sha256'];old_binding=old['semantic_binding'];origin=old['origin_confirmation_sha256']
            previous_query=old['task']['query_text'];previous_atoms=copy.deepcopy(old['task']['oracle_draft']['atoms']);previous_form=copy.deepcopy(old['draft']['form'])
            old_dictionary_sha=content_hash(old['dictionary']) if old.get('dictionary') is not None else None
            same_source=old_sha==task['subject_sha256'];same_guidance=old_binding==semantic_binding(proposal['task_binding']) and old_dictionary_sha==new_dictionary_sha
            changed_paths=_changed_paths({'query_record':old['task']['query_record'],'oracle_draft':old['task']['oracle_draft']},{'query_record':task['query_record'],'oracle_draft':task['oracle_draft']})
            changed_paths+=_changed_paths(old_binding,semantic_binding(proposal['task_binding']),'/guidance')
            changed_paths+=_changed_paths(old.get('dictionary'),new_packet['dictionary'],'/dictionary')
            editable=same_source and _form_compatible(task,previous_form)
            if editable:entry['form']=copy.deepcopy(previous_form)
            entry['revision']=old['draft'].get('revision',0)+1
            entry['edit_sources']=copy.deepcopy(old['draft'].get('edit_sources',[]))+[{'revision':entry['revision'],'origin':'inherited','migration_source_sha256':source_sha}]
            entry['receipts']=[]
            if same_source and same_guidance and editable:
                entry['issues']=copy.deepcopy(old['draft'].get('issues',[]))
                entry['status']=old['draft'].get('status','draft')
                if entry['status'] in ('deferred','requires_source_fix') and not entry['issues']:
                    entry['issues']=[{'code':'unspecified_defer','reason':'系统提示：历史暂存缺少原因，请补全后继续。'}]
                if old['confirmed']:
                    entry['status']='draft'
                    try:
                        _assert_confirmable(task,proposal,entry)
                    except (ValidationError,KeyError,TypeError,ValueError):
                        outcome='requires_review';reason='current_validation_requires_review'
                    else:
                        outcome='carried_confirmation';reason='same_subject_and_guidance'
                else:
                    outcome='preserved_draft';reason='same_subject_draft_preserved'
            else:
                outcome='requires_review'
                reason='source_changed' if not same_source else 'legacy_guide_unbound' if old_binding is None else 'guidance_changed' if not same_guidance else 'legacy_form_not_browser_editable'
            if outcome=='requires_review':
                entry['status']='deferred'
                entry['issues']=[{'code':'migration_review_required','reason':reason+'；原编辑已只读保留，请对照当前源完整重审。'}]+entry['issues']
        record={'outcome':outcome,'reason':reason,'old_subject_sha256':old_sha,'new_subject_sha256':task['subject_sha256'],'old_semantic_binding':old_binding,'new_semantic_binding':semantic_binding(proposal['task_binding']),'old_dictionary_sha256':old_dictionary_sha,'new_dictionary_sha256':new_dictionary_sha,'changed_paths':changed_paths,'origin_confirmation_sha256':origin,'draft_content_sha256':wire_hash(draft_content(entry)),'previous_query_text':previous_query,'previous_atoms':previous_atoms,'previous_form':previous_form}
        subjects[qid]=record;entries.append(entry)
    core={'version':'1','source_kind':source_kind,'source_artifact_sha256':source_sha,'source_packet_id':source_packet_id,'subjects':subjects,'removed_subject_ids':sorted(set(old_records)-set(subjects))}
    ledger={**core,'migration_id':wire_hash(core)}
    new_packet=copy.deepcopy(new_packet);new_packet['migration']=ledger
    new_packet['packet_id']=wire_hash({k:v for k,v in new_packet.items() if k!='packet_id'})
    validate_migration_ledger(new_packet,ledger)
    from .review_packet import browser_snapshot
    for item,entry in zip(new_packet['items'],entries):
        record=subjects[item['task']['subject_id']]
        if record['outcome']=='carried_confirmation':
            projection=confirmation_projection(item['task'],item['proposal'],entry)
            entry['receipts']=[{'action':'carried_confirmation','content_sha256':wire_hash(browser_snapshot(new_packet['packet_id'],item['task'],item['proposal'],entry,item.get('revision_proposals'),item.get('surface_diff'))),'covered_units':[u['id'] for u in projection['units']],'reviewer_id':new_packet['reviewer_id'],'revision':entry['revision'],'migration_id':ledger['migration_id'],'origin_confirmation_sha256':record['origin_confirmation_sha256']}]
            entry['status']='submitted'
    return new_packet,entries,ledger


def migrate_review(*, old_submission_path, old_library, old_oracle, new_library, new_oracle, output_dir, old_assignment_path=None, reviewer_id=None):
    from .review_packet import build_packet, _write_packet, validate_browser_submission
    output=review_state_path(output_dir)
    if output.exists():raise ValidationError('migration output exists; original files were not changed')
    submission_bytes=Path(old_submission_path).read_bytes()
    old_submission=strict_json_object_bytes(submission_bytes,'historical review')
    archives={'original_submission.json':submission_bytes};lineage={}
    with tempfile.TemporaryDirectory(prefix='review-migration-') as directory:
        inputs,source_archives=_snapshot_sources({'old_library':old_library,'old_oracle':old_oracle,'new_library':new_library,'new_oracle':new_oracle},Path(directory));archives.update(source_archives)
        if old_assignment_path is not None:
            raw=Path(old_assignment_path).read_bytes();assignment=strict_json_object_bytes(raw,'historical assignment');archives['original_assignment.json']=raw
            if reviewer_id is not None and assignment.get('reviewer_id')!=reviewer_id:raise ValidationError('migration cannot reassign another reviewer')
            reviewer=assignment['reviewer_id'];kind='browser';source_id=assignment['packet_id'];generation=old_submission.get('generation',0)
            old=validate_historical_browser(assignment,old_submission,inputs['old_library'],inputs['old_oracle'])
            lineage=_prior_lineage(old_assignment_path,assignment)
            suggestions=assignment.get('proposal_profile')=='literal_review_rules_v1'
        else:
            if not reviewer_id:raise ValidationError('legacy migration requires the assigned reviewer ID')
            reviewer=reviewer_id;kind='legacy_checkpoint';source_id=old_submission.get('packet_id');generation=0;suggestions=False
            old=validate_legacy_checkpoint(old_submission,inputs['old_library'],inputs['old_oracle'],reviewer)
        packet=build_packet(inputs['new_library'],inputs['new_oracle'],reviewer,with_suggestions=suggestions)
        packet,entries,ledger=_migrate_records(old,packet,kind,source_id,sha256_bytes(submission_bytes))
        core={'artifact_type':'browser_review_backup','packet_version':packet['packet_version'],'packet_id':packet['packet_id'],'reviewer_id':reviewer,'generation':generation+1,'entries':entries,'human_gold':False}
        backup={**core,'backup_sha256':wire_hash(core)}
        validate_browser_submission(packet,backup,inputs['new_library'],inputs['new_oracle'])
        output.mkdir(parents=True,exist_ok=False,mode=0o700)
        archive=output/'originals';archive.mkdir(mode=0o700)
        for name,payload in archives.items():
            path=archive/name;path.write_bytes(payload);path.chmod(0o400)
        if lineage:
            (archive/'lineage').mkdir(mode=0o700)
            for digest,payload in lineage.items():
                path=archive/'lineage'/(digest+'.blob');path.write_bytes(payload);path.chmod(0o400)
        _write_packet(packet,output/'packet')
        write_json(output/'progress.json',backup)
        report={'migration_id':ledger['migration_id'],'human_gold':False,'source_kind':kind,'outcomes':{qid:record['outcome'] for qid,record in ledger['subjects'].items()},'removed_subject_ids':ledger['removed_subject_ids'],'input_files':{name:sha256_bytes(payload) for name,payload in archives.items()},'lineage_files':{digest:digest+'.blob' for digest in lineage},'originals_read_only':True,'notes':'Carried confirmations are administrative rebinding of prior explicit confirmations, not new human actions. External human custody remains required.'}
        write_json(output/'migration_report.json',report)
    return {'output':str(output),'migration_id':ledger['migration_id'],'subjects':len(entries),'carried_confirmations':sum(e['status']=='submitted' for e in entries),'requires_review':sum(r['outcome']=='requires_review' for r in ledger['subjects'].values()),'human_gold':False}
