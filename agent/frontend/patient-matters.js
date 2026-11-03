let matterView=null,matterBusy=false,matterRequest=0;
const matterKinds={VERIFY_INFO:'待核实信息',RECHECK:'复查计划',CONTACT:'联系／安排',OTHER:'其他待办'};
const matterStates={CANDIDATE:'待确认',OPEN:'进行中',COMPLETED:'已完成',DISMISSED:'已忽略',CANCELLED:'已取消'};
function matterGroups(items,owners){
  const groups=[['待确认','核对Agent提取的事项，确认后进入正式待办。',i=>i.status==='CANDIDATE'],
    ['进行中','已进入正式待办；Agent建议在各事项的进展详情中单独处理。',i=>i.status==='OPEN'],
    ['已结束','保留完成、取消和忽略的记录。',i=>!['CANDIDATE','OPEN'].includes(i.status)]];
  return groups.map(([title,description,test])=>{const rows=items.filter(test);return rows.length?`<section class="matter-group"><h3>${title} <span class="tag blue">${rows.length}</span></h3><p class="section-note">${description}</p>${rows.map(i=>matterCard(i,owners)).join('')}</section>`:'';}).join('')||'<p class="section-note">当前范围暂无事项，保存问诊或完成查房后会自动提取。</p>';
}
function matterLocalInput(value){if(!value)return '';const d=new Date(value);return new Date(d-d.getTimezoneOffset()*60000).toISOString().slice(0,16);}
function matterQuoteContext(item){
  const text=String(item.source?.text||''),quote=String(item.quote||''),hit=quote?text.indexOf(quote):-1;
  if(hit<0)return html(quote||'未记录原文片段');
  // Expand the exact saved quotation, never the generated task title.
  // Sentence ends and speaker turns are boundaries; decimals remain intact.
  const ends=[0];
  for(const match of text.matchAll(/[。！？!?]+[”’」』"]*|\r?\n+/g))ends.push(match.index+match[0].length);
  if(ends[ends.length-1]!==text.length)ends.push(text.length);
  let first=0,last=0;
  while(first+1<ends.length&&ends[first+1]<=hit)first++;
  last=first;
  while(last+1<ends.length&&ends[last+1]<hit+quote.length)last++;
  let start=ends[Math.max(0,first-2)],end=ends[Math.min(ends.length-1,last+3)];
  // Unpunctuated transcripts may consist of one very long turn.
  if(ends.length===2){start=Math.max(0,hit-200);end=Math.min(text.length,hit+quote.length+200);}
  return (start>0?'…':'')+html(text.slice(start,hit))+'<mark>'+html(quote)+'</mark>'+html(text.slice(hit+quote.length,end))+(end<text.length?'…':'');
}
async function openPatientMatters(patientId,encounterId=null,filter='active',sourceKey=null){
  const request=++matterRequest;++patientDetailRequest;++historyRequest;
  document.getElementById('panelTitle').textContent='当前待跟进事项';document.getElementById('panelSub').textContent='正在读取…';
  document.getElementById('panelBody').innerHTML='<div id="patientMatterView">正在读取事项记忆…</div>';
  const box=document.getElementById('patientMatterView'),button=document.getElementById('panelAction');button.disabled=false;button.textContent='刷新事项';button.onclick=()=>openPatientMatters(patientId,encounterId,filter,sourceKey);
  document.getElementById('panelModal').classList.add('show');
  try{
    const p=await workflowFetch(`/patients/${patientId}/profile?doctor_id=1`+(encounterId?`&encounter_id=${encounterId}`:''));
    if(!p.encounter_id)throw new Error('暂无可用住院记录');
    const data=await workflowFetch(`/agent/encounters/${p.encounter_id}/matters?doctor_id=1`);
    if(request!==matterRequest||box!==document.getElementById('patientMatterView'))return;
    matterView={patientId,encounterId:p.encounter_id,filter,sourceKey,data,box};
    document.getElementById('panelTitle').textContent='待跟进事项 · '+p.name;
    document.getElementById('panelSub').textContent=p.encounter_number+' · 住院问题与计划';
    box.insertAdjacentHTML('beforebegin',patientTabs(patientId,'matters',p.encounter_id));
    const items=data.items.filter(i=>filter==='all'||(filter==='active'?['CANDIDATE','OPEN'].includes(i.status):['COMPLETED','DISMISSED','CANCELLED'].includes(i.status)));
    box.innerHTML=`<label>住院 <select id="matterEncounter" onchange="openPatientMatters(${patientId},Number(this.value))">${p.encounters.map(e=>`<option value="${e.id}" ${e.id===p.encounter_id?'selected':''}>${html(e.encounter_number)}</option>`).join('')}</select></label>
      <p class="matter-flow">待确认 ${data.items.filter(i=>i.status==='CANDIDATE').length} <span>→</span> 进行中 ${data.items.filter(i=>i.status==='OPEN').length} <span>→</span> 已结束 ${data.items.filter(i=>!['CANDIDATE','OPEN'].includes(i.status)).length}</p>
      <label>显示范围 <select id="matterFilter" onchange="openPatientMatters(${patientId},${p.encounter_id},this.value)"><option value="active">待确认与进行中</option><option value="done">已结束／已忽略</option><option value="all">全部事项</option></select></label>
      <div id="patientMatterCards">${matterGroups(items,data.owners)}</div><p id="matterError" role="alert" style="color:#b42318"></p><details class="matter-tools"><summary>自动提取状态与重试</summary>      <section><h3>自动提取状态</h3><div data-extraction-encounter="${p.encounter_id}">正在读取…</div></section>
      <section class="admission-section"><h3>手动重新识别／失败重试</h3>
      <select id="matterSource">${data.sources.map(s=>`<option value="${s.kind}:${s.id}">${html(s.label)} · ${html(localTime(s.at))}</option>`).join('')}</select>
      <button class="primary" id="scanPatientMatters" ${!data.sources.length||!data.model_configured?'disabled':''} onclick="scanPatientMatters()">Agent识别问题与计划</button>
      <p class="section-note">保存入院问诊、完成查房后会自动提取候选，无需点击识别。此按钮用于手动识别或失败重试。候选由医生确认后进入待办并自动跟进，不生成查房总结。</p>
      ${!data.model_configured?'<p>模型尚未配置；已有事项仍可查看和处理。</p>':''}
      <p id="matterScanStatus" role="status"></p>
      ${data.scans.slice(0,3).map(s=>`<p class="section-note">识别 #${s.id} · ${html(s.status)} ${html(s.error_code||'')}</p>`).join('')}</section>
</details>`;
    document.getElementById('matterFilter').value=filter;
    if(sourceKey&&data.sources.some(s=>s.kind+':'+s.id===sourceKey))document.getElementById('matterSource').value=sourceKey;
    refreshMatterProgress();
    button.onclick=()=>openPatientMatters(patientId,p.encounter_id,filter,document.getElementById('matterSource')?.value);
  }catch(error){if(request===matterRequest&&box===document.getElementById('patientMatterView'))box.textContent='事项读取失败：'+error.message;}
}
function matterCard(i,owners){
  const editable=['CANDIDATE','OPEN'].includes(i.status),active=i.status==='OPEN';
  return `<article class="admission-section matter-card" data-patient-matter="${i.id}"><div class="matter-card-heading"><b>${html(i.title)}</b> <span class="tag ${i.status==='CANDIDATE'?'orange':active?'blue':'green'}">事项：${html(matterStates[i.status])}</span></div>
    <p class="section-note">${html(matterKinds[i.kind])} · 负责人：${html(i.assigned_doctor_name||'待确认')} · 计划时间：${html(i.due_at?localTime(i.due_at):'待安排')}</p>
    ${i.source_changed?'<p class="tag orange">来源已修改；旧候选须重新识别。已确认事项保留原始依据，不自动改写。</p>':''}
    <div style="display:flex;align-items:center;justify-content:space-between;gap:12px"><span class="section-note">原文关键片段（含上下文）</span><button class="secondary" data-matter-source="${i.id}" onclick="openMatterSource(${i.id})">查看原文 ↗</button></div><blockquote class="record-text matter-quote">${matterQuoteContext(i)}</blockquote><p>来源：${html(i.source.label)} · ${html(localTime(i.source.at))}</p>
    ${active||i.progress?.actions?.length?`<div id="matterProgress-${i.id}">${matterProgressHtml(i)}</div>`:''}
    ${active?`<p><button class="primary matter-action" id="recordMatterCompletion-${i.id}" onclick="openMatterCompletion(${i.id})">记录完成情况</button></p>
      <section class="matter-completion" id="matterCompletion-${i.id}" hidden>
        <h4>记录实际完成情况</h4><p class="section-note">保存后，该事项将变为已完成并移出未完成待办。请记录实际执行结果；关联的其他事项不会自动完成。</p>
        <label for="matterCompletionNote-${i.id}">完成情况（必填）</label>
        <textarea id="matterCompletionNote-${i.id}" placeholder="请记录做了什么、执行结果及必要的后续交接。"></textarea>
        <p id="matterCompletionError-${i.id}" role="alert" style="color:#b42318"></p>
        <button class="primary matter-action" id="submitMatterCompletion-${i.id}" onclick="reviewPatientMatter(${i.id},'COMPLETE')">保存并完成事项</button>
        <button class="secondary matter-action" onclick="document.getElementById('matterCompletion-${i.id}').hidden=true">暂不完成</button>
      </section>`:''}
    ${i.parent_id?`<p>前序事项 M#${i.parent_id} · 本项由医生批准创建</p>`:''}
    ${i.children?.length?`<p>后续事项：${i.children.map(c=>'M#'+c.id+' '+html(c.title)+' · '+html(matterStates[c.status])).join('；')}</p>`:''}
    <details class="matter-editor"><summary>${i.status==='CANDIDATE'?'核对并确认事项':active?'修改安排／记录完成':'查看处理记录'}</summary>
    <p><label>事项 <input id="matterTitle-${i.id}" value="${html(i.title)}" ${editable?'':'readonly'} /></label></p>
    ${i.due_text?`<p>原文时间：${html(i.due_text)}，具体时间由医生安排。</p>`:''}
    ${editable?`<label>负责人 <select id="matterOwner-${i.id}">${owners.map(o=>`<option value="${o.id}" ${o.id===(i.assigned_doctor_id||1)?'selected':''}>${html(o.name)}</option>`).join('')}</select></label>
      <label>计划时间 <input type="datetime-local" id="matterDue-${i.id}" value="${matterLocalInput(i.due_at)}" /></label>
      <p><label>备注／实际完成情况 <input id="matterNote-${i.id}" placeholder="忽略、取消或完成时必填" /></label></p>
      ${active?`<button class="secondary matter-action" onclick="reviewPatientMatter(${i.id},'UPDATE')">保存安排</button> <button class="secondary matter-action" onclick="reviewPatientMatter(${i.id},'CANCEL')">取消事项</button>`:
      `<button class="primary matter-action" ${i.source_changed?'disabled':''} onclick="reviewPatientMatter(${i.id},'CONFIRM')">确认纳入跟进</button> <button class="secondary matter-action" onclick="reviewPatientMatter(${i.id},'DISMISS')">填写原因并忽略</button>`}`:
      `<p>负责人：${html(i.assigned_doctor_name||'未指派')} · 计划时间：${html(i.due_at?localTime(i.due_at):'待安排')}</p><p class="record-text">处理记录：${html(i.resolution||'无')}</p><p class="section-note">记录时间：${html(localTime(i.updated_at))}</p>`}
    </details><p id="matterItemError-${i.id}" role="alert" style="color:#b42318"></p></article>`;
}
function openMatterCompletion(id){
  const section=document.getElementById('matterCompletion-'+id),field=document.getElementById('matterCompletionNote-'+id);
  if(!section||matterBusy)return;
  section.hidden=false;
  if(!field.value)field.value=document.getElementById('matterNote-'+id)?.value||'';
  document.getElementById('matterCompletionError-'+id).textContent='';
  section.scrollIntoView({block:'center'});field.focus({preventScroll:true});
}
async function openMatterSource(id){
  const view=matterView,item=view?.data.items.find(i=>i.id===id);
  if(!item||matterBusy||matterActionBusy)return;
  const edits=captureMatterInputs(view.box),scroll=document.getElementById('panelBody').scrollTop;
  let target;
  if(item.source.kind==='ADMISSION'){
    await openAdmissionRecord(view.patientId,view.encounterId);
    target=document.getElementById('admissionTranscript');
  }else if(item.source.kind==='ROUNDING'){
    await openRoundingHistory(item.source.id,view.patientId,view.encounterId);
    target=document.getElementById('bedsideTranscript')||document.getElementById('historicalTranscript');
  }else{
    // Approved follow-ups may originate from a report or a progress event.
    const kind={LAB:'lab_report',PROGRESS_NOTE:'clinical_event'}[item.source.kind];
    if(!kind){showToast('暂不支持此来源跳转');return;}
    try{
      const source=await workflowFetch(`/agent/sources/${kind}/${item.source.id}?doctor_id=1`);
      if(view.box!==document.getElementById('patientMatterView'))return;
      document.getElementById('panelTitle').textContent=item.source.label+' · 原文';
      document.getElementById('panelSub').textContent='来源 #'+item.source.id+' · '+localTime(source.observed_at);
      document.getElementById('panelBody').innerHTML='<div class="record-text" id="matterOriginalSource"></div>';
      target=document.getElementById('matterOriginalSource');
      target.textContent=kind==='clinical_event'?JSON.parse(source.text).text||source.text:source.text;
      const action=document.getElementById('panelAction');action.textContent='返回待跟进事项';action.disabled=false;
    }catch(error){showToast('原文读取失败：'+error.message);return;}
  }
  if(!target){showToast('未能打开对应原文，请核对来源是否仍可访问');return;}
  const originalValue='value' in target?target.value:null;
  const back=document.createElement('button');back.className='secondary';back.textContent='← 返回待跟进事项';back.id='backToMatterSource';
  back.onclick=async()=>{
    if(originalValue!==null&&target.value!==originalValue){showToast('原文有修改，请先保存，再通过患者页返回待跟进事项');return;}
    await openPatientMatters(view.patientId,view.encounterId,view.filter,view.sourceKey);
    if(matterView?.box===document.getElementById('patientMatterView')&&matterView.encounterId===view.encounterId){
      restoreMatterInputs(matterView.box,edits);document.getElementById('panelBody').scrollTop=scroll;
    }
  };
  document.getElementById('panelBody').prepend(back);
  if(document.getElementById('matterOriginalSource'))document.getElementById('panelAction').onclick=back.onclick;
  const text=originalValue===null?target.textContent:originalValue,index=text.indexOf(item.quote);
  const note=document.createElement('p');note.className='section-note';note.id='matterSourceLocation';
  note.textContent=index<0?'当前原文中未找到该片段，来源可能已更新，请核对记录版本。':item.source_changed?'来源已有更新，已定位当前原文中的对应片段，请核对版本。':'已定位到事项引用的原文片段。';
  target.before(note);
  if(index>=0){
    if(originalValue!==null){
      target.focus();target.setSelectionRange(index,index+item.quote.length);
      const lines=text.slice(0,index).split('\n').length-1,total=text.split('\n').length;
      target.scrollTop=Math.max(0,(target.scrollHeight-target.clientHeight)*lines/Math.max(1,total-1));
    }else{
      const mark=document.createElement('mark');mark.textContent=item.quote;
      target.replaceChildren(document.createTextNode(text.slice(0,index)),mark,document.createTextNode(text.slice(index+item.quote.length)));
    }
  }
  target.scrollIntoView({block:'center'});
}
async function scanPatientMatters(){
  if(matterBusy||!matterView)return;const view=matterView;
  const key=document.getElementById('matterSource').value,s=view.data.sources.find(s=>s.kind+':'+s.id===key);
  if(!s)return;
  matterBusy=true;document.getElementById('scanPatientMatters').disabled=true;document.getElementById('matterScanStatus').textContent='Agent正在识别明确的问题与计划，原文不会被修改…';
  try{
    const result=await postWorkflow(`/agent/encounters/${view.encounterId}/matters/scan`,{doctor_id:1,source_kind:s.kind,source_id:s.id,expected_digest:s.digest});
    if(view===matterView&&view.box===document.getElementById('patientMatterView'))await openPatientMatters(view.patientId,view.encounterId,view.filter,key);
    showToast(result.candidate_count?`已识别 ${result.candidate_count} 项，请医生核对确认`:'该段原文未识别到明确待跟进事项');
  }catch(error){if(view.box===document.getElementById('patientMatterView')){document.getElementById('matterScanStatus').textContent='识别失败：'+error.message;document.getElementById('scanPatientMatters').disabled=false;}}
  finally{matterBusy=false;}
}
async function reviewPatientMatter(id,action){
  if(matterBusy||!matterView)return;
  const view=matterView,item=view.data.items.find(i=>i.id===id);if(!item)return;
  const completion=document.getElementById('matterCompletion-'+id),completionField=document.getElementById('matterCompletionNote-'+id);
  const field=action==='COMPLETE'&&completion&&!completion.hidden?completionField:document.getElementById('matterNote-'+id);
  const note=field?.value||'',error=document.getElementById(action==='COMPLETE'?'matterCompletionError-'+id:'matterItemError-'+id)||document.getElementById('matterError');
  if(['DISMISS','CANCEL','COMPLETE'].includes(action)&&!note.trim()){
    if(action==='COMPLETE')openMatterCompletion(id);
    error.textContent=action==='COMPLETE'?'请填写实际完成情况，再保存并完成事项。':action==='CANCEL'?'请填写取消原因。':'请填写忽略原因。';
    const input=action==='COMPLETE'?completionField:field;input?.focus();error.scrollIntoView({block:'center'});return;
  }
  const due=document.getElementById('matterDue-'+id)?.value;
  const preserved=captureMatterInputs(view.box),controls=[...view.box.querySelectorAll('.matter-action')].map(button=>({button,disabled:button.disabled}));
  matterBusy=true;error.textContent='';controls.forEach(({button})=>button.disabled=true);
  const submit=document.getElementById('submitMatterCompletion-'+id);if(action==='COMPLETE'&&submit)submit.textContent='正在保存…';
  let saved=false;
  try{
    await postWorkflow(`/agent/matters/${id}/review`,{doctor_id:1,expected_version:item.version,action,
      title:document.getElementById('matterTitle-'+id)?.value,assigned_doctor_id:Number(document.getElementById('matterOwner-'+id)?.value)||null,
      due_at:due?new Date(due).toISOString():null,note});
    saved=true;
    if(view.box===document.getElementById('patientMatterView')){
      await openPatientMatters(view.patientId,view.encounterId,action==='COMPLETE'?'all':view.filter,view.sourceKey);
      if(matterView?.box===document.getElementById('patientMatterView')){
        restoreMatterInputs(matterView.box,preserved,preserved.filter(v=>v.id.endsWith('-'+id)||v.id==='matterFilter').map(v=>v.id));
        if(action==='COMPLETE'){
          const card=document.querySelector('[data-patient-matter="'+id+'"]');
          if(card){card.querySelector('.matter-editor').open=true;card.scrollIntoView({block:'center'});}
        }
      }
    }
    await refreshDashboardTotals();showToast(action==='COMPLETE'?'完成情况已保存，事项已完成并移出未完成待办':'事项已保存，原文依据和处理历史保留');
  }catch(exc){
    if(saved)showToast('事项已保存，页面刷新失败，请刷新查看已保存结果');
    else if(view.box===document.getElementById('patientMatterView')){error.textContent='保存失败：'+exc.message+'。填写内容已保留。';error.scrollIntoView({block:'center'});}
    else showToast('保存失败：'+exc.message);
  }
  finally{matterBusy=false;controls.forEach(({button,disabled})=>{if(button.isConnected)button.disabled=disabled;});if(submit?.isConnected)submit.textContent='保存并完成事项';}
}
function openSavedSourceMatters(patientId,encounterId,kind,id){
  if(kind==='ADMISSION'&&admissionCurrent&&document.getElementById('admissionTranscript')?.value.trim()!==admissionCurrent.data.agent.transcript){showToast('请先保存问诊文字修改，再识别事项');return;}
  if(kind==='ROUNDING'&&bedsideView&&document.getElementById('bedsideTranscript')?.value.trim()!==bedsideView.row?.transcript){showToast('请先暂存查房文字，再识别事项');return;}
  openPatientMatters(patientId,encounterId,'active',id?kind+':'+id:null);
}
