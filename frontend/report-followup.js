// All incoming report text is data; writes go through clinician/worker APIs.
let reportBusy=false;
let viewedReportTask=null;
const pendingReportImports=new Map();
const pendingReportWrites=new Map();
function reportWriteKey(kind,data) {
  const fingerprint=kind+JSON.stringify(data);
  if(!pendingReportWrites.has(fingerprint))pendingReportWrites.set(fingerprint,crypto.randomUUID());
  return pendingReportWrites.get(fingerprint);
}

function candidateMatchingHtml(item) {
  if(!['FOLLOW_RESULT','RECHECK'].includes(item.intent_type))return '';
  return `<details><summary>自动接收报告：补充匹配编号（可选）</summary><p>按项目与编号核对；不凭姓名或床号猜测标本。</p>
    <input id="candidateSpecimen-${item.id}" placeholder="标本号，如 BC-001" /> <input id="candidateOrder-${item.id}" placeholder="医嘱号（可选）" /></details>`;
}
function reportWorkflowHtml(t,data) {
  viewedReportTask={id:t.id,version:t.version};
  if(!['FOLLOW_RESULT','RECHECK'].includes(t.intent_type))return '';
  const c=t.matching_criteria||{},r=data.result||{};
  return `<section id="reportWorkflow"><h3>报告随访</h3><div id="taskRemoteUpdate" role="status"></div>
    <span id="taskEvidenceId" data-evidence-id="${data.evidence?.id||''}"></span>
    <p>${html(t.state_reason||'')}</p>
    ${r.report_id?`<p><b>报告 ${html(r.report_id)} · v${r.report_version}</b> · ${html(r.test_name)} · 标本 ${html(r.specimen_id||'未提供')} · 医嘱 ${html(r.order_id||'未提供')}</p><button class="secondary" onclick="showReportEvidence(${data.evidence.id})">查看报告证据与来源</button><div id="reportEvidenceView"></div>`:''}
    ${(data.findings||[]).map(f=>`<div class="draft" style="display:block" data-finding-id="${f.id}"><b>报告提醒 · ${html(f.status)}</b><p>${html(f.claim)}</p><small>${html(f.coverage)}</small>
      ${f.status==='PENDING_REVIEW'?`<p><input id="reviewReason-${f.id}" placeholder="审核备注（驳回必填）" /></p><button class="secondary" onclick="reviewReportFinding(${t.id},${f.id},${f.version},'ACCEPT')">接受提醒</button> <button class="secondary" onclick="reviewReportFinding(${t.id},${f.id},${f.version},'REJECT')">填写理由并驳回提醒</button><p>审核提醒不会确认已查看报告，也不会完成任务。</p>`:''}</div>`).join('')}
    ${t.status==='WAITING_EVENT'&&!t.current_evidence_id?`<details id="matchingForm"><summary>设置／补充报告匹配条件</summary><p><label>检验项目 <input id="matchTest" value="${html(c.test_name||t.waiting_for)}" /></label></p>
      <p><input id="matchSpecimen" value="${html(c.specimen_id||'')}" placeholder="标本号" /> <input id="matchOrder" value="${html(c.order_id||'')}" placeholder="医嘱号" /> <input id="matchReport" value="${html(c.report_id||'')}" placeholder="报告号" /></p>
      <button class="secondary" onclick="bindTaskReport(${t.id},${t.version})">保存匹配条件并回查已返回报告</button></details>`:''}
    ${!['CANCELLED','REJECTED'].includes(t.status)?`<details id="labImportForm"><summary>开发验证：模拟报告返回／修订</summary><p>仅导入合成报告；保存后自动核对并恢复匹配任务。</p>
      <p><label>报告号 <input id="labReportId" value="${html(r.report_id||c.report_id||'')}" placeholder="如 REPORT-001" /></label> <label>版本 <input id="labVersion" type="number" min="1" value="${(r.report_version||0)+1}" style="width:70px" /></label></p>
      <p><label>检验项目 <input id="labTest" value="${html(c.test_name||t.waiting_for)}" /></label></p>
      <p><input id="labSpecimen" value="${html(c.specimen_id||r.specimen_id||'')}" placeholder="标本号" /> <input id="labOrder" value="${html(c.order_id||r.order_id||'')}" placeholder="医嘱号" /></p>
      <textarea id="labResult" placeholder="填写合成报告原文"></textarea><button class="secondary" id="importLabButton" onclick="importTaskLab(${t.id},${t.patient_id},${t.encounter_id})">保存报告并自动跟进</button></details>`:''}
    ${['ACKNOWLEDGED','RESOLVED'].includes(t.status)?`<details id="followUpForm"><summary>确认建立后续随访（如等待药敏）</summary>
      <p><input id="followTest" placeholder="下一检验项目，如药敏" /> <input id="followSpecimen" value="${html(c.specimen_id||r.specimen_id||'')}" placeholder="标本号" /></p>
      <p><input id="followOrder" placeholder="医嘱号（可选）" /> <input id="followNote" placeholder="后续跟进说明（必填）" /></p>
      <button class="secondary" onclick="createReportFollowUp(${t.id},${t.version})">确认建立后续任务</button><p>沿用同一随访目标；当前任务的完成仍需单独记录结论。</p></details>`:''}
    <details ontoggle="if(this.open)showReportRuns(${t.id})"><summary>查看规则调查与事件恢复记录</summary><div id="reportRunView"></div></details>
    <details ontoggle="if(this.open)showReportJobs()"><summary>查看报告处理队列／失败重试</summary><div id="reportJobsView"></div></details>
    <p id="reportWorkflowError" role="alert" style="color:#b42318"></p></section>`;
}
function reportCriteria(prefix) {
  return {test_name:document.getElementById(prefix+'Test').value.trim(),
    specimen_id:document.getElementById(prefix+'Specimen')?.value.trim()||undefined,
    order_id:document.getElementById(prefix+'Order')?.value.trim()||undefined,
    report_id:document.getElementById(prefix+'Report')?.value.trim()||undefined};
}
async function reportWrite(work,taskId) {
  if(reportBusy)return;
  reportBusy=true;
  const note=document.getElementById('taskNote')?.value||'';
  try {
    const result=await work();
    if(result?.processing&&result.processing.status!=='SUCCEEDED')throw new Error('报告已保存，处理状态为 '+result.processing.status+'。请查看处理队列；临时失败会自动重试，重试耗尽后需明确重试。');
    await openTaskDetail(taskId);
    const editor=document.getElementById('taskNote');if(editor)editor.value=note;
    await refreshDashboardTotals();
  }catch(error){const box=document.getElementById('reportWorkflowError');if(box)box.textContent=error.message;else showToast(error.message);}
  finally{reportBusy=false;}
}
function bindTaskReport(id,version) {
  return reportWrite(()=>postWorkflow(`/loops/${id}/matching`,{doctor_id:1,expected_version:version,matching:reportCriteria('match')}),id);
}
function importTaskLab(id,patientId,encounterId) {
  return reportWrite(()=>{
    const data={doctor_id:1,patient_id:patientId,encounter_id:encounterId,
      report_id:document.getElementById('labReportId').value.trim(),report_version:Number(document.getElementById('labVersion').value),
      test_name:document.getElementById('labTest').value.trim(),specimen_id:document.getElementById('labSpecimen').value.trim()||undefined,
      order_id:document.getElementById('labOrder').value.trim()||undefined,result_text:document.getElementById('labResult').value.trim()};
    const fingerprint=JSON.stringify(data),previous=pendingReportImports.get(id);
    const payload=previous?.fingerprint===fingerprint?previous.payload:{...data,event_key:'UI-LAB-'+crypto.randomUUID(),reported_at:new Date().toISOString()};
    pendingReportImports.set(id,{fingerprint,payload});
    return postWorkflow('/lab-reports',payload);
  },id);
}
function reviewReportFinding(taskId,id,version,decision) {
  const data={doctor_id:1,expected_version:version,decision,reason:document.getElementById('reviewReason-'+id).value};
  return reportWrite(()=>postWorkflow(`/findings/${id}/reviews`,{...data,idempotency_key:reportWriteKey('review-'+id,data)}),taskId);
}
async function createReportFollowUp(id,version) {
  if(reportBusy)return;
  reportBusy=true;
  try {
    const data={doctor_id:1,expected_version:version,matching:reportCriteria('follow'),note:document.getElementById('followNote').value};
    const child=await postWorkflow(`/loops/${id}/follow-up`,{...data,request_key:reportWriteKey('follow-'+id,data)});
    showToast('已建立后续任务 #'+child.id);await openTaskDetail(child.id);await refreshDashboardTotals();
  }catch(error){document.getElementById('reportWorkflowError').textContent=error.message;}
  finally{reportBusy=false;}
}
async function showReportEvidence(id) {
  const box=document.getElementById('reportEvidenceView');
  try{const evidence=await workflowFetch(`/evidence/${id}?doctor_id=1`);if(box===document.getElementById('reportEvidenceView')){box.className='record-text';box.textContent=JSON.stringify(evidence,null,2);}}catch(error){if(box)box.textContent=error.message;}
}
async function showReportRuns(id) {
  const box=document.getElementById('reportRunView');
  try{const data=await workflowFetch(`/loops/${id}/runs?doctor_id=1`);if(box===document.getElementById('reportRunView')){box.className='record-text';box.textContent=JSON.stringify(data,null,2);}}catch(error){if(box)box.textContent=error.message;}
}
async function showReportJobs() {
  const box=document.getElementById('reportJobsView');
  try{const jobs=await workflowFetch('/event-jobs?doctor_id=1');if(box===document.getElementById('reportJobsView'))box.innerHTML=jobs.map(job=>`<p>事件 #${job.event_id} · ${html(job.status)} · 尝试 ${job.attempt} 次 ${job.last_error?html(job.last_error):''} ${job.status==='FAILED'?`<button class="secondary" onclick="retryReportJob(${job.id},${job.attempt})">明确重试该事件</button>`:''}</p>`).join('')||'暂无处理记录';}catch(error){if(box)box.textContent=error.message;}
}
function retryReportJob(id,attempt) {
  return reportWrite(async()=>({processing:await postWorkflow(`/event-jobs/${id}/retry`,{doctor_id:1,expected_attempt:attempt})}),viewedReportTask.id);
}

// Do not overwrite unsaved clinician input when a background event changes a task.
let reportPollBusy=false;
setInterval(async()=>{
  if(reportPollBusy||reportBusy||taskSaving||document.visibilityState!=='visible'||!document.getElementById('reportWorkflow')||!viewedReportTask)return;
  reportPollBusy=true;const watched={...viewedReportTask};
  try{
    const data=await workflowFetch(`/loops/${watched.id}/history?doctor_id=1`);
    const box=document.getElementById('taskRemoteUpdate');
    if(box&&viewedReportTask?.id===watched.id&&data.task.version!==watched.version){box.textContent='任务或报告有更新。请先保留未提交内容，再刷新核对。';const button=document.createElement('button');button.className='secondary';button.textContent='刷新到最新版本';button.onclick=()=>openTaskDetail(watched.id);box.append(button);}
  }catch(_){}finally{reportPollBusy=false;}
},8000);
