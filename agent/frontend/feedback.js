// Doctor-facing, persisted Agent feedback. Polling never invokes a model or reviews a candidate.
let agentFeedbackData={items:[],counts:{attention:0,pending_review:0,running:0}};
let agentFeedbackBusy=false,agentFeedbackInitialized=false;
const agentFeedbackSeen=new Set();
function agentFeedbackKey(item){return [item.loop_id,item.state,item.proposal_id,item.proposal_version,['pending_review','stale'].includes(item.state)?'':item.job_id].join(':');}
function agentFeedbackCards(items){
  return items.map(item=>`<article class="alert" style="display:block" data-agent-feedback="${item.loop_id}"><b>${html(item.patient)} · ${html(item.goal)} · ${html(item.title)}</b><p>${html(item.summary)}</p><button class="secondary" onclick="openAgentFeedbackTask(${item.loop_id})">${html(item.action_label)}</button>${item.child_loop_id?` <button class="secondary" onclick="openAgentFeedbackTask(${item.child_loop_id})">查看后续任务 #${item.child_loop_id}</button>`:''}</article>`).join('');
}
function agentTaskSummaryHtml(id){
  const item=agentFeedbackData.items.find(item=>item.loop_id===Number(id));
  return item?`<p><b>Agent：${html(item.title)}</b></p><p>${html(item.summary)}</p><button class="secondary" onclick="openAgentFeedbackTask(${id})">${html(item.action_label)}</button>`:'<p class="section-note">Agent 尚无调查记录；可在任务详情启用跟进。</p>';
}
async function refreshAgentFeedback(notify=true){
  if(agentFeedbackBusy)return;agentFeedbackBusy=true;
  try{
    const data=await workflowFetch('/agent/feedback?doctor_id=1');
    const fresh=data.items.filter(item=>item.requires_attention&&!agentFeedbackSeen.has(agentFeedbackKey(item)));
    for(const item of data.items)agentFeedbackSeen.add(agentFeedbackKey(item));
    if(notify&&agentFeedbackInitialized&&fresh.length)showToast(`Agent 有 ${fresh.length} 项新反馈需要您处理，请查看首页反馈中心。`);
    const changed=JSON.stringify(data)!==JSON.stringify(agentFeedbackData);
    agentFeedbackInitialized=true;agentFeedbackData=data;
    const count=document.getElementById('agentFeedbackCount');if(count)count.textContent=data.counts.attention+' 项待处理';
    const status=document.getElementById('agentFeedbackStatus');if(status)status.textContent=`待审核 ${data.counts.pending_review} 项 · 调查中 ${data.counts.running} 项 · 每 5 秒自动更新`;
    const home=document.getElementById('agentFeedbackHome');if(home&&(changed||!home.children.length))home.innerHTML=agentFeedbackCards(data.items.slice(0,5))||'<p class="section-note" style="padding:16px 20px">尚无 Agent 调查反馈。请在检验随访任务中启用 Agent，随后报告和病程事件会触发跟进。</p>';
    const inbox=document.getElementById('agentInboxList');if(inbox&&(changed||inbox.textContent.includes('正在读取')))inbox.innerHTML=agentFeedbackCards(data.items)||'<p>当前没有 Agent 调查记录。</p>';
    const inboxCount=document.getElementById('agentInboxCount');if(inboxCount)inboxCount.textContent=data.counts.attention+' 项需要医生处理';
    document.querySelectorAll('[data-agent-task-summary]').forEach(box=>{box.innerHTML=agentTaskSummaryHtml(Number(box.dataset.agentTaskSummary));});
  }catch(error){const status=document.getElementById('agentFeedbackStatus');if(status)status.textContent='反馈读取失败，保留上次结果：'+error.message;}
  finally{agentFeedbackBusy=false;}
}
async function openAgentInbox(){
  document.getElementById('panelTitle').textContent='Agent 工作反馈';
  document.getElementById('panelSub').textContent='负责患者的调查结果、待审核计划与失败处理';
  document.getElementById('panelBody').innerHTML='<p id="agentInboxCount"></p><div id="agentInboxList">正在读取反馈…</div>';
  document.getElementById('panelModal').classList.add('show');
  const button=document.getElementById('panelAction');button.disabled=false;button.textContent='刷新反馈';button.onclick=()=>refreshAgentFeedback(false);
  await refreshAgentFeedback(false);
}
async function openAgentFeedbackTask(id){
  await openTaskDetail(id);
  const panel=document.getElementById('agentWorkflowPanel');if(panel){panel.open=true;await loadAgentPanel(id);panel.scrollIntoView({behavior:'smooth',block:'start'});}
}
setInterval(()=>{if(document.visibilityState==='visible')refreshAgentFeedback();},5000);
