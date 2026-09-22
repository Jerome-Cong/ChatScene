"""Complete lexical surface diff and conservative inheritance applicability.

Categories are navigation hints, never a semantic equivalence classifier.
Unknown differences trigger a full review; shared atom IDs are not evidence.
"""
import difflib
import re
from .human_workflow import atom_semantic_projection
from .jsonio import canonical_json_bytes, sha256_bytes
from .review_presentation import quick_confirmation_issues

def content_hash(value):
    return sha256_bytes(canonical_json_bytes(value))

TOKENS = re.compile(r"\d+(?:\.\d+)?|[A-Za-z]+(?:[-'][A-Za-z]+)*|[^\s]")
CUES = {
    'cardinality': {'one','two','three','four','five','multiple','several','many','some','each','all','a','an','optional'},
    'negation': {'no','not','never','without','neither','nor'},
    'roles': {'bus','buses','ego','cyclist','cyclists','bicycle','bicycles','pedestrian','pedestrians','vehicle','vehicles','car','cars','passenger','passengers','motorcycle','motorcycles','e-bike'},
    'relations': {'left','right','ahead','behind','oncoming','opposing','adjacent','inside','outside','between','front','rear','near','beside','through'},
    'triggers': {'if','when','unless','until','whenever','once'},
    'order': {'before','after','then','while','meanwhile','simultaneously','first','second','finally'},
    'modifiers': {'suddenly','sudden','fast','slow','slowly','normal','normally','may','might','could','must','should','intends','continues','late','early','only','at','least','most'},
    'numbers_units': {'m','km','s','ms','meters','metres','seconds','km/h','m/s'},
}
LABELS = {'cardinality':'数量','negation':'否定','roles':'对象与角色','relations':'空间关系','triggers':'触发条件','order':'顺序/同时','modifiers':'修饰语与强弱','numbers_units':'数值与单位','unclassified':'其他文字（需完整核对）'}


def surface_diff(source_task, target_task):
    source=source_task['query_text'];target=target_task['query_text']
    left=list(TOKENS.finditer(source));right=list(TOKENS.finditer(target))
    changes=[];categories=set()
    matcher=difflib.SequenceMatcher(a=[m.group() for m in left],b=[m.group() for m in right],autojunk=False)
    for tag,a,b,c,d in matcher.get_opcodes():
        if tag=='equal':continue
        before=[m.group() for m in left[a:b]];after=[m.group() for m in right[c:d]]
        kinds=set()
        for token in before+after:
            found={name for name,words in CUES.items() if token.lower() in words}
            if re.fullmatch(r'\d+(?:\.\d+)?',token):found.update(('numbers_units','cardinality'))
            kinds.update(found or {'unclassified'})
        categories.update(kinds)
        changes.append({'operation':tag,'source_tokens':before,'target_tokens':after,'categories':sorted(kinds),'source_span':[left[a].start(),left[b-1].end()] if a<b else None,'target_span':[right[c].start(),right[d-1].end()] if c<d else None})
    same_text=' '.join(source.split())==' '.join(target.split())
    a={x['atom_id']:x for x in source_task['oracle_draft']['atoms']};b={x['atom_id']:x for x in target_task['oracle_draft']['atoms']}
    shared=sorted(set(a)&set(b))
    changed=[aid for aid in shared if atom_semantic_projection(a[aid])!=atom_semantic_projection(b[aid])]
    atom_changes=[{'kind':'changed','source':atom_semantic_projection(a[aid]),'target':atom_semantic_projection(b[aid])} for aid in changed]
    atom_changes += [{'kind':'source_only','source':atom_semantic_projection(a[aid]),'target':None} for aid in sorted(set(a)-set(b))]
    atom_changes += [{'kind':'target_only','source':None,'target':atom_semantic_projection(b[aid])} for aid in sorted(set(b)-set(a))]
    unknown=bool(quick_confirmation_issues(list(a.values()),source) or quick_confirmation_issues(list(b.values()),target))
    return {'version':'1','source_subject_id':source_task['subject_id'],'target_subject_id':target_task['subject_id'],'source_query_sha256':content_hash(source),'target_query_sha256':content_hash(target),'source_text':source,'target_text':target,'changes':changes,'categories':sorted(categories),'category_labels':[LABELS[x] for x in sorted(categories)],'identical_text':same_text,'full_review_required':not same_text or bool(atom_changes) or unknown,'unknown_semantics':unknown,'atom_changes':atom_changes,'source_only_atom_ids':sorted(set(a)-set(b)),'target_only_atom_ids':sorted(set(b)-set(a)),'changed_common_atom_ids':changed,'same_atom_id_is_sufficient':False}


def inheritance_assessment(source_task,target_task,reviewed_atoms=None):
    diff=surface_diff(source_task,target_task);reasons=[]
    for key in ('intent_group_id','dataset_split'):
        if source_task['query_record'].get(key)!=target_task['query_record'].get(key):reasons.append('different_'+key)
    if not diff['identical_text']:reasons.append('surface_text_differs_full_review')
    for field in ('expected_support','acceptable_response'):
        if source_task['oracle_draft'].get(field)!=target_task['oracle_draft'].get(field):reasons.append('different_'+field)
    if diff['changed_common_atom_ids']:reasons.append('shared_atom_semantics_differ')
    if diff['source_only_atom_ids'] or diff['target_only_atom_ids']:reasons.append('source_target_requirements_differ')
    atoms=reviewed_atoms if reviewed_atoms is not None else source_task['oracle_draft']['atoms']
    if quick_confirmation_issues(list(atoms),source_task['query_text']) or quick_confirmation_issues(target_task['oracle_draft']['atoms'],target_task['query_text']):reasons.append('unknown_semantics')
    def policy(task):return {k:v for k,v in task['oracle_draft']['cpd_policy'].items() if k!='decision_status'}
    return {'can_inherit':not reasons,'reasons':reasons,'diff':diff,'cpd_compatible':policy(source_task)==policy(target_task),'target_applicability':[{'atom_id':a['atom_id'],'target_query_sha256':diff['target_query_sha256'],'basis':'whitespace_normalized_query_text_match'} for a in atoms] if not reasons else []}
