"""Read-only validation of historical browser assignments and checkpoints.

Only the known model/compiler formats are accepted. UI bytes may differ, but
original query/oracle sources must reconstruct every task exactly. This module
never edits an old file and never treats hashes as proof of human identity.
"""
import copy

from .errors import ValidationError
from .human_workflow import export_query_review_bundle, validate_query_review_task_response
from .jsonio import canonical_json_bytes, sha256_bytes
from .review_forms import _form_from_response, response_from_form
from .review_model import _attested_form, content_hash
from .review_session import QueryReviewSession
from .review_wire import wire_hash


def semantic_binding(binding):
    keys=('model_version','guide_version','guide_sha256','registry_version','registry_sha256','compiler_version')
    return {key:binding.get(key) for key in keys}


def draft_content(draft):
    return {k:v for k,v in draft.items() if k not in ('receipts','status','human_gold')}


def validate_historical_browser(assignment, backup, library_source, oracle_source):
    from .review_packet import browser_snapshot
    required={'packet_version','ui_version','reviewer_id','human_gold','source_binding','bundle_sha256','items','dictionary','guide','practice','ui_sha256','packet_id'}
    if not isinstance(assignment,dict) or not required<=set(assignment) or set(assignment)-required-{'proposal_profile','migration'}:
        raise ValidationError('historical assignment has unknown structure')
    if assignment['packet_version']!='1' or assignment['ui_version']!='1' or assignment['human_gold'] is not False or not isinstance(assignment['guide'],str):
        raise ValidationError('historical packet format is not supported')
    if assignment['packet_id']!=wire_hash({k:v for k,v in assignment.items() if k!='packet_id'}):
        raise ValidationError('historical packet content differs')
    bundle=export_query_review_bundle(library_source,oracle_source,reviewer_id=assignment['reviewer_id'])
    if assignment['source_binding']!=bundle['reviewer_packet']['source_binding'] or assignment['bundle_sha256']!=wire_hash(bundle):
        raise ValidationError('historical source binding differs from original sources')
    tasks=bundle['reviewer_packet']['tasks']
    if not isinstance(assignment['items'],list) or len(assignment['items'])!=len(tasks):
        raise ValidationError('historical assignment coverage differs')
    for item,task in zip(assignment['items'],tasks):
        if not isinstance(item,dict) or not {'task','proposal','initial_draft'}<=set(item) or set(item)-{'task','proposal','initial_draft','revision_proposals','surface_diff'} or canonical_json_bytes(item.get('task'))!=canonical_json_bytes(task):
            raise ValidationError('historical task differs from trusted original sources')
        proposal=item['proposal']
        if not isinstance(proposal,dict):raise ValidationError('historical proposal is not an object')
        binding=proposal.get('task_binding',{})
        if not isinstance(binding,dict) or set(binding)!={'task_id','subject_id','subject_sha256','task_sha256','query_sha256','model_version','registry_version','guide_version','compiler_version','guide_sha256','registry_sha256'}:
            raise ValidationError('historical task binding has unknown fields')
        if set(proposal)!={'artifact_type','model_version','task_binding','human_gold','form','provenance','proposal_id'} or proposal['artifact_type']!='review_machine_proposal' or proposal['model_version']!='1' or proposal['human_gold'] is not False:
            raise ValidationError('historical proposal is malformed')
        if proposal['proposal_id']!=content_hash({k:v for k,v in proposal.items() if k!='proposal_id'}):
            raise ValidationError('historical proposal content differs')
        expected={'task_id':task['task_id'],'subject_id':task['subject_id'],'subject_sha256':task['subject_sha256'],'task_sha256':content_hash(task),'query_sha256':content_hash(task['query_text'])}
        if any(binding.get(k)!=v for k,v in expected.items()) or binding.get('model_version')!='1' or binding.get('compiler_version')!='1':
            raise ValidationError('historical proposal source/compiler differs')
        if binding.get('guide_sha256')!=sha256_bytes(assignment['guide'].encode('utf-8')):
            raise ValidationError('historical guide content differs')
        if proposal['form']!=_form_from_response(task['machine_recommendation']['recommended_response']) or proposal['provenance']!={'source':'legacy_machine_recommendation'}:
            raise ValidationError('historical machine proposal does not match source recommendation')
    keys={'artifact_type','packet_version','packet_id','reviewer_id','generation','entries','human_gold','backup_sha256'}
    if not isinstance(backup,dict) or set(backup)!=keys or backup['artifact_type']!='browser_review_backup' or backup['human_gold'] is not False:
        raise ValidationError('historical browser backup is malformed')
    if backup['packet_id']!=assignment['packet_id'] or backup['packet_version']!='1' or backup['reviewer_id']!=assignment['reviewer_id'] or type(backup['generation']) is not int or backup['generation']<0:
        raise ValidationError('historical backup packet/reviewer/version differs')
    if backup['backup_sha256']!=wire_hash({k:v for k,v in backup.items() if k!='backup_sha256'}) or not isinstance(backup['entries'],list) or len(backup['entries'])!=len(tasks):
        raise ValidationError('historical backup integrity or coverage differs')
    result={}
    if 'migration' in assignment:
        from .review_migration import validate_migration_ledger
        validate_migration_ledger(assignment,assignment['migration'])
    for item,entry in zip(assignment['items'],backup['entries']):
        task=item['task'];proposal=item['proposal']
        if not isinstance(entry,dict) or set(entry)!={'model_version','task_binding','proposal_id','reviewer_id','form','revision','edit_sources','receipts','issues','status','human_gold'} or entry['human_gold'] is not False:
            raise ValidationError('historical subject is malformed')
        if entry['task_binding']!=proposal['task_binding'] or entry['model_version']!='1' or entry['proposal_id']!=proposal['proposal_id'] or entry['reviewer_id']!=assignment['reviewer_id'] or type(entry['revision']) is not int or entry['revision']<0:
            raise ValidationError('historical subject source/reviewer differs')
        if entry['status'] not in ('draft','submitted','deferred','requires_source_fix') or not isinstance(entry['form'],dict) or any(not isinstance(entry[k],list) for k in ('edit_sources','issues','receipts')):
            raise ValidationError('historical subject state is malformed')
        form=entry['form']
        if set(form)!={'required_check_decisions','atom_decisions','cpd_decision','added_atoms_json','notes'} or not isinstance(form['atom_decisions'],list) or not isinstance(form['required_check_decisions'],dict) or not isinstance(form['cpd_decision'],dict) or not isinstance(form['notes'],str) or not isinstance(form['added_atoms_json'],str):
            raise ValidationError('historical form structure is malformed')
        digest=wire_hash(browser_snapshot(assignment['packet_id'],task,proposal,entry,item.get('revision_proposals'),item.get('surface_diff')))
        known={'atom:'+a['atom_id'] for a in task['oracle_draft']['atoms']}|{'support','cpd','additions','notes'};covered=set()
        for receipt in entry['receipts']:
            from .review_migration import validate_receipt_origin
            validate_receipt_origin(assignment,task,entry,receipt,ledger_validated=True)
            if receipt['content_sha256']!=digest or receipt['reviewer_id']!=assignment['reviewer_id'] or type(receipt['revision']) is not int or receipt['revision']!=entry['revision']:
                raise ValidationError('historical confirmation receipt differs')
            units=receipt['covered_units']
            if not isinstance(units,list) or not units or any(not isinstance(x,str) for x in units) or len(set(units))!=len(units) or not set(units)<=known:
                raise ValidationError('historical confirmation scope differs')
            covered.update(units)
        if entry['status']=='submitted':
            if entry['issues'] or covered!=known:raise ValidationError('historical submission was not completely confirmed')
            try:
                response=response_from_form(task,_attested_form(entry['form']))
            except (KeyError,TypeError,ValueError) as exc:
                raise ValidationError('historical confirmed form is malformed') from exc
            validate_query_review_task_response(task,response,reviewer_id=entry['reviewer_id'],require_confirmation=True)
        elif covered==known or entry['status'] in ('deferred','requires_source_fix') and covered:
            raise ValidationError('historical status contradicts confirmation receipts')
        result[task['subject_id']]={'task':task,'draft':copy.deepcopy(entry),'confirmed':entry['status']=='submitted','semantic_binding':semantic_binding(proposal['task_binding']),'dictionary':copy.deepcopy(assignment['dictionary']),'origin_confirmation_sha256':wire_hash(entry['receipts'])}
    return result


