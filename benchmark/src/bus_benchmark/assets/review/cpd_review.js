'use strict';
// CPD choices attest only to the visible semantic questions, never hidden rules.
function createCPDReview({packet,node,text,lookup,clone,wireTree,fail,showTree,markChanged,render,editable}) {
  const equal=(a,b)=>JSON.stringify(wireTree(a))===JSON.stringify(wireTree(b));
  const policy=(entry,index)=>entry.form.cpd_decision.verdict==='revise'?JSON.parse(entry.form.cpd_decision.replacement_policy_json):packet.items[index].task.oracle_draft.cpd_policy;
  const basis=(entry,index)=>({subject_sha256:packet.items[index].task.subject_sha256,policy:policy(entry,index),atom_decisions:entry.form.atom_decisions,added_atoms_json:entry.form.added_atoms_json});
  const ids=(entry,index)=>(policy(entry,index).dimensions||[]).map((_,i)=>'dimension:'+i).concat('coverage');
  const latest=entry=>[...entry.edit_sources].reverse().find(e=>e.origin==='cpd_semantics');
  const fresh=(entry,index)=>{const r=latest(entry);return r&&equal(r.basis,basis(entry,index))&&equal(r.answers.map(a=>a.id),ids(entry,index));};
  function answers(entry,index){const r=latest(entry);return ids(entry,index).map(id=>({id,choice:fresh(entry,index)?r.answers.find(a=>a.id===id).choice:'',reason:r?.answers.find(a=>a.id===id)?.reason||''}));}
  function check(entry,index,required=false){
    const r=latest(entry);
    if(r){
      fail(equal(Object.keys(r).sort(),['answers','basis','origin','revision'])&&Number.isSafeInteger(r.revision)&&r.revision>=0&&r.revision<=entry.revision&&Array.isArray(r.answers),'CPD逐项记录损坏');
      const seen=new Set();for(const a of r.answers){fail(a&&equal(Object.keys(a).sort(),['choice','id','reason'])&&typeof a.id==='string'&&!seen.has(a.id)&&['','allow','restricted','uncertain'].includes(a.choice)&&typeof a.reason==='string','CPD逐项判断损坏');seen.add(a.id);}
    }
    if(required)fail(fresh(entry,index)&&r.answers.every(a=>a.choice==='allow'),'CPD逐项判断尚未完成、有异议或已失效');
  }
  function update(entry,index,id,choice,reason){
    if(!editable())return;
    const values=answers(entry,index),answer=values.find(a=>a.id===id);answer.choice=choice;answer.reason=reason;
    entry.issues=entry.issues.filter(i=>i.code!=='cpd_semantic_question');
    for(const a of values)if(['restricted','uncertain'].includes(a.choice))entry.issues.push({code:'cpd_semantic_question',cpd_question_id:a.id,cpd_choice:a.choice,reason:(a.id==='coverage'?'可变化细节是否遗漏':('变化项目 '+(Number(a.id.split(':')[1])+1)))+'：'+(a.choice==='restricted'?'认为草稿需要修改':'信息不足')+'。'+(a.reason.trim()||'尚未填写具体原因。')});
    markChanged('cpd_semantics');
    // Keep a single current source-bound answer snapshot, not an ever-growing form copy.
    entry.edit_sources=entry.edit_sources.filter(e=>e.origin!=='cpd_semantics');
    entry.edit_sources.push({origin:'cpd_semantics',revision:entry.revision,basis:clone(basis(entry,index)),answers:values});
  }
  function problems(entry,index){
    const out=[];
    if(!fresh(entry,index)||answers(entry,index).some(a=>a.choice===''))out.push('请逐项判断 CPD 中的可变化细节，修改要求后须重新核对');
    if(answers(entry,index).some(a=>['restricted','uncertain'].includes(a.choice)))out.push('CPD 中仍有异议，请保留进度并交给维护者处理');
    for(const dimension of policy(entry,index).dimensions||[]){const spec=lookup(packet.dictionary.cpd_catalog.dimensions,dimension.name);if(!spec)out.push('未登记 CPD 维度，须专家核对');if(spec?.bin_definition_m&&!equal(dimension.bin_definition_m,spec.bin_definition_m))out.push('距离分箱与现有提取器不一致，须专家处理');}
    return out;
  }
  function renderPanel(parent,entry,index){
    const current=policy(entry,index),catalog=packet.dictionary.cpd_catalog,values=answers(entry,index);
    parent.replaceChildren(node('h3','哪些细节可以变化？（CPD）'),node('p','请对照原文，判断下面的不同安排是否仍符合题意。这里只确认文字含义，不要求你检查程序规则或平台实现。'),node('p','这些变化不能放宽本页已经确认的必须满足项或禁止项。若原文明确了某个细节，就不能把它当成自由变化。'));
    if(latest(entry)&&!fresh(entry,index))parent.append(node('p','要求或策略已修改，先前的 CPD 选择已失效。原因文字仍保留，请重新逐项判断。','warning'));
    if(!(current.dimensions||[]).length)parent.append(node('p','草稿没有列出可变化的细节。请在下方检查是否有遗漏；不需要打开专家规则。'));
    const actors={ego_bus:'公交自车',motor_vehicle:'机动车',cyclist:'骑行者',pedestrian:'行人'};
    for(const a of values){
      const box=node('article',undefined,'cpd-question');box.dataset.questionId=a.id;
      if(a.id==='coverage')box.append(node('h4','是否遗漏了可以变化的细节？'),node('p','请核对：原文中是否还有未限定、可以有不同安排的细节，却没有列在上面？'));
      else {
        const i=Number(a.id.split(':')[1]),dimension=current.dimensions[i],spec=lookup(catalog.dimensions,dimension.name);
        box.append(node('h4',(i+1)+'. '+(spec?.label||('尚未解释的变化项目：'+dimension.name))));
        if(dimension.target_selector){const actor=dimension.target_selector.target_signature?.actor_class;box.append(node('p','这项变化针对：'+(lookup(actors,actor)||text(actor??null))+'。'));}
        if(dimension.name==='actor_longitudinal_distance_bin')box.append(node('p','这里比较场景开始时，该对象与公交车沿行驶方向的距离，不区分前方或后方。'));
        const options=(dimension.allowed_values||[]).map(v=>{if(dimension.name==='optional_road_feature_presence'&&typeof v==='string'){const parts=v.split(':');if(parts.length===2&&['present','absent'].includes(parts[1]))return (parts[1]==='present'?'场景中有':'场景中没有')+text(parts[0]);}return lookup(spec?.values||{},v)||text(v);});
        box.append(node('p','草稿允许的不同安排：'));const list=node('ul');for(const option of options)list.append(node('li',option));box.append(list,node('p','原文是否允许这些不同安排？请核对变化对象及每一种安排，不要从机器建议推断原文。'));
      }
      const label=node('label','你的判断'),select=node('select');select.className='cpd-choice';select.setAttribute('aria-label',a.id==='coverage'?'CPD遗漏判断':'CPD逐项判断');
      const choices=a.id==='coverage'?[['','请选择'],['allow','未发现遗漏'],['restricted','有可变化的细节被遗漏'],['uncertain','信息不足，无法判断']]:[['','请选择'],['allow','原文允许这些变化'],['restricted','原文限制了这些变化'],['uncertain','信息不足，无法判断']];
      for(const [value,title]of choices){const o=node('option',title);o.value=value;select.append(o);}select.value=a.choice;label.append(select);box.append(label);
      const reasonLabel=node('label','本项原因或修改建议'),reason=node('textarea');reason.className='cpd-reason';reason.rows=2;reason.value=a.reason;reason.placeholder='例如：原文指定在公交前方，不能允许改到后方。';reason.setAttribute('aria-label','本项原因或修改建议');reasonLabel.append(reason);reasonLabel.hidden=!['restricted','uncertain'].includes(a.choice)&&!a.reason;box.append(reasonLabel);
      select.onchange=()=>{update(entry,index,a.id,select.value,reason.value);render();};
      reason.oninput=()=>{update(entry,index,a.id,select.value,reason.value);document.getElementById('issue-list').textContent=entry.issues.map(i=>i.reason).join('\n');};
      if(['restricted','uncertain'].includes(a.choice))box.append(node('p','请在本项填写原因，不用重复写到全题备注。此题将保留为有疑问，修改策略由维护者处理。','muted'));
      parent.append(box);
    }
    parent.append(node('p','整题提交会记录你对上述可见问题的判断。目标选择、距离分箱和平台规则由维护者另行核对，未通过技术核对不能定稿。','muted'));
    const expert=node('details');expert.append(node('summary','维护者技术资料（标注者无需阅读）'));showTree(expert,current);const shared=node('details');shared.append(node('summary','共享维度规则'));showTree(shared,catalog);expert.append(shared);parent.append(expert);
  }
  return {check,problems,render:renderPanel};
}
