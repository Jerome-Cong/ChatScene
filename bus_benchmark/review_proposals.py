"""Conservative offline, source-bound semantic revision proposals.

Rules only propose changes supported by literal local evidence. Unrecognized
paraphrases, role ambiguity and metadata-only claims remain unresolved. This is
review assistance, never the query-only method generator or an oracle repair.
"""
import copy
import re
from collections import Counter
from pathlib import Path

from .errors import ValidationError
from .jsonio import sha256_file
from .review_forms import _normalize_human_atom
from .review_model import content_hash, task_binding
from .paths import asset_path
from .jsonio import read_json
from .review_registry import presentation_warnings

PROFILE = "literal_review_rules_v1"
ACTOR_WORDS = {
    "bicycle": r"(?:cyclists?|bicycles?)", "e_bike": r"(?:e[- ]bikes?|electric bicycles?)",
    "motorcycle": r"(?:motorcycles?|motorcyclists?)", "pedestrian": r"pedestrians?",
    "passenger": r"passengers?", "social_vehicle": r"(?:cars?|vehicles?)",
}
COUNTS = {"no":"0", "zero":"0", "one":"1", "a":"1", "an":"1", "two":"2", "three":"3", "four":"4", "five":"5"}
RELATIONS = {
    "ahead_of_ego": r"\b(?:ahead of|in front of) the (?:ego )?bus\b",
    "behind_ego": r"\bbehind the (?:ego )?bus\b",
    "nonmotor_space": r"\bin the (?:cycle|non[- ]motor(?:ized)?) lane\b",
    "opposing_lane": r"\b(?:in|through) the (?:opposing|oncoming) (?:lane|approach)\b",
    "adjacent_lane": r"\bin the adjacent lane\b",
}
EVENT_WORDS = {
    "cyclist_straight_crossing": r"\bcontinues straight through (?:the )?(?:cycle )?crossing\b",
    "oncoming_motorcycle_through": r"\bcontinues straight(?: at normal speed)?\b",
    "pedestrian_crossing": r"\bpedestrian (?:crosses|is crossing|starts crossing)\b",
    "pedestrian_starts_crossing": r"\bpedestrian (?:starts|begins) crossing\b",
    "pedestrian_reaches_refuge": r"\bpedestrian reaches (?:the )?refuge\b",
    "pedestrian_waits_on_median": r"\bpedestrian waits (?:on|at) the (?:median|refuge)\b",
    "following_vehicle_deceleration": r"\bfollowing (?:vehicle|car) (?:decelerates|slows down)\b",
    "following_vehicle_queue_present": r"\bfollowing vehicles (?:are waiting|are queued|form a queue)\b",
    "vehicle_queue_present": r"\bvehicles (?:are waiting|are queued)\b",
    "passenger_alights": r"\bpassenger (?:alights|gets off)\b",
    "red_signal_phase": r"\bred (?:light|signal|phase)\b",
    "green_signal_phase": r"\bgreen (?:light|signal|phase)\b",
}
EVENT_ACTOR_TYPES = {"cyclist_straight_crossing":"bicycle", "oncoming_motorcycle_through":"motorcycle", "pedestrian_crossing":"pedestrian", "pedestrian_starts_crossing":"pedestrian", "pedestrian_reaches_refuge":"pedestrian", "pedestrian_waits_on_median":"pedestrian", "passenger_alights":"passenger", "following_vehicle_deceleration":"social_vehicle", "following_vehicle_queue_present":"social_vehicle", "vehicle_queue_present":"social_vehicle"}


def _evidence(text, start, end):
    return {"source":"query_text", "start":start, "end":end, "quote":text[start:end], "query_sha256":content_hash(text)}


def _matches(pattern, text):
    return list(re.finditer(pattern, text, re.I))


def _uncertain_prefix(text, start):
    boundary=max(text.rfind('.',0,start),text.rfind(';',0,start))+1
    return bool(re.search(r'\b(?:not|no|never|unless|if|when|without|may|might|could|at least|at most|more than|less than|fewer than|up to|about|around|approximately)\b',text[boundary:start],re.I))


def _unique_event(event, text):
    matches = _matches(EVENT_WORDS[event], text) if event in EVENT_WORDS else []
    return matches[0] if len(matches)==1 else None


