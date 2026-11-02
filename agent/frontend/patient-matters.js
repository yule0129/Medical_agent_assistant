let matterView=null,matterBusy=false,matterRequest=0;
const matterKinds={VERIFY_INFO:'待核实信息',RECHECK:'复查计划',CONTACT:'联系／安排',OTHER:'其他待办'};
const matterStates={CANDIDATE:'待医生确认',OPEN:'跟进中',COMPLETED:'已完成',DISMISSED:'已忽略',CANCELLED:'已取消'};
function matterLocalInput(value){if(!value)return '';const d=new Date(value);return new Date(d-d.getTimezoneOffset()*60000).toISOString().slice(0,16);}
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
      <p>候选 ${data.items.filter(i=>i.status==='CANDIDATE').length} · 跟进中 ${data.items.filter(i=>i.status==='OPEN').length} · 已完成 ${data.items.filter(i=>i.status==='COMPLETED').length}</p>
      <section class="admission-section"><h3>从已保存的问诊中识别事项</h3>
      <select id="matterSource" onchange="showMatterSource()">${data.sources.map(s=>`<option value="${s.kind}:${s.id}">${html(s.label)} · ${html(localTime(s.at))}</option>`).join('')}</select>
      <button class="primary" id="scanPatientMatters" ${!data.sources.length||!data.model_configured?'disabled':''} onclick="scanPatientMatters()">Agent识别问题与计划</button>
      <p class="section-note">仅识别已保存原文中明确待核实或待执行的事项，先提出候选，由医生确认。不会生成查房总结。确认后的事项由后台核对新资料，反馈候选进展，正式完成仍由医生决定。</p>
      <details><summary>查看所选原文</summary><div class="record-text" id="matterSourceText"></div></details>
      ${!data.model_configured?'<p>模型尚未配置；已有事项仍可查看和处理。</p>':''}
      <p id="matterScanStatus" role="status"></p>
      ${data.scans.slice(0,3).map(s=>`<p class="section-note">识别 #${s.id} · ${html(s.status)} ${html(s.error_code||'')}</p>`).join('')}</section>
      <label>显示范围 <select id="matterFilter" onchange="openPatientMatters(${patientId},${p.encounter_id},this.value)"><option value="active">候选与跟进中</option><option value="done">已结束／已忽略</option><option value="all">全部事项</option></select></label>
      <div id="patientMatterCards">${items.map(i=>matterCard(i,data.owners)).join('')||'<p>当前范围暂无事项，可选择问诊原文让Agent识别。</p>'}</div><p id="matterError" role="alert" style="color:#b42318"></p>`;
    document.getElementById('matterFilter').value=filter;
    if(sourceKey&&data.sources.some(s=>s.kind+':'+s.id===sourceKey))document.getElementById('matterSource').value=sourceKey;
    showMatterSource();refreshMatterProgress();
    button.onclick=()=>openPatientMatters(patientId,p.encounter_id,filter,document.getElementById('matterSource')?.value);
  }catch(error){if(request===matterRequest&&box===document.getElementById('patientMatterView'))box.textContent='事项读取失败：'+error.message;}
}
function showMatterSource(){
  if(!matterView)return;
  const key=document.getElementById('matterSource')?.value;
  const source=matterView.data.sources.find(s=>s.kind+':'+s.id===key);
  document.getElementById('matterSourceText').textContent=source?.text||'尚无已保存的问诊原文';
}
function matterCard(i,owners){
  const editable=['CANDIDATE','OPEN'].includes(i.status),active=i.status==='OPEN';
  return `<article class="admission-section" data-patient-matter="${i.id}"><b>${html(matterKinds[i.kind])}</b> <span class="tag ${editable?'orange':'green'}">${html(matterStates[i.status])}</span>
    ${i.source_changed?'<p class="tag orange">来源已修改；旧候选须重新识别。已确认事项保留原始依据，不自动改写。</p>':''}
    <p><label>事项 <input id="matterTitle-${i.id}" value="${html(i.title)}" ${editable?'':'readonly'} /></label></p>
    <blockquote class="record-text">${html(i.quote)}</blockquote><p>来源：${html(i.source.label)} · ${html(localTime(i.source.at))} · 版本 ${i.source.version} · 记录医生 #${i.source.doctor_id||'未记录'}</p>
    <details><summary>查看识别时完整原文</summary><div class="record-text">${html(i.source.text)}</div></details>
    ${active||i.progress?.actions?.length?`<div id="matterProgress-${i.id}">${matterProgressHtml(i)}</div>`:''}
    ${i.parent_id?`<p>前序事项 M#${i.parent_id} · 本项由医生批准创建</p>`:''}
    ${i.children?.length?`<p>后续事项：${i.children.map(c=>'M#'+c.id+' '+html(c.title)+' · '+html(matterStates[c.status])).join('；')}</p>`:''}
    <p>原文时间表达：${html(i.due_text||'未指定')}。具体时间由医生安排。</p>
    ${editable?`<label>负责人 <select id="matterOwner-${i.id}">${owners.map(o=>`<option value="${o.id}" ${o.id===(i.assigned_doctor_id||1)?'selected':''}>${html(o.name)}</option>`).join('')}</select></label>
      <label>计划时间 <input type="datetime-local" id="matterDue-${i.id}" value="${matterLocalInput(i.due_at)}" /></label>
      <p><label>备注／实际完成情况 <input id="matterNote-${i.id}" placeholder="忽略、取消或完成时必填" /></label></p>
      ${active?`<button class="secondary matter-action" onclick="reviewPatientMatter(${i.id},'UPDATE')">保存安排</button> <button class="primary matter-action" onclick="reviewPatientMatter(${i.id},'COMPLETE')">记录完成情况</button> <button class="secondary matter-action" onclick="reviewPatientMatter(${i.id},'CANCEL')">取消事项</button>`:
      `<button class="primary matter-action" ${i.source_changed?'disabled':''} onclick="reviewPatientMatter(${i.id},'CONFIRM')">确认纳入跟进</button> <button class="secondary matter-action" onclick="reviewPatientMatter(${i.id},'DISMISS')">填写原因并忽略</button>`}`:
      `<p>负责人：${html(i.assigned_doctor_name||'未指派')} · 计划时间：${html(i.due_at?localTime(i.due_at):'待安排')}</p><p>处理记录：${html(i.resolution||'无')}</p>`}
    </article>`;
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
  const note=document.getElementById('matterNote-'+id)?.value||'',error=document.getElementById('matterError');
  if(['DISMISS','CANCEL','COMPLETE'].includes(action)&&!note.trim()){error.textContent='请填写忽略原因、取消原因或实际完成情况';return;}
  const due=document.getElementById('matterDue-'+id)?.value;
  matterBusy=true;
  try{
    await postWorkflow(`/agent/matters/${id}/review`,{doctor_id:1,expected_version:item.version,action,
      title:document.getElementById('matterTitle-'+id)?.value,assigned_doctor_id:Number(document.getElementById('matterOwner-'+id)?.value)||null,
      due_at:due?new Date(due).toISOString():null,note});
    if(view.box===document.getElementById('patientMatterView'))await openPatientMatters(view.patientId,view.encounterId,view.filter);
    await refreshDashboardTotals();showToast('事项已保存，原文依据和处理历史保留');
  }catch(exc){if(view.box===document.getElementById('patientMatterView'))error.textContent='操作失败：'+exc.message;}
  finally{matterBusy=false;}
}
function openSavedSourceMatters(patientId,encounterId,kind,id){
  if(kind==='ADMISSION'&&admissionCurrent&&document.getElementById('admissionTranscript')?.value.trim()!==admissionCurrent.data.agent.transcript){showToast('请先保存问诊文字修改，再识别事项');return;}
  if(kind==='ROUNDING'&&bedsideView&&document.getElementById('bedsideTranscript')?.value.trim()!==bedsideView.row?.transcript){showToast('请先暂存查房文字，再识别事项');return;}
  openPatientMatters(patientId,encounterId,'active',id?kind+':'+id:null);
}
