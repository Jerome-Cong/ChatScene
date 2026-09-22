"""Source-locatable issue exports; no transmission or source modification."""
from .errors import ValidationError
from .jsonio import read_json,write_json
from .paths import review_state_path
from .review_history import validate_historical_browser
from .review_wire import wire_hash


def export_issues(assignment_path,submission_path,library_source,oracle_source,output_dir):
    assignment=read_json(assignment_path);submission=read_json(submission_path)
    records=validate_historical_browser(assignment,submission,library_source,oracle_source)
    issues=[]
    for qid,value in records.items():
        task,draft=value['task'],value['draft']
        pending=draft['issues'] or ([{'code':'unspecified_defer','reason':'系统提示：该历史暂存记录缺少原因，需补全。'}] if draft['status'] in ('deferred','requires_source_fix') else [])
        for issue in pending:
            if isinstance(issue,str):issue={'code':'legacy_unstructured','reason':issue}
            if not isinstance(issue,dict) or not isinstance(issue.get('reason'),str) or not issue['reason'].strip():
                raise ValidationError('issue reason is malformed; original backup was not changed')
            atom_ids=issue.get('atom_ids',[issue['atom_id']] if issue.get('atom_id') else [])
            if not isinstance(atom_ids,list) or any(a not in {x['atom_id'] for x in task['oracle_draft']['atoms']} for a in atom_ids):
                raise ValidationError('issue references another subject atom')
            core={'packet_id':assignment['packet_id'],'reviewer_id':assignment['reviewer_id'],'subject_id':qid,'source_query_sha256':task['source_query_sha256'],'source_oracle_sha256':task['source_oracle_sha256'],'query_text':task['query_text'],'workflow_status':draft['status'],'reason_code':issue.get('code','legacy_unstructured'),'reason':issue['reason'],'atom_ids':atom_ids,'notes':draft['form'].get('notes',''),'draft_form':draft['form'],'origin':'machine' if issue.get('code')=='machine_unresolved' else 'system' if issue.get('code')=='unspecified_defer' else 'reviewer','human_gold':False}
            issues.append({**core,'issue_id':wire_hash(core)})
    package={'artifact_type':'review_source_issue_package','version':'1','packet_id':assignment['packet_id'],'reviewer_id':assignment['reviewer_id'],'issues':issues,'human_gold':False}
    output=review_state_path(output_dir);output.parent.mkdir(parents=True,exist_ok=True)
    try:output.mkdir(mode=0o700)
    except FileExistsError as exc:raise ValidationError('issue output exists; no overwrite') from exc
    write_json(output/'issues.json',package)
    return {'output':str(output/'issues.json'),'issue_count':len(issues),'human_gold':False}