def validate_legacy_checkpoint(checkpoint,library_source,oracle_source,reviewer_id):
    if not isinstance(checkpoint,dict) or checkpoint.get('schema_version')!='0.2' or checkpoint.get('human_gold') is not False or checkpoint.get('formal_submission') is not False:
        raise ValidationError('legacy checkpoint version/flags are unsupported; original preserved')
    bundle=export_query_review_bundle(library_source,oracle_source,reviewer_id=reviewer_id)
    # Use the existing validator without constructor I/O or creating a lock next
    # to a historical file. Its context is explicitly bound to original sources.
    validator=object.__new__(QueryReviewSession)
    validator.packet=bundle['reviewer_packet'];validator.reviewer_id=reviewer_id
    validator.split=checkpoint['split'];validator.agent_artifact_id=checkpoint['agent_review_artifact_id']
    validator.tasks_by_id={t['subject_id']:t for t in validator.packet['tasks']}
    checked=validator._validate_checkpoint(checkpoint)
    return {entry['subject_id']:{'task':validator.tasks_by_id[entry['subject_id']],'draft':{'form':copy.deepcopy(entry['form']),'revision':0,'edit_sources':[],'issues':[],'receipts':[],'status':'draft'},'confirmed':entry['human_confirmed'],'semantic_binding':None,'origin_confirmation_sha256':content_hash(entry)} for entry in checked['entries']}
