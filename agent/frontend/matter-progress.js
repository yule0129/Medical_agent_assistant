// Poll observations only; never replace clinician editors or automatically complete a matter.
let matterProgressFeed={items:[],attention_count:0},matterProgressPolling=false;
const progressStates={WAITING:'仍在等待',RESPONSE_RECORDED:'发现响应记录',RESULT_AVAILABLE:'相关结果已到',CONFLICT:'资料存在冲突',PLAN_CHANGED:'计划可能已变化',UNCLEAR:'需要人工核对'};
const progressJobs={NOT_SCHEDULED:'待后台检查',PENDING:'等待后台调查',RUNNING:'Agent正在调查',RECOVERING:'调查中断，等待恢复',FAILED:'调查失败，可重试',STALE:'资料或计划已更新，等待重新调查'};
function matterProgressHtml(item){
  const p=item.progress||{status:'NOT_SCHEDULED'},version=item.matter_version||item.version,id=item.matter_id||item.id;
  const closed=Boolean(item.status&&item.status!=='OPEN');
  const valid=p.status==='SUCCEEDED'&&!p.stale&&!closed;
  return `<div class="draft" style="display:block" data-progress-state="${html(p.state||p.status)}"><b>Agent进展：${html(closed?'事项已结束 · 历史反馈':valid?progressStates[p.state]:progressJobs[p.status]||p.status)}</b>
    ${p.summary?`<p>${html(p.summary)}</p>`:''}
    ${p.stale?'<p>以上是旧资料下的历史判断，不能用于确认当前进展。</p>':''}
    ${p.error_code?`<p>错误：${html(p.error_code)}${p.retry_at?' · 将延迟重试':''}</p>`:''}
    ${(p.evidence||[]).map(e=>`<details><summary>${html(e.label)} · 版本 ${e.version} · ${html(localTime(e.at))}</summary><blockquote class="record-text">${html(e.quote)}</blockquote></details>`).join('')}
    ${p.coverage?`<p class="section-note">${html(p.coverage.scope)}${p.coverage.complete?'':'；资料有截断，须人工核对覆盖范围'}</p>`:''}
    <p class="section-note">此处为资料进展反馈，已阅不会完成事项。执行结论、取消或修改计划仍由医生明确记录。</p>
    ${valid&&!p.acknowledged?`<button class="secondary" onclick="ackMatterProgress(${id},${version},${p.result_id})">标记已阅</button>`:p.acknowledged?'<span class="tag green">医生已阅</span>':''}
    ${p.status==='FAILED'?`<button class="secondary" onclick="retryMatterProgress(${id},${version},${p.job_id},${p.attempt})">重试当前资料调查</button>`:''}
    ${closed?'':`<button class="secondary" onclick="checkMatterProgress(${id},${version})">检查当前资料</button>`}
    <details ${p.actions?.some(a=>a.status==='PROPOSED'&&!a.stale)?'open':''}><summary>下一步行动建议与审核历史</summary>${(p.actions||[]).map(a=>matterActionHtml(a,item)).join('')||'<p>当前没有可执行的下一步建议。</p>'}</details>
    <p id="matterProgressError-${id}" role="alert" style="color:#b42318"></p></div>`;
}
async function refreshMatterProgress(){
  if(matterProgressPolling||matterActionBusy||document.visibilityState==='hidden')return;
  matterProgressPolling=true;
  try{
    const data=await workflowFetch('/agent/matter-progress-feedback?doctor_id=1');matterProgressFeed=data;
    let home=document.getElementById('matterProgressHome');
    if(!home){
      const anchor=document.getElementById('agentFeedbackHome')?.closest('.card');
      if(anchor){const card=document.createElement('section');card.className='card';card.style.marginBottom='20px';card.innerHTML='<div class="card-head"><div class="card-title">事项跟进反馈</div><span id="matterProgressCount" class="tag orange"></span></div><div id="matterProgressHome" style="padding:16px"></div>';anchor.insertAdjacentElement('afterend',card);home=document.getElementById('matterProgressHome');}
    }
    const count=document.getElementById('matterProgressCount');if(count)count.textContent=data.attention_count+' 项待查看';
    if(home){const sorted=[...data.items].sort((a,b)=>Number(b.progress.needs_attention)-Number(a.progress.needs_attention));home.innerHTML=sorted.slice(0,5).map(i=>`<article class="alert" style="display:block" data-matter-feedback="${i.matter_id}"><b>${html(i.patient)} · ${html(i.title)}</b><p>${html(i.progress.status==='SUCCEEDED'?progressStates[i.progress.state]:progressJobs[i.progress.status]||i.progress.status)}</p><p>${html(i.progress.summary||'新问诊、检查或计划到期后，后台会核对本次住院资料。')}</p><button class="secondary" onclick="openPatientMatters(${i.patient_id},${i.encounter_id})">查看进展与证据</button></article>`).join('')||'<p class="section-note">尚无已确认的跟进事项。在患者详情中确认事项后，后台会持续核对新资料。</p>';}
    for(const item of data.items){
      const box=document.getElementById('matterProgress-'+item.matter_id);
      if(box){const signature=JSON.stringify(item.progress);if(box.dataset.signature!==signature){const inputs=captureMatterInputs(box);box.innerHTML=matterProgressHtml(item);restoreMatterInputs(box,inputs);box.dataset.signature=signature;}}
    }
  }catch(error){console.warn('事项进展反馈读取失败',error.message);}
  finally{matterProgressPolling=false;}
}
async function progressWrite(id,work){
  try{await work();await refreshMatterProgress();showToast('进展操作已保存，正式事项状态不变');}
  catch(error){const box=document.getElementById('matterProgressError-'+id);if(box)box.textContent=error.message;else showToast(error.message);}
}
function ackMatterProgress(id,version,result){return progressWrite(id,()=>postWorkflow(`/agent/matters/${id}/progress/acknowledge`,{doctor_id:1,expected_version:version,result_id:result}));}
function checkMatterProgress(id,version){return progressWrite(id,()=>postWorkflow(`/agent/matters/${id}/progress/check`,{doctor_id:1,expected_version:version}));}
function retryMatterProgress(id,version,job,attempt){return progressWrite(id,()=>postWorkflow(`/agent/matters/${id}/progress/retry`,{doctor_id:1,expected_version:version,job_id:job,expected_attempt:attempt}));}
setInterval(refreshMatterProgress,5000);
document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible')refreshMatterProgress();});
