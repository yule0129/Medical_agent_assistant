// Poll observations only; never replace clinician editors or automatically complete a matter.
let matterProgressFeed={items:[],attention_count:0},matterProgressPolling=false;
const progressStates={WAITING:'仍在等待',RESPONSE_RECORDED:'发现响应记录',RESULT_AVAILABLE:'相关结果已到',CONFLICT:'资料存在冲突',PLAN_CHANGED:'计划可能已变化',UNCLEAR:'需要人工核对'};
const progressJobs={NOT_SCHEDULED:'待后台检查',PENDING:'等待后台调查',RUNNING:'Agent正在调查',RECOVERING:'调查中断，等待恢复',FAILED:'调查失败，可重试',STALE:'资料或计划已更新，等待重新调查'};
function extractionCard(s){
  const label=s.source_kind==='ADMISSION'?'入院问诊':'查房 #'+s.source_id;
  const message=s.status==='READY'?(s.candidate_count?`已提取 ${s.candidate_count} 项，待医生确认`:s.total_count?'提取的事项已由医生处理':'原文未发现明确待执行计划或待核实事项'):
    ({QUEUED:'资料已保存，自动提取已排队',RUNNING:'Agent正在自动提取事项',FAILED:'自动提取失败，原文已保留'+(s.retry_at?'，稍后自动重试':'，请进入事项页重试'),STALE:'原文或住院状态已变化，旧识别结果已失效'}[s.status]||s.status);
  return `<article class="alert" style="display:block" data-extraction-feedback="${s.scan_id}"><b>${html(s.patient)} · 候选事项提取</b><p class="section-note">来源：${html(label)}原文 · 此卡片展示提取结果，确认后进入待办</p><p>${html(message)}</p>${s.error_code?`<p>${html(s.error_code)}</p>`:''}<button class="secondary" onclick="openSavedSourceMatters(${s.patient_id},${s.encounter_id},'${s.source_kind}',${s.source_id})">${s.candidate_count?'核对并确认候选事项':'查看事项与识别记录'}</button></article>`;
}
function matterProgressHtml(item){
  const p=item.progress||{status:'NOT_SCHEDULED'},version=item.matter_version||item.version,id=item.matter_id||item.id;
  const closed=Boolean(item.status&&item.status!=='OPEN');
  const valid=p.status==='SUCCEEDED'&&!p.stale&&!closed;
  const pending=(p.actions||[]).filter(a=>a.status==='PROPOSED'&&!a.stale).length;
  return `<details class="matter-progress-details" data-matter-section="progress" data-progress-state="${html(p.state||p.status)}"><summary>Agent进展：${html(closed?'历史反馈':valid?progressStates[p.state]:progressJobs[p.status]||p.status)}${pending?` <span class="tag orange">${pending} 条建议待处理</span>`:''}</summary>
    ${valid?`<p class="section-note">下一步：${html(matterNextStep(p))}</p>`:''}
    ${p.summary?`<p>${html(p.summary)}</p>`:''}
    ${p.stale?'<p>以上是旧资料下的历史判断，不能用于确认当前进展。</p>':''}
    ${p.error_code?`<p>错误：${html(p.error_code)}${p.retry_at?' · 将延迟重试':''}</p>`:''}
    ${(p.evidence||[]).map((e,index)=>`<details><summary>${html(e.label)} · 版本 ${e.version} · ${html(localTime(e.at))}</summary><blockquote class="record-text">${html(e.quote)}</blockquote><button class="secondary" onclick="openMatterEvidence(${id},${index})">查看证据原文 ↗</button></details>`).join('')}
    ${p.coverage?`<p class="section-note">${html(p.coverage.scope)}${p.coverage.complete?'':'；资料有截断，须人工核对覆盖范围'}</p>`:''}
    <p class="section-note">此处为资料进展反馈，已阅不会完成事项。执行结论、取消或修改计划仍由医生明确记录。</p>
    ${valid&&!p.acknowledged?`<button class="secondary" onclick="ackMatterProgress(${id},${version},${p.result_id})">标记已阅</button>`:p.acknowledged?'<span class="tag green">医生已阅</span>':''}
    ${p.status==='FAILED'?`<button class="secondary" onclick="retryMatterProgress(${id},${version},${p.job_id},${p.attempt})">重试当前资料调查</button>`:''}
    ${closed?'':`<button class="secondary" onclick="checkMatterProgress(${id},${version})">检查当前资料</button>`}
    <details data-matter-section="suggestions"><summary>Agent行动建议${pending?' · '+pending+' 条待处理':'与处理历史'}</summary><p class="section-note">以下是对当前事项的操作建议。处理建议不会要求再次确认原事项。</p>${(p.actions||[]).map(a=>matterActionHtml(a,item)).join('')||'<p>当前没有可执行的下一步建议。</p>'}</details>
    <p id="matterProgressError-${id}" role="alert" style="color:#b42318"></p></details>`;
}
async function refreshMatterProgress(){
  if(matterProgressPolling||matterActionBusy||document.visibilityState==='hidden')return;
  matterProgressPolling=true;
  try{
    const data=await workflowFetch('/agent/matter-progress-feedback?doctor_id=1');matterProgressFeed=data;
    const extractions=data.extractions||[];
    refreshMatterWorkSummary(data);
    document.querySelectorAll('[data-extraction-encounter]').forEach(box=>{
      const rows=extractions.filter(s=>s.encounter_id===Number(box.dataset.extractionEncounter));
      box.innerHTML=rows.slice(0,5).map(extractionCard).join('')||'<p class="section-note">保存入院问诊或完成查房后，Agent会自动提取候选事项。</p>';
    });
    for(const item of data.items){
      const stored=matterView?.data.items.find(i=>i.id===item.matter_id);if(stored){stored.progress=item.progress;syncMatterWorkflow(item);}
      const box=document.getElementById('matterProgress-'+item.matter_id);
      if(box){const signature=JSON.stringify(item.progress);if(box.dataset.signature!==signature){const inputs=captureMatterInputs(box),opened=[...box.querySelectorAll('details[data-matter-section][open]')].map(e=>e.dataset.matterSection);box.innerHTML=matterProgressHtml(item);restoreMatterInputs(box,inputs);for(const e of box.querySelectorAll('details[data-matter-section]'))e.open=opened.includes(e.dataset.matterSection);box.dataset.signature=signature;}}
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