def _revision(atom, **changes):
    value = copy.deepcopy(atom)
    value.update(changes)
    return value


def _result(atom):
    return {"source_atom_id":atom["atom_id"], "source_atom_sha256":content_hash(atom), "status":"unresolved", "reason_code":"no_literal_evidence_rule", "machine_rationale":"本规则未能建立精确原文依据，需人工核对；未自动改变要求。", "evidence":[], "operation":"none", "replacement_atoms":[], "semantic_kind":"undetermined"}


def _set(result, text, match, *, status="supported", code, rationale, operation="retain", replacements=(), kind="undetermined"):
    result.update(status=status, reason_code=code, machine_rationale=rationale, evidence=[_evidence(text,*match)], operation=operation, replacement_atoms=list(replacements), semantic_kind=kind)


def _actor_rule(atom, task, result):
    args=atom["arguments"];text=task["query_text"];kind=args.get("type")
    peers=[a for a in task["oracle_draft"]["atoms"] if a["predicate"]=="actor_role_count" and a["arguments"].get("type")==kind]
    if kind not in ACTOR_WORDS or len(peers)!=1:
        result.update(reason_code="actor_role_ambiguous",machine_rationale="同类型对象或 ego/其他公交无法唯一绑定，不从角色名猜测。")
        return
    pattern=r"\b(?P<optional>(?:(?:there\s+)?(?:may|might|could)\s+be\s+))?(?P<count>no|zero|one|two|three|four|five|a|an|\d+)\s+(?:(?:oncoming|through|nearby|waiting|following|adjacent|crossing)\s+){0,3}"+ACTOR_WORDS[kind]+r"\b"
    matches=_matches(pattern,text)
    if len(matches)!=1 or len(_matches(r'\b'+ACTOR_WORDS[kind]+r'\b',text))!=1 or _uncertain_prefix(text,matches[0].start() if matches else 0) or re.search(r'\b(?:or|either|whether)\b',text,re.I):
        result.update(reason_code="count_or_binding_not_explicit",machine_rationale="没有唯一的数量与类型原文片段；角色名不能证明数量、极性或方位。")
        return
    m=matches[0];count=COUNTS.get(m['count'].lower(),m['count'])
    if count.isdigit():count=str(int(count))
    optional=bool(m['optional'])
    if optional and count=='0':
        result.update(reason_code='optional_absence_not_a_prohibition',machine_rationale='可能没有不等于禁止出现，不能自动合并可选与否定。')
        return
    if count=='0' and not (re.search(r'\bthere (?:is|are)\s*$',text[:m.start()],re.I) or re.match(r'\s+(?:is|are)\s+(?:present|nearby)\b',text[m.end():],re.I) or text[m.end():].strip(' .!?')==''):
        result.update(reason_code='negative_count_scope_unclear',machine_rationale='否定可能修饰动作或必要性，不能直接当作参与者不存在。')
        return
    desired=copy.deepcopy(args);desired['count']='optional' if optional else count
    polarity='absent' if count=='0' else 'present'
    layer='forbidden' if count=='0' else 'permitted' if optional else ('surface_required' if atom['layer']=='permitted' else atom['layer'])
    changed=desired!=args or polarity!=atom['polarity'] or layer!=atom['layer']
    _set(result,text,m.span(),status='proposed' if changed else 'supported',code='literal_cardinality_and_modality',rationale='原文明确数量/可选性与参与者类型；保留 role 作为已有对象标识，不据此新增空间关系。',operation='modify' if changed else 'retain',replacements=[_revision(atom,arguments=desired,polarity=polarity,layer=layer)] if changed else [])


