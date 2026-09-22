'use strict';
(() => {
  const packet = JSON.parse(document.getElementById('packet').textContent);
  const $ = id => document.getElementById(id), clone = v => JSON.parse(JSON.stringify(v));
  const fail = (ok, message) => { if (!ok) throw new Error(message); };
  const exact = (v, keys) => v && typeof v === 'object' && !Array.isArray(v) && Object.keys(v).sort().join('|') === [...keys].sort().join('|');
  const codepointOrder = (a,b) => { const x=Array.from(a),y=Array.from(b); for(let i=0;i<Math.min(x.length,y.length);i++){const d=x[i].codePointAt(0)-y[i].codePointAt(0);if(d)return d;} return x.length-y.length; };
  function wireTree(v) {
    if(v===null)return ['null'];
    if(typeof v==='boolean')return ['bool',v];
    if(typeof v==='string')return ['str',v];
    if(typeof v==='number'){
      fail(Number.isFinite(v),'数值不是有限数字');
      const b=new ArrayBuffer(8);new DataView(b).setFloat64(0,v===0?0:v,false);
      return ['num',Array.from(new Uint8Array(b),x=>x.toString(16).padStart(2,'0')).join('')];
    }
    if(Array.isArray(v))return ['list',v.map(wireTree)];
    fail(v&&typeof v==='object','数据类型不受支持');
    return ['dict',Object.keys(v).sort(codepointOrder).map(k=>[k,wireTree(v[k])])];
  }
  async function hash(v){const bytes=new TextEncoder().encode(JSON.stringify(wireTree(v)));return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes)),x=>x.toString(16).padStart(2,'0')).join('');}
  const draftKeys=['model_version','task_binding','proposal_id','reviewer_id','form','revision','edit_sources','receipts','issues','status','human_gold'];
  const backupKeys=['artifact_type','packet_version','packet_id','reviewer_id','generation','entries','human_gold','backup_sha256'];
  const stateKey='bus-review:'+packet.packet_id+':'+packet.reviewer_id;
  let state={artifact_type:'browser_review_backup',packet_version:packet.packet_version,packet_id:packet.packet_id,reviewer_id:packet.reviewer_id,generation:0,entries:packet.items.map(i=>clone(i.initial_draft)),human_gold:false};
  let current=0,readonly=true,busy=false,composing=false,saveTimer=null,storedDigest=null,storedGeneration=null,storageFailed=false,savesPending=0;
  const item=()=>packet.items[current], draft=()=>state.entries[current];
  const policyLabels={candidate:'是否存在合理变化',eligible:'是否纳入多样性评价',cross_platform_judgeable:'跨平台能否一致判断',decision_status:'材料状态',dimensions:'允许变化的维度',name:'维度名称',reason:'理由',allowed_values:'允许取值',bin_definition:'数值分档规则',target_selector:'目标选择规则（专家设置）',cardinality:'目标数量条件',common_semantic:'跨平台共有语义',query_blind:'不读取题目元数据',candidate_scope:'候选对象范围',missing_target:'目标缺失时的处理',target_signature:'目标特征',actor_class:'参与者类型',platforms:'适用平台',near_max:'近距离上界',medium_max:'中距离上界'};
  const fieldLabel=k=>(packet.dictionary.fields[k]||[policyLabels[k]||k])[0];
  const tokens={...packet.dictionary.tokens,supported:'应生成场景',unsupported:'应明确拒绝',generate_scene:'生成场景',draft:'机器草稿',near:'近',far:'远',exactly_one:'恰好一个可识别目标'};
  function text(v){if(v===null)return '草稿未明确';if(v===true)return '是';if(v===false)return '否';if(Array.isArray(v))return v.length?v.map(text).join('、'):'空列表';if(typeof v==='object')return Object.entries(v).map(([k,x])=>fieldLabel(k)+'：'+text(x)).join('；');return tokens[String(v)]||String(v);}
  function node(tag,value,cls){const n=document.createElement(tag);if(value!==undefined)n.textContent=value;if(cls)n.className=cls;return n;}
  function button(label,action){const b=node('button',label);b.type='button';b.addEventListener('click',action);return b;}
  function notify(message){$('message').textContent=message;}
  function originalAtoms(){return item().task.oracle_draft.atoms;}
  function decode(s,type){const v=JSON.parse(s);fail(type==='list'?Array.isArray(v):v&&typeof v==='object'&&!Array.isArray(v),'修订格式不正确');return v;}
  function atomWarnings(atom,query){
    const out=[],spec=packet.dictionary.predicates[atom.predicate],args=atom.arguments;
    if(!spec)out.push('未登记要求类型：'+atom.predicate);
    else if(atom.category!==spec[0])out.push('类别与要求类型不一致');
    if(!args||typeof args!=='object'||Array.isArray(args))return [...out,'参数结构无效'];
    if(spec){
      for(const k of spec[2])if(!(k in args)||args[k]===null||args[k]==='')out.push('草稿未明确：'+fieldLabel(k));
      for(const k of Object.keys(args))if(![...spec[2],...spec[3]].includes(k))out.push('未登记字段：'+k);
      if(atom.predicate==='before')for(const group of [['first','before','source'],['second','after','target']])if(group.filter(k=>k in args).length!==1||!group.some(k=>args[k]))out.push('先后关系端点未明确');
      if(atom.predicate==='parallel_group'&&(['events','members'].filter(k=>k in args).length!==1||!(args.events||args.members)?.length))out.push('并行事件成员未明确');
    }
    for(const [k,v]of Object.entries(args))if(v&&typeof v==='object'&&(!Array.isArray(v)||v.some(x=>x&&typeof x==='object')))out.push('嵌套字段需专家核对：'+k);
    for(const k of Object.keys(atom))if(!['atom_id','category','predicate','arguments','layer','polarity','weight','provenance','decision_status','notes'].includes(k))out.push('未登记字段：'+k);
    if(!['core_required','surface_required','permitted','forbidden'].includes(atom.layer))out.push('计分层级未明确');
    if(!['present','absent'].includes(atom.polarity))out.push('出现方向未明确');
    if(atom.layer==='forbidden'&&atom.polarity==='present')out.push('禁止层与要求出现需专家核对，不能额外取反');
    const p=atom.provenance;
    if(p?.source==='query_text_regex'&&(!Array.isArray(p.span)||p.span.length!==2||!p.span.every(Number.isInteger)||p.span[0]<0||p.span[0]>=p.span[1]||p.span[1]>Array.from(query).length))out.push('原文依据位置无效');
    return out;
  }
  function statement(a){
    const layer={core_required:'同一意图各表述必须满足',surface_required:'当前文字必须满足',permitted:'允许但不强制，缺失不扣分',forbidden:'禁止项，不额外取反'}[a.layer]||a.layer;
    const polarity={present:'出现或成立',absent:'不出现或不成立'}[a.polarity]||a.polarity;
    const label=(packet.dictionary.predicates[a.predicate]||[])[1]||('未登记要求：'+a.predicate);
    const extras=Object.fromEntries(Object.entries(a).filter(([k])=>!['atom_id','category','predicate','arguments','layer','polarity','weight','provenance','decision_status','notes'].includes(k)));
    return layer+'；'+polarity+'；'+label+'。'+text(a.arguments)+(a.notes?'；备注：'+a.notes:'')+(a.weight!==undefined&&a.weight!==1?'；权重：'+a.weight:'')+(Object.keys(extras).length?'；未登记原值：'+text(extras):'')+(a.predicate==='event_spec'?'；草稿未给出的参与者绑定、触发和结束条件不在展示层补写':'');
  }
  function showTree(parent,v,key=''){
    if(v&&typeof v==='object'){
      if(key)parent.append(node('strong',fieldLabel(key)+'：'));
      const list=node('div',undefined,'value-tree');for(const [k,x]of Object.entries(v))showTree(list,x,k);parent.append(list);
    }else parent.append(node('p',(key?fieldLabel(key)+'：':'')+text(v),'value-tree'));
  }
  function snapshot(entry,index){const content={};for(const [k,v]of Object.entries(entry))if(!['receipts','status','human_gold'].includes(k))content[k]=v;const result={packet_id:packet.packet_id,task:packet.items[index].task,proposal:packet.items[index].proposal,draft_content:content};if(packet.items[index].revision_proposals)result.revision_proposals=packet.items[index].revision_proposals;if(packet.items[index].surface_diff)result.surface_diff=packet.items[index].surface_diff;return result;}
  function coverage(index){return [...packet.items[index].task.oracle_draft.atoms.map(a=>'atom:'+a.atom_id),'support','cpd','additions','notes'];}
  function controls(){ $('editor').disabled=readonly||busy;$('confirm').disabled=readonly||busy||composing; $('defer').disabled=readonly||busy||composing; $('restore').disabled=readonly||busy;for(const id of ['queue','filter','previous','next'])$(id).disabled=busy; }
  function markChanged(origin='human',audit={}){
    if(readonly||busy)return false;
    const d=draft();d.revision++;d.receipts=[];d.status=d.issues.length?'deferred':'draft';d.edit_sources.push({revision:d.revision,origin,...audit});
    state.generation++;$('storage').textContent='有未保存修改；正在自动保存到浏览器…';queueUpdate();scheduleSave();return true;
  }
  function scheduleSave(){clearTimeout(saveTimer);saveTimer=setTimeout(()=>{saveTimer=null;save().catch(e=>notify(e.message));},150);}
  async function backup(){const core=clone(state);return {...core,backup_sha256:await hash(core)};}
  async function save(){
    if(readonly)return;clearTimeout(saveTimer);saveTimer=null;savesPending++;
    try {
    const generation=state.generation,value=await backup();if(generation!==state.generation)return;
    try{
      const raw=localStorage.getItem(stateKey);
      if(raw){const live=JSON.parse(raw);fail(live.generation===storedGeneration&&live.backup_sha256===storedDigest,'本地进度被另一会话更新，已停止写入，请导出当前内存备份');}
      else fail(storedDigest===null,'本地保存记录已被删除，已停止写入，请导出备份');
      localStorage.setItem(stateKey,JSON.stringify(value));storedGeneration=value.generation;storedDigest=value.backup_sha256;storageFailed=false;
      $('storage').textContent='已保存到本浏览器；尚未保存为磁盘备份文件。';
    }catch(e){storageFailed=true;$('storage').textContent='仅保存在内存，浏览器保存失败；关闭前必须导出备份。';notify(e.message);}
    } finally {savesPending--;}
  }
  function validateEntry(entry,index){
    const initial=packet.items[index].initial_draft;
    fail(exact(entry,draftKeys)&&entry.human_gold===false,'题目状态结构错误');
    fail(entry.model_version===initial.model_version&&JSON.stringify(entry.task_binding)===JSON.stringify(initial.task_binding)&&entry.proposal_id===initial.proposal_id&&entry.reviewer_id===packet.reviewer_id,'题目、来源、版本或审阅者不匹配');
    fail(Number.isSafeInteger(entry.revision)&&entry.revision>=0&&['draft','submitted','deferred','requires_source_fix'].includes(entry.status),'题目状态无效');
    fail(Array.isArray(entry.receipts)&&Array.isArray(entry.issues)&&Array.isArray(entry.edit_sources),'记录列表损坏');
    const f=entry.form;fail(exact(f,['required_check_decisions','atom_decisions','cpd_decision','added_atoms_json','notes']),'表单字段错误');
    fail(typeof f.notes==='string'&&Array.isArray(f.atom_decisions)&&typeof f.added_atoms_json==='string','表单内容损坏');
    fail(f.atom_decisions.length===initial.form.atom_decisions.length&&f.atom_decisions.every((d,i)=>d.atom_id===initial.form.atom_decisions[i].atom_id),'要求处置列表被改写');
    decode(f.added_atoms_json,'list');for(const d of f.atom_decisions){fail(exact(d,['atom_id','verdict','reason','replacement_atoms_json'])&&typeof d.reason==='string'&&['','accept','modify','split','merge','reject'].includes(d.verdict)&&typeof d.replacement_atoms_json==='string','要求修订记录损坏');decode(d.replacement_atoms_json,'list');}
    fail(exact(f.required_check_decisions,Object.keys(initial.form.required_check_decisions)),'一致性检查字段被改写');for(const d of Object.values(f.required_check_decisions))fail(exact(d,['verdict','reason'])&&typeof d.reason==='string'&&['','accept','revise','reject'].includes(d.verdict),'一致性检查内容损坏');
    fail(exact(f.cpd_decision,['verdict','reason','replacement_policy_json'])&&typeof f.cpd_decision.reason==='string'&&['','accept','revise','reject'].includes(f.cpd_decision.verdict)&&typeof f.cpd_decision.replacement_policy_json==='string','CPD判断损坏');decode(f.cpd_decision.replacement_policy_json,'object');
  }
  async function validateBackup(value){
    fail(exact(value,backupKeys)&&value.artifact_type==='browser_review_backup'&&value.human_gold===false,'备份格式不正确');
    fail(value.packet_version===packet.packet_version&&value.packet_id===packet.packet_id&&value.reviewer_id===packet.reviewer_id,'备份属于另一题包、版本或审阅者');
    fail(Number.isSafeInteger(value.generation)&&value.generation>=0&&Array.isArray(value.entries)&&value.entries.length===packet.items.length,'备份题目或版本不完整');
    const core=clone(value);delete core.backup_sha256;fail(await hash(core)===value.backup_sha256,'备份已损坏，原进度未改变');
    for(let i=0;i<value.entries.length;i++){
      const entry=value.entries[i];validateEntry(entry,i);const digest=await hash(snapshot(entry,i)),covered=new Set();
      for(const r of entry.receipts){fail(exact(r,['action','content_sha256','covered_units','reviewer_id','revision'])&&r.action==='explicit_confirm'&&r.content_sha256===digest&&r.reviewer_id===packet.reviewer_id&&r.revision===entry.revision,'确认收据失效');fail(Array.isArray(r.covered_units)&&r.covered_units.length&&new Set(r.covered_units).size===r.covered_units.length&&r.covered_units.every(x=>coverage(i).includes(x)),'确认范围错误');r.covered_units.forEach(x=>covered.add(x));}
      if(entry.status==='submitted')fail(!entry.issues.length&&covered.size===coverage(i).length,'未完整确认的题不能恢复为已提交');
    }
    return core;
  }
  async function restore(value){
    fail(!readonly&&!busy,'当前为只读或正在处理，请稍后再试');busy=true;controls();const generation=state.generation;
    try{const candidate=await validateBackup(value);fail(state.generation===generation,'当前内容已改变，恢复已取消');fail(candidate.generation>=state.generation,'这是较旧备份，不能覆盖当前进度');if(candidate.generation===state.generation)fail(await hash(candidate)===await hash(state),'同版本备份内容冲突，原进度保留');state=candidate;await save();render();notify('备份恢复完成；仍须由维护者正式校验。');}finally{busy=false;controls();}
  }
  function queueUpdate(){
    const select=$('queue'),filter=$('filter').value;select.replaceChildren();const groups=new Map();state.entries.forEach((e,i)=>{if(filter==='pending'&&e.status==='submitted'||filter==='issues'&&!e.issues.length||filter==='submitted'&&e.status!=='submitted')return;const o=node('option',`${i+1}. ${e.status==='submitted'?'已提交':e.issues.length?'有疑问':'待审阅'} · ${packet.items[i].task.query_record.surface_style}`);o.value=String(i);const rec=packet.items[i].task.query_record,key=rec.dataset_split+' / '+rec.intent_group_id;if(!groups.has(key)){const group=node('optgroup');group.label=key;groups.set(key,group);select.append(group);}groups.get(key).append(o);});select.value=String(current);
    $('progress').textContent=`已提交 ${state.entries.filter(e=>e.status==='submitted').length} / ${state.entries.length}；疑问 ${state.entries.filter(e=>e.issues.length).length}`;
  }
  function fieldEditor(parent,atom,update){
    const box=node('div',undefined,'fields');
    for(const [key,value]of Object.entries(atom.arguments||{})){
      if(value&&typeof value==='object'&&(!Array.isArray(value)||value.some(x=>x&&typeof x==='object'))){const readonlyField=node('div');showTree(readonlyField,value,key);readonlyField.append(node('p','嵌套结构保留原值，请暂存交由专家处理。','warning'));box.append(readonlyField);continue;}
      const label=node('label',fieldLabel(key)),input=typeof value==='boolean'?node('select'):node('input');
      if(typeof value==='boolean'){for(const [v,t]of [['true','是'],['false','否']]){const o=node('option',t);o.value=v;input.append(o);}input.value=String(value);}
      else {input.type=typeof value==='number'?'number':'text';if(input.type==='number')input.step='any';input.value=Array.isArray(value)?value.join('、'):(value??'');}
      input.title=(packet.dictionary.fields[key]||['','未知字段，须专家处理'])[1];input.setAttribute('aria-label',fieldLabel(key));
      input.addEventListener('input',()=>{if(readonly||busy)return;let v=input.value;if(typeof value==='boolean')v=v==='true';else if(typeof value==='number')v=v===''?'':Number(v);else if(Array.isArray(value))v=v.split('、').map(x=>x.trim()).filter(Boolean);atom.arguments[key]=v;update(atom);});label.append(input);box.append(label);
    }
    const layerLabel=node('label','计分层级'),layer=node('select');for(const [value,label]of [['core_required','各表述必须'],['surface_required','当前文字必须'],['permitted','允许但不强制'],['forbidden','禁止项']]){const o=node('option',label);o.value=value;layer.append(o);}layer.value=atom.layer;layer.onchange=()=>{atom.layer=layer.value;update(atom);};layerLabel.append(layer);box.append(layerLabel);
    const polarityLabel=node('label','出现方向'),polarity=node('select');for(const [v,t]of [['present','要求出现或成立'],['absent','要求不出现或不成立']]){const o=node('option',t);o.value=v;polarity.append(o);}polarity.value=atom.polarity;polarity.onchange=()=>{atom.polarity=polarity.value;update(atom);};polarityLabel.append(polarity);box.append(polarityLabel);parent.append(box);
  }
  function render(){
    queueUpdate();$('subject').textContent=item().task.subject_id;$('query').textContent=item().task.query_text;$('cards').replaceChildren();
    const d=draft();$('surface-diff').replaceChildren();const diff=item().surface_diff;if(diff){const details=node('details');details.append(node('summary','与精确表述的差异：'+(diff.category_labels.join('、')||'文字相同，仍须核对来源要求')),node('p','差异分类只用于导航；每个表述独立提交，有差异时不自动继承。'));for(const change of diff.changes)details.append(node('p',(change.source_tokens.join(' ')||'（空）')+' → '+(change.target_tokens.join(' ')||'（空）')));for(const change of diff.atom_changes||[])details.append(node('p','要求差异：'+(change.source?statement(change.source):'精确版无此要求')+' → '+(change.target?statement(change.target):'当前草稿无此要求')));$('surface-diff').append(details);}
    originalAtoms().forEach((source,index)=>{
      const decision=d.form.atom_decisions[index],card=node('article',undefined,'card');card.append(node('h3',`${index+1}. ${(packet.dictionary.predicates[source.predicate]||[])[1]||source.predicate}`),node('p',statement(source)));
      const warnings=atomWarnings(source,item().task.query_text);if(warnings.length)card.append(node('p',warnings.join('；'),'warning'));
      const p=source.provenance;let evidence='机器元数据建议，需对照原文核实';if(p?.source==='query_text_regex')evidence=warnings.includes('原文依据位置无效')?'原文依据位置无效':`精确原文：“${Array.from(item().task.query_text).slice(p.span[0],p.span[1]).join('')}”`;else if(p?.field)evidence+='；来源字段：'+p.field;card.append(node('p',evidence,'source'));
      const suggestion=item().revision_proposals?.items.find(x=>x.source_atom_id===source.atom_id);
      if(suggestion){
        const panel=node('div',undefined,'source');panel.append(node('strong',suggestion.status==='proposed'?'机器修订提案（尚未采用）':suggestion.status==='supported'?'机器找到的文字依据，仍需人工核对':'机器未解决的疑问'),node('p',suggestion.machine_rationale));
        for(const e of suggestion.evidence)panel.append(node('p','原文：“'+e.quote+'”'));
        if(suggestion.legacy_advisory)panel.append(node('p','旧 Agent 提示（机器）：'+suggestion.legacy_advisory.reason));
        if(suggestion.status==='proposed'){for(const target of suggestion.replacement_atoms)panel.append(node('p','建议改为：'+statement(target)));panel.append(button('采用此修订草稿',()=>{decision.verdict=suggestion.operation;decision.replacement_atoms_json=JSON.stringify(suggestion.replacement_atoms);decision.reason='Machine revision proposal: '+suggestion.machine_rationale;d.issues=d.issues.filter(x=>x.atom_id!==source.atom_id);markChanged('machine_proposal',{proposal_report_id:item().revision_proposals.report_id,source_atom_id:source.atom_id});render();}));}
        if(d.issues.some(x=>x.code==='machine_unresolved'&&x.atom_id===source.atom_id))panel.append(button('我已核对这条疑问，使用当前编辑',()=>{d.issues=d.issues.filter(x=>!(x.code==='machine_unresolved'&&x.atom_id===source.atom_id));markChanged();render();}));
        card.append(panel);
      }
      const verdict=node('select');for(const [value,label]of [['accept','保留草稿'],['modify','修改要求'],['reject','排除此草稿要求'],['split','拆分要求'],['merge','合并要求']]){const o=node('option',label);o.value=value;verdict.append(o);}verdict.value=decision.verdict;verdict.setAttribute('aria-label','要求处理');
      verdict.onchange=()=>{if(readonly||busy)return;decision.verdict=verdict.value;decision.reason=verdict.value==='accept'?packet.items[current].initial_draft.form.atom_decisions[index].reason:'Human rationale code: explicit '+verdict.value+' of displayed requirement.';decision.replacement_atoms_json=JSON.stringify(['modify','split','merge'].includes(verdict.value)?(verdict.value==='split'?[clone(source),clone(source)]:[clone(source)]):[]);markChanged();render();};card.append(verdict);
      if(decision.verdict==='merge'){
        const peers=node('select');peers.append(node('option','选择另一个待合并要求'));originalAtoms().forEach((a,j)=>{if(j===index)return;const o=node('option',`${j+1}. ${statement(a)}`);o.value=String(j);peers.append(o);});peers.onchange=()=>{const j=Number(peers.value);if(!Number.isInteger(j)||j===index)return;const raw=decision.replacement_atoms_json;d.form.atom_decisions[j].verdict='merge';d.form.atom_decisions[j].reason=decision.reason;d.form.atom_decisions[j].replacement_atoms_json=raw;markChanged();render();};card.append(peers);
      }
      if(['modify','split','merge'].includes(decision.verdict)){
        const replacements=decode(decision.replacement_atoms_json,'list');const leader=decision.verdict==='merge'?d.form.atom_decisions.findIndex(x=>x.verdict==='merge'&&x.replacement_atoms_json===decision.replacement_atoms_json):index;if(leader!==index)card.append(node('p','此合并共用第 '+(leader+1)+' 条的修订结果，请在该条编辑。'));(leader===index?replacements:[]).forEach((target,j)=>{card.append(node('strong','修订后要求 '+(j+1)));fieldEditor(card,target,()=>{const old=decision.replacement_atoms_json;decision.replacement_atoms_json=JSON.stringify(replacements);if(decision.verdict==='merge')for(const peer of d.form.atom_decisions)if(peer!==decision&&peer.verdict==='merge'&&peer.replacement_atoms_json===old)peer.replacement_atoms_json=decision.replacement_atoms_json;markChanged();});});
      }
      if(decision.verdict!=='accept'){const label=node('label','修订依据（必要时补充）'),reason=node('textarea');reason.value=decision.reason;reason.oninput=()=>{decision.reason=reason.value;markChanged();};label.append(reason);card.append(label);}
      $('cards').append(card);
    });
    const added=decode(d.form.added_atoms_json,'list');added.forEach((a,i)=>{const card=node('article',undefined,'card');card.append(node('h3','新增要求 '+(i+1)));fieldEditor(card,a,()=>{d.form.added_atoms_json=JSON.stringify(added);markChanged();});card.append(button('移除此新增草稿',()=>{added.splice(i,1);d.form.added_atoms_json=JSON.stringify(added);markChanged();render();}));$('cards').append(card);});
    $('support').replaceChildren(node('h3','请求处理方式'),node('p','当前源材料：'+text(item().task.oracle_draft.expected_support)+'；'+text(item().task.oracle_draft.acceptable_response)),node('p','若源材料标错，请选择“原文或源材料有误”并暂存。'));
    $('cpd').replaceChildren(node('h3','允许的合理变化（CPD）'),node('p','以下内容也在本题确认范围内；有异议请暂存交由专家处理。'));showTree($('cpd'),d.form.cpd_decision.verdict==='revise'?decode(d.form.cpd_decision.replacement_policy_json,'object'):item().task.oracle_draft.cpd_policy);
    $('notes').value=d.form.notes;$('issue-list').textContent=d.issues.map(x=>x.reason||String(x)).join('；');$('resume').hidden=!d.issues.length;controls();
  }
  const semanticKey=a=>JSON.stringify(wireTree(Object.fromEntries(['category','predicate','arguments','layer','polarity','weight'].map(k=>[k,a[k]??(k==='weight'?1:null)]))));
  const identityKey=a=>JSON.stringify(wireTree(Object.fromEntries(['category','predicate','arguments','polarity'].map(k=>[k,a[k]??null]))));
  function confirmProblems(){
    const d=draft(),problems=[];if(d.issues.length)problems.push('本题仍有疑问，请先处理或返回审阅');
    const finals=[];
    for(let i=0;i<d.form.atom_decisions.length;i++){
      const dec=d.form.atom_decisions[i];if(!dec.reason?.trim())problems.push('第'+(i+1)+'条缺少修订依据');
      if(dec.verdict==='accept')finals.push(originalAtoms()[i]);else if(['modify','split','merge'].includes(dec.verdict)){
        const targets=decode(dec.replacement_atoms_json,'list');if(dec.verdict==='split'?targets.length<2:targets.length!==1)problems.push('第'+(i+1)+'条修改/拆分目标数量不正确');
        if(targets.length&&!targets.some(a=>semanticKey(a)!==semanticKey(originalAtoms()[i])))problems.push('第'+(i+1)+'条没有实际语义修改，请改值或选择保留');
        if(dec.verdict==='split'&&new Set(targets.map(identityKey)).size!==targets.length)problems.push('第'+(i+1)+'条拆分目标重复，请修改重复条目');
        if(dec.verdict==='merge'&&d.form.atom_decisions.filter(x=>x.verdict==='merge'&&x.replacement_atoms_json===dec.replacement_atoms_json).length<2)problems.push('合并需选择至少两个源要求');
        finals.push(...targets);
      }else if(dec.verdict!=='reject')problems.push('存在未判断的要求');
    }
    finals.push(...decode(d.form.added_atoms_json,'list'));for(const a of finals)problems.push(...atomWarnings(a,item().task.query_text));
    if(d.form.required_check_decisions.support_and_response_disposition.verdict!=='accept'||d.form.cpd_decision.verdict==='reject')problems.push('源问题不能通过提交');
    return problems;
  }
  async function confirm(){
    fail(!readonly&&!busy&&!composing,'当前不能提交');const problems=confirmProblems();fail(!problems.length,problems.join('；'));
    busy=true;controls();try{const index=current,entry=draft(),revision=entry.revision,digest=await hash(snapshot(entry,index));fail(entry.revision===revision,'内容已改变，请重新核对');entry.receipts=[{action:'explicit_confirm',content_sha256:digest,covered_units:coverage(index),reviewer_id:packet.reviewer_id,revision}];entry.status='submitted';state.generation++;await save();notify(storageFailed?'本题仅在内存中确认，必须导出备份后交给维护者；尚未生成正式 gold。':'本题已提交并保存到浏览器，待维护者校验；尚未生成正式 gold。');queueUpdate();}finally{busy=false;controls();}
  }
  function navigate(delta){const visible=Array.from($('queue').options).map(o=>Number(o.value));const at=visible.indexOf(current);if(!visible.length)return;current=visible[(Math.max(0,at)+delta+visible.length)%visible.length];render();notify('');}
  $('notes').oninput=()=>{draft().form.notes=$('notes').value;markChanged();};
  $('editor').addEventListener('compositionstart',()=>{composing=true;controls();});$('editor').addEventListener('compositionend',()=>{composing=false;controls();});
  $('queue').onchange=()=>{current=Number($('queue').value);render();notify('');};$('filter').onchange=queueUpdate;
  $('previous').onclick=()=>navigate(-1);$('next').onclick=()=>navigate(1);
  $('confirm').onclick=()=>confirm().catch(e=>notify(e.message));
  $('defer').onclick=()=>{if(readonly||busy||composing)return;const reason=$('issue').selectedOptions[0].textContent;if($('issue').value==='other'&&!draft().form.notes.trim()){notify('其他原因请在备注中说明');return;}draft().issues=draft().issues.filter(x=>x.code==='machine_unresolved');draft().issues.push({code:$('issue').value,reason});markChanged();if($('issue').value==='source_error')draft().status='requires_source_fix';scheduleSave();navigate(1);};
  $('resume').onclick=()=>{draft().issues=draft().issues.filter(x=>x.code==='machine_unresolved');markChanged();render();};
  $('add').onclick=()=>{const added=decode(draft().form.added_atoms_json,'list');added.push({category:'actor',predicate:'actor_role_count',arguments:{count:'1',role:'',type:'pedestrian'},layer:'surface_required',polarity:'present',weight:1,notes:''});draft().form.added_atoms_json=JSON.stringify(added);markChanged();render();};
  $('export').onclick=async()=>{try{const b=await backup(),blob=new Blob([JSON.stringify(b,null,2)],{type:'application/json'}),url=URL.createObjectURL(blob),a=node('a');a.href=url;a.download='review-'+packet.packet_id.slice(0,12)+'-v'+state.generation+'.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);notify('已发起备份下载，请在浏览器下载记录中确认文件保存。');}catch(e){notify('备份导出失败：'+e.message);}};
  $('restore').onchange=async()=>{try{const file=$('restore').files[0];if(!file)return;fail(file.size<=64*1024*1024,'备份文件过大');await restore(JSON.parse(await file.text()));}catch(e){notify(e.message);}finally{$('restore').value='';}};
  $('help').onclick=()=>$('guide').showModal();$('close-guide').onclick=()=>$('guide').close();$('guide-text').textContent=packet.guide;$('identity').textContent='审阅者：'+packet.reviewer_id;
  const answerLabels={required:'必须满足',permitted:'允许但不强制',forbidden:'禁止出现',exclude:'排除此草稿要求',defer:'暂存疑问'};for(const exercise of packet.practice.cases){const card=node('article',undefined,'card');card.append(node('h3','练习：'+exercise.query_text),node('p',statement(exercise.atom)));const answer=node('p',answerLabels[exercise.answer]+'。'+exercise.explanation);answer.hidden=true;card.append(button('显示参考解释',()=>{answer.hidden=!answer.hidden;}),answer);$('practice').append(card);}
  window.addEventListener('storage',e=>{if(e.key===stateKey&&e.newValue){try{const x=JSON.parse(e.newValue);if(storedDigest!==null&&x.backup_sha256!==storedDigest){readonly=true;controls();$('storage').textContent='另一会话更改了进度，已转为只读；请导出内存备份后重新打开。';}}catch(_){readonly=true;controls();}}});
  window.addEventListener('beforeunload',e=>{if(storageFailed||saveTimer||savesPending){e.preventDefault();e.returnValue='';}});
  async function initialize(){
    let raw=null;try{raw=localStorage.getItem(stateKey);}catch(e){storageFailed=true;notify('浏览器存储不可读取，将使用内存；关闭前须导出备份。');}
    if(raw){try{const value=JSON.parse(raw);state=await validateBackup(value);storedDigest=value.backup_sha256;storedGeneration=value.generation;}catch(e){readonly=true;render();$('storage').textContent='已有本地记录损坏，已保护原件并进入只读。';notify(e.message);return;}}
    fail(navigator.locks&&navigator.locks.request,'浏览器不支持独占编辑锁，请使用当前版本 Chrome 或 Edge。');
    navigator.locks.request(stateKey,{ifAvailable:true},async lock=>{if(!lock){readonly=true;render();$('storage').textContent='另一标签页正在编辑，本页只读。';return;}readonly=false;render();await save();return new Promise(()=>{});}).catch(e=>{readonly=true;controls();notify(e.message);});
  }
  window.ReviewWorkbench=Object.freeze({hash,wireTree,backup,restore,confirm,validateBackup,getState:()=>clone(state),ready:()=>!readonly&&!busy});
  render();initialize().catch(e=>{readonly=true;controls();$('storage').textContent='只读：无法取得安全编辑条件。';notify(e.message);});
})();
