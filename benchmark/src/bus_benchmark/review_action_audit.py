"""Source-bound, reversible batch-draft audit; never undo human confirmations."""
import copy
import datetime
import re
import uuid
from .errors import ValidationError
from .jsonio import canonical_json_bytes, read_json, sha256_bytes, write_json
from .paths import review_state_path


def _digest(value):
    return sha256_bytes(canonical_json_bytes(value))


def _root(session):
    return review_state_path(str(session.checkpoint_path)+'.batch-history')


def persist_draft_action(session, entries, kind, batch_id, *, source_subject_id=None, applicability=None):
    before={e['subject_id']:e for e in session._state['entries']}
    if source_subject_id is not None:
        if kind!='inheritance' or not any(e['subject_id']==source_subject_id and e['human_confirmed'] is True for e in entries):
            raise ValidationError('inheritance needs an explicitly confirmed source')
    changes=[{'before':copy.deepcopy(before[e['subject_id']]),'after':copy.deepcopy(e)} for e in entries if e['subject_id']!=source_subject_id and canonical_json_bytes(before[e['subject_id']])!=canonical_json_bytes(e)]
    checkpoint=session._checkpoint_core(entries);checkpoint['checkpoint_id']=_digest(checkpoint)
    session._validate_checkpoint(checkpoint)
    if any(c['before']['human_confirmed'] or c['after']['human_confirmed'] for c in changes):
        raise ValidationError('batch edits must remain unconfirmed drafts')
    core={'version':'1','artifact_type':'review_batch_draft_action','human_gold':False,'reviewer_id':session.reviewer_id,'packet_id':session.packet['packet_id'],'source_binding':session.packet['source_binding'],'kind':kind,'batch_id':batch_id,'source_checkpoint_id':session._state['checkpoint_id'],'target_checkpoint_id':checkpoint['checkpoint_id'],'nonce':uuid.uuid4().hex,'created_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'changes':changes,'source_subject_id':source_subject_id,'target_applicability':applicability or []}
    action_id=_digest(core)
    directory=_root(session)/action_id
    directory.mkdir(parents=True,exist_ok=False,mode=0o700)
    write_json(directory/'prepared.json',{**core,'action_id':action_id})
    # A failed CAS leaves a preparation record, never a completed undo target.
    session._persist(entries)
    write_json(directory/'committed.json',{'action_id':action_id,'checkpoint_id':session._state['checkpoint_id']})
    return action_id


def undo_batch(session, action_id):
    if not isinstance(action_id,str) or re.fullmatch(r'[0-9a-f]{64}',action_id) is None:
        raise ValidationError('invalid batch action ID')
    directory=_root(session)/action_id
    action=read_json(directory/'prepared.json');receipt=read_json(directory/'committed.json')
    if set(receipt)!={'action_id','checkpoint_id'} or receipt.get('action_id')!=action_id or receipt.get('checkpoint_id')!=action.get('target_checkpoint_id') or action.get('action_id')!=action_id or _digest({k:v for k,v in action.items() if k!='action_id'})!=action_id:
        raise ValidationError('batch audit content differs or is incomplete')
    if action.get('human_gold') is not False or any(action.get(k)!=expected for k,expected in (('packet_id',session.packet['packet_id']),('reviewer_id',session.reviewer_id),('source_binding',session.packet['source_binding']),('version','1'),('artifact_type','review_batch_draft_action'))):
        raise ValidationError('batch audit belongs to another source or reviewer')
    entries=copy.deepcopy(session._state['entries']);current={e['subject_id']:e for e in entries}
    restored=[];skipped=[];seen=set()
    for change in action['changes']:
        before,after=change['before'],change['after'];qid=before['subject_id']
        if qid in seen or qid not in current or after['subject_id']!=qid or before['human_confirmed'] is not False or after['human_confirmed'] is not False:
            raise ValidationError('invalid batch undo scope')
        seen.add(qid)
        if current[qid]['human_confirmed'] or canonical_json_bytes(current[qid])!=canonical_json_bytes(after):
            skipped.append(qid);continue
        current[qid].clear();current[qid].update(copy.deepcopy(before));restored.append(qid)
    if restored:
        core=session._checkpoint_core(entries);core['checkpoint_id']=_digest(core)
        session._validate_checkpoint(core)
        session._persist(entries)
    # Never mutate old audit files; repeated undo gets a separate receipt.
    write_json(directory/('undo-'+uuid.uuid4().hex+'.json'),{'action_id':action_id,'restored':restored,'skipped_edited_or_confirmed':skipped,'checkpoint_id':session._state['checkpoint_id'],'human_gold':False})
    return {'restored':restored,'skipped_edited_or_confirmed':skipped,'human_gold':False}
