"""Visible CPD judgements, separate from maintainer technical approval."""
import copy
import json
from .errors import ValidationError
from .review_wire import wire_hash

ORIGIN = 'cpd_semantics'
CHOICES = ('', 'allow', 'restricted', 'uncertain')


def policy_for(task, form):
    return json.loads(form['cpd_decision']['replacement_policy_json']) if form['cpd_decision']['verdict'] == 'revise' else task['oracle_draft']['cpd_policy']


def review_basis(task, form):
    return copy.deepcopy({'subject_sha256':task['subject_sha256'], 'policy':policy_for(task,form), 'atom_decisions':form['atom_decisions'], 'added_atoms_json':form['added_atoms_json']})


def question_ids(task, form):
    return ['dimension:'+str(i) for i,_ in enumerate(policy_for(task,form).get('dimensions',[]))]+['coverage']


def latest_review(draft):
    return next((e for e in reversed(draft['edit_sources']) if isinstance(e,dict) and e.get('origin') == ORIGIN), None)


def validate_semantic_review(task, draft, *, required=False):
    entry = latest_review(draft)
    if entry is None:
        if required:raise ValidationError('CPD semantic questions must be explicitly answered')
        return
    if set(entry) != {'origin','revision','basis','answers'} or type(entry['revision']) is not int or not 0 <= entry['revision'] <= draft['revision'] or not isinstance(entry['answers'],list):
        raise ValidationError('malformed CPD semantic review')
    ids=[]
    for answer in entry['answers']:
        if not isinstance(answer,dict) or set(answer) != {'id','choice','reason'} or not isinstance(answer['id'],str) or answer['choice'] not in CHOICES or not isinstance(answer['reason'],str):
            raise ValidationError('malformed CPD semantic answer')
        ids.append(answer['id'])
    if len(ids)!=len(set(ids)):raise ValidationError('duplicate CPD semantic question')
    if required:
        if wire_hash(entry['basis']) != wire_hash(review_basis(task,draft['form'])) or ids != question_ids(task,draft['form']):
            raise ValidationError('CPD semantic answers are stale or incomplete')
        if any(a['choice'] != 'allow' for a in entry['answers']):
            raise ValidationError('CPD semantic questions are unresolved')


def technical_template(assignment, backup):
    return {'artifact_type':'cpd_technical_review','version':'1','packet_id':assignment['packet_id'],
            'backup_sha256':backup['backup_sha256'],'reviewer_id':'','approved':False,
            'policy_sha256':{item['task']['subject_id']:wire_hash(policy_for(item['task'],draft['form'])) for item,draft in zip(assignment['items'],backup['entries'])}}


def validate_technical_review(approval, assignment, backup):
    expected=technical_template(assignment,backup)
    if not isinstance(approval,dict) or set(approval)!=set(expected) or approval.get('approved') is not True or not isinstance(approval.get('reviewer_id'),str) or not approval['reviewer_id'].strip():
        raise ValidationError('CPD technical review must be explicitly approved by a named maintainer')
    if any(approval[k]!=v for k,v in expected.items() if k not in ('reviewer_id','approved')):
        raise ValidationError('CPD technical review belongs to another packet, backup or policy')