def _spatial_rule(atom, task, result):
    text=task['query_text'];role=atom['arguments'].get('role')
    actors=[a for a in task['oracle_draft']['atoms'] if a['predicate']=='actor_role_count' and a['arguments'].get('role')==role]
    result.update(reason_code='role_name_is_not_spatial_evidence',machine_rationale='角色名不能证明空间关系；需同一句中明确的参与者与方位。')
    if len(actors)!=1 or actors[0]['arguments'].get('type') not in ACTOR_WORDS:return
    kind=actors[0]['arguments']['type']
    if sum(a['predicate']=='actor_role_count' and a['arguments'].get('type')==kind for a in task['oracle_draft']['atoms'])!=1:return
    noun=ACTOR_WORDS[kind];found=[]
    for relation,pattern in RELATIONS.items():
        for m in _matches(pattern,text):
            start=max(text.rfind('.',0,m.start()),text.rfind(';',0,m.start()))+1
            prefix=text[start:m.start()]
            mentions=[(mention.end(),actor_type) for actor_type,pattern in {**ACTOR_WORDS,'bus':r'buses|bus'}.items() for mention in _matches(r'\b(?:'+pattern+r')\b',prefix)]
            nearest=max(mentions)[1] if mentions else None
            if nearest==kind and len(_matches(noun,prefix))==1 and not re.search(r'\b(?:not|no|never|may|might|could|if|when|unless)\b',prefix,re.I):
                found.append((relation,start,m.end()))
    if not found:return
    relations=sorted(set(x[0] for x in found));replacements=[]
    for relation in relations:
        args=copy.deepcopy(atom['arguments']);args['relation']=relation;replacements.append(_revision(atom,arguments=args))
    changed=len(relations)!=1 or relations[0]!=atom['arguments'].get('relation')
    _set(result,text,(min(x[1] for x in found),max(x[2] for x in found)),status='proposed' if changed else 'supported',code='literal_actor_spatial_clause',rationale='同一句原文中明确出现对象类型与相对区域；仅提议这些明示关系。',operation=('split' if len(relations)>1 else 'modify') if changed else 'retain',replacements=replacements if changed else [])


def _temporal_rule(atom, task, result):
    text=task['query_text'];a=atom['arguments'];left=_unique_event(a.get('first'),text);right=_unique_event(a.get('second'),text)
    result.update(reason_code='list_order_is_not_temporal_evidence',machine_rationale='事件列表顺序不是原文先后关系；无明确连接词和两端绑定时保持疑问。')
    if left is None or right is None or left.end()>right.start():return
    between=text[left.end():right.start()]
    if _uncertain_prefix(text,left.start()) or re.search(r'\b(?:not|never|unless|without|may|might|could)\b',between,re.I):return
    span=(left.start(),right.end())
    if re.search(r'\b(?:while|at the same time as)\b',between,re.I):
        replacement=_revision(atom,predicate='parallel_group',arguments={'events':[a['first'],a['second']]})
        _set(result,text,span,status='proposed',code='explicit_parallel_not_list_order',rationale='原文明示同时发生，建议把机器列表推导的 before 改为并行事件组。',operation='modify',replacements=[replacement])
    elif re.search(r'\bafter\b',between,re.I):
        replacement=_revision(atom,arguments={'first':a['second'],'second':a['first']})
        _set(result,text,span,status='proposed',code='explicit_order_reversal',rationale='原文 after 表明当前机器边方向相反，建议交换先后端点。',operation='modify',replacements=[replacement])
    elif re.search(r'\b(?:then|before)\b',between,re.I):
        _set(result,text,span,code='explicit_order',rationale='两个事件由原文明示的先后连接词连接；仍需人工核对作用范围。')


