// Model investigations are real queued operations; no simulated model success.
let agentPanelTask=null,agentPanelData=null,agentPanelBusy=false;
const agentDrafts=new Map();
function agentWorkflowHtml(t) {
  return `<details id="agentWorkflowPanel" open ontoggle="if(this.open)loadAgentPanel(${t.id})"><summary><b>医疗 Agent 调查</b></summary><div id="agentPanelContent">正在读取 Agent 状态…</div><div id="agentPanelError" role="alert" style="color:#b42318"></div></details>`;
}
async function loadAgentPanel(id) {
  const box=document.getElementById('agentPanelContent');if(!box)return;
  try {
    const data=await workflowFetch(`/agent/loops/${id}?doctor_id=1`);
    if(box!==document.getElementById('agentPanelContent'))return;
    const savedFields=[...box.querySelectorAll('input[id],textarea[id]')].map(node=>({id:node.id,value:node.value,focused:node===document.activeElement,start:node.selectionStart,end:node.selectionEnd}));
    for(const field of savedFields){const key=id+':'+field.id;if(field.value)agentDrafts.set(key,field.value);else agentDrafts.delete(key);}
    const opened=[...box.querySelectorAll('details[open]')].map(node=>node.querySelector('summary')?.textContent);
    agentPanelTask=id;agentPanelData=data;
    const proposals=data.proposals||[],runs=data.runs||[];
    box.innerHTML=`<div data-agent-task-summary="${id}">${agentTaskSummaryHtml(id)}</div><p><b>${html(data.provider)} / ${html(data.model)}</b> · ${data.configured?'已配置':'待配置'}</p><p>${html(data.message)}</p>${data.start_block_reason?`<p role="status">${html(data.start_block_reason)}</p>`:''}
      <p>此目标：${data.memory?.agent_enabled?'已启用事件跟进':'尚未启用'} · 当前停止原因 ${html(data.memory?.agent_stop_reason||'尚无模型运行')}</p>
      <button class="secondary" id="agentStartButton" ${data.configured&&data.start_ready?'':'disabled'} onclick="startAgentInvestigation(${id},${data.loop_version})">启用 Agent 并调查</button>
      <button class="secondary" onclick="loadAgentPanel(${id})">刷新调查结果</button><p>启用后报告、病程更新及到期事件由后台 Worker 唤醒；医生确认和任务完成仍需明确操作。</p>
      ${(data.jobs||[]).map(j=>`<p>调查 #${j.id} · ${html(j.status)} · ${html(j.result?.stop_reason||'')} ${j.status==='FAILED'?`<button class="secondary" onclick="retryAgentInvestigation(${j.id})">重试调查</button>`:''}</p>`).join('')}
      <h4>Agent 审核候选</h4>${proposals.map(p=>`<article class="draft" style="display:block" data-agent-proposal="${p.id}"><b>${html(p.candidate.kind)} · ${html(p.status)} ${p.stale?'· 任务版本已变化，需重新调查':''}</b><p>${html(p.candidate.summary)}</p>
        ${(p.snapshots||[]).map((s,i)=>`<details><summary>证据 ${html(s.source_type)} #${s.source_id} · v${s.source_version}</summary><pre style="white-space:pre-wrap">${html(s.text)}</pre><button class="secondary" onclick="showAgentSource('${s.source_type}',${s.source_id})">读取当前来源核对</button></details>`).join('')}
        <details><summary>实际检索覆盖</summary><pre style="white-space:pre-wrap">${html(JSON.stringify(p.candidate.searched_sources,null,2))}</pre></details>
        ${p.status==='PENDING_REVIEW'&&!p.stale?`<p><input id="agentReviewNote-${p.id}" placeholder="医生审核备注（驳回必填）" /></p>${!['FOLLOW_UP','RESPONSE_FOUND'].includes(p.candidate.kind)?`<button class="secondary" onclick="reviewAgentProposal(${p.id},'ACCEPT','REVIEW_ONLY')">接受调查发现（不确认结果）</button>`:''} <button class="secondary" onclick="reviewAgentProposal(${p.id},'REJECT','REVIEW_ONLY')">填写理由并驳回</button>
        ${p.candidate.kind==='RESPONSE_FOUND'?`<button class="secondary" onclick="reviewAgentProposal(${p.id},'ACCEPT','CONFIRM_RESPONSE')">核对本次报告并确认已查看</button>`:''}
        ${p.candidate.kind==='FOLLOW_UP'?`<p>后续计划：${html(p.candidate.follow_up?.test_name)}</p><button class="secondary" onclick="reviewAgentProposal(${p.id},'ACCEPT','CREATE_FOLLOW_UP')">核对报告、确认查看并批准后续任务</button>`:''}`:''}
        ${p.child_loop_id?`<button class="secondary" onclick="openTaskDetail(${p.child_loop_id})">打开后续任务 #${p.child_loop_id}</button>`:''}</article>`).join('')||'<p>暂无模型候选。</p>'}
      <div id="agentSourceView"></div><h4>模型实际运行轨迹</h4>${runs.map(r=>`<details data-agent-run="${r.id}"><summary>Run #${r.id} · ${html(r.provider)} · ${html(r.stop_reason||r.status)} · Plan ${r.plan_version} · 前序 ${r.prior_run_id||'无'}</summary><p>触发事件 #${r.trigger_event_id}</p>${r.steps.map(s=>`<details><summary>${s.number}. ${html(s.action)} ${html(s.tool_name||'')} · ${s.duration_ms||0} ms</summary><pre style="white-space:pre-wrap">${html(JSON.stringify({input:s.input,result:s.result},null,2))}</pre></details>`).join('')}</details>`).join('')||'<p>尚未运行真实模型。</p>'}
      <h4>补充医生病程</h4><textarea id="agentProgressText" placeholder="例如：已知晓本次血培养报告，继续等待药敏。保存为草稿，Agent 仍需提出候选供审核。"></textarea>
      <button class="secondary" onclick="saveAgentProgress(${id},${data.loop_version})">保存病程草稿并触发跟进</button>`;
    for(const field of savedFields){const node=document.getElementById(field.id);if(node&&box.contains(node)){node.value=field.value;if(field.focused){node.focus({preventScroll:true});if(field.start!==null)node.setSelectionRange(field.start,field.end);}}}
    box.querySelectorAll('details').forEach(node=>{if(opened.includes(node.querySelector('summary')?.textContent))node.open=true;});
    const orphans=[...agentDrafts].filter(([key,value])=>key.startsWith(id+':')&&!document.getElementById(key.slice(key.indexOf(':')+1)));
    if(orphans.length){const note=document.createElement('details');const title=document.createElement('summary');title.textContent='已暂存未提交的审核备注（候选可能已更新）';note.append(title);for(const [key,value] of orphans){const text=document.createElement('pre');text.style.whiteSpace='pre-wrap';text.textContent=value;note.append(text);}box.append(note);}
  }catch(error){if(box===document.getElementById('agentPanelContent'))box.textContent='Agent 状态读取失败：'+error.message;}
}
async function agentWrite(work) {
  if(agentPanelBusy)return;agentPanelBusy=true;
  const errorBox=document.getElementById('agentPanelError');if(errorBox)errorBox.textContent='';
  const note=document.getElementById('taskNote')?.value||'';
  try {const result=await work();showToast(result?.child_loop_id?'已批准后续任务 #'+result.child_loop_id+'；原任务处理完成仍需单独记录。':result?.status==='ACCEPTED'?'已保存审核决定；任务状态以实际确认和处理操作为准。':result?.job_id?'调查已排队，完成后会自动反馈到首页和当前任务。':'已保存，Agent 将继续跟进。');await openTaskDetail(agentPanelTask);const editor=document.getElementById('taskNote');if(editor)editor.value=note;await loadAgentPanel(agentPanelTask);await refreshAgentFeedback(false);await refreshDashboardTotals();return result;}
  catch(error){const box=document.getElementById('agentPanelError');if(box)box.textContent=error.message;else showToast(error.message);}
  finally{agentPanelBusy=false;}
}
function startAgentInvestigation(id,version) {
  return agentWrite(()=>postWorkflow(`/agent/loops/${id}/runs`,{doctor_id:1,expected_version:version,request_key:crypto.randomUUID()}));
}
function retryAgentInvestigation(id) {return agentWrite(()=>postWorkflow(`/agent/jobs/${id}/retry`,{doctor_id:1}));}
function reviewAgentProposal(id,decision,effect) {
  const p=agentPanelData.proposals.find(p=>p.id===id);
  return agentWrite(()=>postWorkflow(`/agent/proposals/${id}/review`,{doctor_id:1,expected_version:p.version,expected_loop_version:agentPanelData.loop_version,
    evidence_id:p.evidence_id,decision,effect,note:document.getElementById('agentReviewNote-'+id)?.value||''}));
}
function saveAgentProgress(id,version) {
  const text=document.getElementById('agentProgressText').value;
  return agentWrite(()=>postWorkflow(`/agent/loops/${id}/progress`,{doctor_id:1,expected_version:version,text,request_key:reportWriteKey('agent-progress-'+id,{text})}));
}
async function showAgentSource(kind,id) {
  const box=document.getElementById('agentSourceView');try {const source=await workflowFetch(`/agent/sources/${kind}/${id}?doctor_id=1`);if(box===document.getElementById('agentSourceView')){box.className='record-text';box.textContent=JSON.stringify(source,null,2);}}catch(error){if(box)box.textContent=error.message;}
}
// Refresh actual results in place. Existing clinical inputs retain their contents and focus.
let agentPanelPolling=false;
setInterval(async()=>{
  const panel=document.getElementById('agentWorkflowPanel');
  if(agentPanelPolling||agentPanelBusy||!agentPanelTask||!panel?.open||document.visibilityState!=='visible')return;
  agentPanelPolling=true;
  try {await loadAgentPanel(agentPanelTask);} finally {agentPanelPolling=false;}
},5000);

async function openAgentHandoff(encounterId,create=true) {
  const box=document.getElementById('panelBody');
  try {
    if(create)await postWorkflow(`/encounters/${encounterId}/handoffs`,{doctor_id:1});
    const reports=await workflowFetch(`/agent/encounters/${encounterId}/handoffs?doctor_id=1`);
    box.innerHTML=`<p>当前住院交接快照，已确认版本保留；确认交接不会完成任务。</p>${reports.map((r,i)=>`<details ${i===0?'open':''}><summary>交接 #${r.id} · v${r.version} · ${html(r.status)}</summary><textarea id="handoffContent-${r.id}">${html(r.content)}</textarea>
      <button class="secondary" onclick="reviseAgentHandoff(${encounterId},${r.id},${r.version})">保存为新版本</button>
      ${r.status==='DRAFT'?`<button class="secondary" onclick="confirmAgentHandoff(${encounterId},${r.id},${r.version})">确认此交接快照</button>`:''}
      ${(r.references||[]).filter(ref=>ref.loop_id).map(ref=>`<button class="secondary" onclick="openTaskDetail(${ref.loop_id})">任务 #${ref.loop_id}${ref.evidence_id?' · 证据 #'+ref.evidence_id:''}</button>`).join('')}</details>`).join('')||'暂无交接记录'}<p id="agentHandoffError" role="alert"></p>`;
  }catch(error){box.textContent='交接读取失败：'+error.message;}
}
async function reviseAgentHandoff(encounterId,id,version) {
  try {await postWorkflow(`/agent/handoffs/${id}/revisions`,{doctor_id:1,expected_version:version,content:document.getElementById('handoffContent-'+id).value});await openAgentHandoff(encounterId,false);}catch(error){document.getElementById('agentHandoffError').textContent=error.message;}
}
async function confirmAgentHandoff(encounterId,id,version) {
  try {const reports=await workflowFetch(`/agent/encounters/${encounterId}/handoffs?doctor_id=1`);const saved=reports.find(r=>r.id===id);
    if(saved.content!==document.getElementById('handoffContent-'+id).value)throw new Error('请先保存编辑内容为新版本，再确认该版本');
    await postWorkflow(`/agent/handoffs/${id}/confirm`,{doctor_id:1,expected_version:version,request_key:'handoff-'+id});await openAgentHandoff(encounterId,false);
  }catch(error){document.getElementById('agentHandoffError').textContent=error.message;}
}