def _event_rule(atom, task, result):
    text=task['query_text'];event=atom['arguments'].get('event');m=_unique_event(event,text)
    result.update(reason_code='event_state_trigger_binding_unresolved',machine_rationale='标签可能描述事件、持续状态或触发，未匹配到完整文字时不补充角色/触发。')
    if m is None:return
    prefix=text[:m.start()]
    trigger=re.search(r'\b(?:if|when)\s+([^,.;]+),\s*(?:the\s+)?$',prefix,re.I)
    if trigger and _uncertain_prefix(text,trigger.start()):return
    if not trigger and re.search(r'\b(?:if|when|unless)\b',text,re.I):return
    if trigger and 'trigger' not in atom['arguments']:
        args=copy.deepcopy(atom['arguments']);args['trigger']=trigger.group(1)
        _set(result,text,(trigger.start(),m.end()),status='proposed',code='literal_trigger_clause',rationale='将原文明示的 if/when 前提作为触发文本保留，不新增未明示的角色或结束条件。',operation='modify',replacements=[_revision(atom,arguments=args)],kind='triggered_event')
    elif not _uncertain_prefix(text,m.start()):
        kind='state' if event.endswith('_present') or '_waits_' in event else 'event'
        actor_type=EVENT_ACTOR_TYPES.get(event)
        actors=[a for a in task['oracle_draft']['atoms'] if a['predicate']=='actor_role_count' and a['arguments'].get('type')==actor_type]
        boundary=max(text.rfind('.',0,m.start()),text.rfind(';',0,m.start()))+1
        mentions=[(x.end(),x.start(),t) for t,pattern in {**ACTOR_WORDS,'bus':r'buses|bus'}.items() for x in _matches(r'\b(?:'+pattern+r')\b',text[boundary:m.end()])]
        nearest=max(mentions) if mentions else None
        if actor_type and (nearest is None or nearest[2]!=actor_type or len(actors)>1 or sum(t==actor_type for _,_,t in mentions)>1):
            result.update(reason_code='event_actor_binding_unresolved',machine_rationale='事件文字的主体与当前角色不能唯一对应，不能把另一个对象的动作绑定过来。')
            return
        if len(actors)==1 and nearest and nearest[2]==actor_type and not any(k in atom['arguments'] for k in ('actor','role','subject')):
            args=copy.deepcopy(atom['arguments']);args['actor']=actors[0]['arguments']['role']
            _set(result,text,(min(m.start(),boundary+nearest[1]),m.end()),status='proposed',code='literal_single_actor_binding',rationale='同一句的对象类型与事件文字明确对应，建议补全唯一的既有角色绑定；不从角色名补空间关系或结束条件。',operation='modify',replacements=[_revision(atom,arguments=args)],kind=kind)
        else:
            _set(result,text,m.span(),code='literal_event_or_state',rationale='该文字匹配'+('持续状态' if kind=='state' else '变化事件')+'；分类仅为审阅解释，不把持续状态伪装为发生顺序。',kind=kind)


def _risk_rule(atom, task, result):
    text=task['query_text'];matches=_matches(r'\b(low|medium|high)[ -]risk\b',text)
    result.update(reason_code='risk_metadata_not_requested',machine_rationale='未找到显式风险等级要求；元数据风险标签不是原文依据，人工决定排除、允许或保留。')
    if len(matches)!=1 or _uncertain_prefix(text,matches[0].start()):return
    m=matches[0];args={'value':m.group(1).lower()};changed=args!=atom['arguments']
    _set(result,text,m.span(),status='proposed' if changed else 'supported',code='literal_risk_level',rationale='原文明示风险等级，不从其他行为推断风险数值。',operation='modify' if changed else 'retain',replacements=[_revision(atom,arguments=args)] if changed else [])


_RULES={'actor_role_count':_actor_rule,'actor_relative_region':_spatial_rule,'before':_temporal_rule,'event_spec':_event_rule,'risk_level':_risk_rule}


def build_revision_proposals(task, legacy_review=None):
    legacy={}
    if legacy_review is not None:
        for key,expected in (('query_id',task['subject_id']),('source_query_sha256',task['source_query_sha256']),('source_oracle_sha256',task['source_oracle_sha256'])):
            if legacy_review.get(key)!=expected:raise ValidationError('legacy Agent review source binding differs')
        legacy={d['atom_id']:d for d in legacy_review['atom_decisions']}
        if len(legacy)!=len(legacy_review['atom_decisions']) or set(legacy)!={a['atom_id'] for a in task['oracle_draft']['atoms']}:raise ValidationError('legacy Agent review atom mapping is incomplete')
    items=[]
    for atom in task['oracle_draft']['atoms']:
        result=_result(atom)
        if presentation_warnings(atom):
            result.update(reason_code='unknown_source_semantics',machine_rationale='源包含未登记或冲突字段，不能借修订提案删除条件；须先完整核对。')
        elif atom['predicate'] in _RULES:_RULES[atom['predicate']](atom,task,result)
        if atom['atom_id'] in legacy:result['legacy_advisory']=copy.deepcopy(legacy[atom['atom_id']])
        for index,replacement in enumerate(result['replacement_atoms']):
            normalized=_normalize_human_atom(replacement,task['subject_id'])
            evidence=result['evidence'][0]
            normalized['provenance']={'source':'query_text_regex','query_id':task['subject_id'],'span':[evidence['start'],evidence['end']]}
            result['replacement_atoms'][index]=normalized
        items.append(result)
    core={'artifact_type':'query_revision_proposals','proposal_version':'1','profile':PROFILE,'task_binding':task_binding(task),'human_gold':False,'generator':{'kind':'offline_rules','llm_calls':0,'config_sha256':content_hash([ACTOR_WORDS,RELATIONS,EVENT_WORDS,EVENT_ACTOR_TYPES,COUNTS]),'implementation_sha256':sha256_file(Path(__file__))},'items':items}
    report={**core,'report_id':content_hash(core)}
    validate_revision_proposals(task,report)
    return report


def validate_revision_proposals(task,report):
    from jsonschema import Draft7Validator
    errors=list(Draft7Validator(read_json(asset_path('review','revision_proposals.schema.json'))).iter_errors(report))
    if errors:raise ValidationError('revision proposal schema violation: '+errors[0].message)
    if not isinstance(report,dict) or set(report)!={'artifact_type','proposal_version','profile','task_binding','human_gold','generator','items','report_id'}:raise ValidationError('malformed revision proposal report')
    if report['artifact_type']!='query_revision_proposals' or report['proposal_version']!='1' or report['profile']!=PROFILE or report['human_gold'] is not False or report['task_binding']!=task_binding(task):raise ValidationError('revision proposal source/version differs')
    if report['report_id']!=content_hash({k:v for k,v in report.items() if k!='report_id'}):raise ValidationError('revision proposal content differs')
    sources={a['atom_id']:a for a in task['oracle_draft']['atoms']}
    if not isinstance(report['items'],list):raise ValidationError('proposal items must be a list')
    seen=[]
    for item in report['items']:
        required={'source_atom_id','source_atom_sha256','status','reason_code','machine_rationale','evidence','operation','replacement_atoms','semantic_kind'}
        if not isinstance(item,dict) or not required<=set(item) or set(item)-required-{'legacy_advisory'}:raise ValidationError('malformed atom proposal')
        aid=item['source_atom_id'];seen.append(aid)
        if aid not in sources or item['source_atom_sha256']!=content_hash(sources[aid]):raise ValidationError('proposal atom source differs')
        if item['status'] not in ('supported','proposed','unresolved') or not item['reason_code'] or not item['machine_rationale']:raise ValidationError('proposal disposition missing')
        if item['status']!='unresolved' and not item['evidence']:raise ValidationError('proposal without text evidence must remain unresolved')
        if item['status']!='unresolved' and presentation_warnings(sources[aid]):raise ValidationError('unknown source semantics must remain unresolved')
        for e in item['evidence']:
            if set(e)!={'source','start','end','quote','query_sha256'} or e['source']!='query_text' or type(e['start']) is not int or type(e['end']) is not int or not 0<=e['start']<e['end']<=len(task['query_text']) or task['query_text'][e['start']:e['end']]!=e['quote'] or e['query_sha256']!=content_hash(task['query_text']):raise ValidationError('proposal quote is not exact current-query evidence')
        if item['status']=='proposed':
            expected=(1,) if item['operation']=='modify' else range(2,10000) if item['operation']=='split' else (0,) if item['operation']=='reject' else ()
            if len(item['replacement_atoms']) not in expected:raise ValidationError('proposal operation has invalid target count')
            for replacement in item['replacement_atoms']:
                normalized=_normalize_human_atom(replacement,task['subject_id'])
                e=item['evidence'][0]
                normalized['provenance']={'source':'query_text_regex','query_id':task['subject_id'],'span':[e['start'],e['end']]}
                if normalized!=replacement:raise ValidationError('proposal replacement ID/provenance is not canonical machine evidence')
                if presentation_warnings(replacement):raise ValidationError('proposal replacement has unknown or incomplete semantics')
        elif item['replacement_atoms'] or item['operation']!=('retain' if item['status']=='supported' else 'none'):raise ValidationError('unresolved/retained proposal cannot hide a revision')
    if len(set(seen))!=len(seen) or set(seen)!=set(sources):raise ValidationError('source atoms were omitted or duplicated')


def proposal_coverage(reports):
    counts=Counter(item['status'] for report in reports for item in report['items'])
    reasons=Counter(item['reason_code'] for report in reports for item in report['items'] if item['status']=='unresolved')
    return {'subjects':len(reports),'atoms':sum(counts.values()),'statuses':dict(counts),'unresolved_reasons':dict(reasons),'human_gold':False,'quality_claim':'rule coverage only; semantic accuracy and user-time benefit not measured'}
