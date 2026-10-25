// Persisted rounding, records and task scheduling. No fallback writes to demo data.
let roundRequestKey = null;
let roundSaving = false;
let workflowIntents = [];
let workflowTasks = [];
let tasksView = false;
let roundDraftText = '';
let currentRoundingId = null;
let taskSaving = false;
let scheduleFilter = 'pending';
const html = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const localTime = value => value ? new Date(value).toLocaleString('zh-CN') : '待安排';
const taskType = type => ({FOLLOW_RESULT:'跟进结果',RECHECK:'复查',MONITOR:'监测观察',ACTION:'联系／安排'}[type] || type);

async function workflowFetch(path, options) {
  const response = await fetch(API_BASE + path, options);
  if (!response.ok) {
    let detail = 'HTTP ' + response.status;
    try { const data = await response.json(); detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail); } catch (_) {}
    throw new Error(detail);
  }
  return response.json();
}
const postWorkflow = (path, data) => workflowFetch(path, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)});

function recordTable(list) {
  if(!list.length) return '<p class="section-note">暂无符合条件的病历文书。</p>';
  return '<table class="clinical-table"><thead><tr><th>患者</th><th>住院号</th><th>文书类型</th><th>时间</th><th>状态</th></tr></thead><tbody>'+list.map(r=>`<tr onclick="openMedicalRecord(${Number(r.id)})"><td>${html(r.patient)} · ${html(r.bed)}</td><td>${html(r.admission_number)}</td><td>${html(r.record_type)}</td><td>${html(r.time)}</td><td>${html(r.status)}</td></tr>`).join('')+'</tbody></table>';
}
async function saveMedicalRecord(id) {
  try {
    await workflowFetch('/medical-records/'+id,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({content:document.getElementById('recordEditor').value})});
    await openMedicalRecord(id,activeRecordHistory); showToast('病历修改已保存到数据库');
  }catch(error){showToast('保存失败：'+error.message);}
}
async function signMedicalRecord(id) {
  try {await postWorkflow('/medical-records/'+id+'/sign',{}); await openMedicalRecord(id,activeRecordHistory);showToast('模拟签署已保存');}
  catch(error){showToast('签署失败：'+error.message);}
}

async function loadBackendPatients() {
  const data = await workflowFetch('/patients?doctor_id=1');
  patients.splice(0, patients.length, ...data.map(p => ({name:p.name, id:p.admission_number,
    bed:p.bed, diagnosis:p.diagnosis, backendPatientId:p.id, encounterId:p.encounter_id,
    encounterNumber:p.encounter_number, gender:p.gender, age:p.age, todayRoundings:p.today_rounding_count||0, todayCompleted:p.today_completed_rounding_count||0, status:p.rounding_status||'待查房', tone:p.rounding_status==='已完成'?'green':'orange', latestRoundingId:p.latest_rounding_id})));
  if (!patients.some(p => p.encounterId === currentEncounterId) && !patients.some(p=>p.backendPatientId===workflowPatientId)) currentEncounterId = patients[0]?.encounterId;
  renderPatients();
  const total=document.getElementById('patientTotal'); if(total)total.textContent=patients.length;
  const completed=document.getElementById('roundCompleted'); if(completed)completed.textContent=patients.reduce((n,p)=>n+p.todayCompleted,0);
  const hint=document.getElementById('roundProgress'); if(hint)hint.textContent=patients.reduce((n,p)=>n+p.todayRoundings-p.todayCompleted,0)+' 次待结束';
  const date=new Date().toLocaleDateString('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'long',day:'numeric',weekday:'long'});
  const heading=document.querySelector('.page-heading .date');if(heading)heading.textContent=date+' · '+patients.filter(p=>p.status!=='已完成').length+' 位患者待查房或待结束';
  const dateLabel=document.getElementById('workDate');if(dateLabel)dateLabel.textContent=new Date().toLocaleDateString('zh-CN',{timeZone:'Asia/Shanghai'});
}
async function refreshDashboardTotals() {
  try {
    const [records,tasks]=await Promise.all([workflowFetch('/medical-records'),workflowFetch('/tasks?doctor_id=1')]);
    const pendingRecords=document.getElementById('pendingRecordTotal');if(pendingRecords)pendingRecords.textContent=records.filter(r=>r.status!=='已签署').length;
    const pendingTasks=document.getElementById('pendingTaskTotal');if(pendingTasks)pendingTasks.textContent=tasks.filter(t=>!['RESOLVED','CANCELLED','REJECTED'].includes(t.status)).length;
    const open=tasks.filter(t=>!['RESOLVED','CANCELLED','REJECTED'].includes(t.status));
    const reminders=document.getElementById('homeReminders');
    if(reminders)reminders.innerHTML=(patients.filter(p=>p.status==='待结束').map(p=>`<div class="alert" onclick="currentEncounterId=${p.encounterId};currentRoundingId=${p.latestRoundingId};openWorkflowPanel()"><div><b>${html(p.name)}</b><br/>本次查房已保存，等待结束。</div></div>`).join(''))+
      open.slice(0,3).map(t=>`<div class="alert" onclick="openTaskDetail(${t.id})"><div><b>${html(t.patient)}</b><br/>待办：${html(t.waiting_for)}</div></div>`).join('')+
      (records.some(r=>r.status!=='已签署')?'<div class="alert" onclick="openRecordList()">有病历待审核；审核签署与查房结束分别处理。</div>':'')||'<p class="section-note" style="padding:0 20px">当前没有待处理事项。</p>';
    const count=document.getElementById('homeReminderCount');if(count)count.textContent=(open.length+patients.filter(p=>p.status==='待结束').length+records.filter(r=>r.status!=='已签署').length)+' 项';
  }catch(error){console.warn('工作台统计读取失败',error.message);}
}

async function selectPatient(name) {
  try { await loadBackendPatients(); } catch (error) { showToast('无法读取患者，请检查后端：' + error.message); return; }
  const patient = patients.find(p => p.name === name);
  if (!patient?.backendPatientId || !patient.encounterId) { showToast('该患者没有可用的住院记录'); return; }
  stopRoundRecording();
  currentEncounterId = patient.encounterId;
  workflowPatientId=patient.backendPatientId;
  currentRoundingId = null;
  roundRequestKey = crypto.randomUUID(); roundStartedAt = new Date(); roundChunks = []; roundDraftText = '';
  document.getElementById('modalPatient').textContent = patient.name;
  const overview = document.querySelector('#roundModal .patient-overview');
  overview.querySelector('.patient-avatar').textContent = patient.name[0];
  overview.querySelector('b + div').textContent = `${patient.bed} · ${patient.id} · ${patient.diagnosis}`;
  document.querySelector('#roundModal .record-grid').style.display = 'none';
  const old = document.getElementById('roundText'); if (old) old.id = 'roundTranscript';
  const transcript = document.getElementById('roundTranscript');
  transcript.value = ''; transcript.placeholder = '录音转写后请核对，也可直接输入查房记录和计划';
  transcript.oninput = () => { document.getElementById('draft').style.display='none'; roundDraftText=''; const b=document.getElementById('generateBtn'); b.textContent='生成病程草稿'; b.onclick=generateDraft; };
  if (!document.getElementById('recordButton')) {
    const controls = document.createElement('div');
    controls.innerHTML = '<button class="secondary" id="recordButton" onclick="toggleRoundRecording()">● 开始录音</button> <span id="recordingStatus" class="section-note">请核对转写后再保存</span>';
    transcript.before(controls);
  }
  document.getElementById('draft').style.display='none';
  const button=document.getElementById('generateBtn'); button.disabled=false; button.textContent='生成病程草稿'; button.onclick=generateDraft;
  document.getElementById('roundModal').classList.add('show');
}
function openRound() {
  const patient = patients.find(p => p.backendPatientId === workflowPatientId) || patients.find(p => p.encounterId === currentEncounterId) || patients[0];
  if (patient) selectPatient(patient.name); else showToast('请先加载患者');
}
function stopRoundRecording() {
  if (roundRecorder?.state === 'recording') roundRecorder.stop();
  if (roundSpeech) { roundSpeech.onend=null; roundSpeech.onresult=null; try {roundSpeech.stop();} catch (_) {} }
  const button=document.getElementById('recordButton'); if(button) button.textContent='● 开始录音';
}
function closeRound() { stopRoundRecording(); document.getElementById('roundModal').classList.remove('show'); }
function generateDraft() {
  if (roundRecorder?.state === 'recording') { showToast('请先停止录音并核对转写文本'); return; }
  const text = document.getElementById('roundTranscript').value.trim();
  if (!text) { showToast('请先录音或输入查房记录'); return; }
  roundDraftText=text;
  document.getElementById('draft').textContent='本次查房记录（待医生审核）\n\n'+text;
  document.getElementById('draft').style.display='block';
  const button=document.getElementById('generateBtn'); button.textContent='保存病历并提取小任务'; button.onclick=saveRoundingSession;
}
async function saveRoundingSession() {
  if (roundSaving) return;
  const text=document.getElementById('roundTranscript').value.trim();
  if (text !== roundDraftText) { generateDraft(); showToast('记录已变化，请核对新草稿后保存'); return; }
  const patient=patients.find(p => p.encounterId===currentEncounterId);
  if (!patient?.backendPatientId || !patient.encounterId || !text) return;
  roundSaving=true; const button=document.getElementById('generateBtn'); button.disabled=true;
  let result;
  try {
    result=await postWorkflow('/rounding-sessions', {doctor_id:1, patient_id:patient.backendPatientId,
      encounter_id:patient.encounterId, request_key:roundRequestKey, started_at:roundStartedAt.toISOString(),
      ended_at:new Date().toISOString(), transcript:text, generated_note:document.getElementById('draft').textContent,
      transcript_source:roundChunks.length?'browser_audio_plus_manual':'manual', submit_event:true});
  } catch(error) { showToast('保存失败：'+error.message); }
  finally { roundSaving=false; button.disabled=false; }
  if (!result) return;
  currentRoundingId=result.id;
  closeRound();
  try{await loadBackendPatients();await refreshDashboardTotals();}catch(error){showToast('查房已保存，患者状态刷新失败，请稍后刷新');}
  openWorkflowPanel();
  const saved=document.createElement('div'); saved.className='draft'; saved.style.display='block';
  saved.textContent='查房和病历已保存。请核对下面提取的小任务，确认后加入任务安排。';
  const link=document.createElement('button'); link.className='secondary'; link.textContent='查看本次病历'; link.onclick=()=>openMedicalRecord(result.record_id); saved.append(' ',link);
  document.getElementById('panelBody').prepend(saved);
  showToast('查房已保存，请确认提取的小任务');
}

async function loadBackendRecords() {
  try {
    const data=await workflowFetch('/medical-records'); backendRecords=true;
    medicalRecords.splice(0,medicalRecords.length,...data);
    const table=document.getElementById('recordListTable'); if(table) table.innerHTML=recordTable(data);
    return data;
  } catch(error) {
    backendRecords=false; medicalRecords.splice(0);
    const table=document.getElementById('recordListTable'); if(table) table.textContent='病历读取失败：'+error.message+'。请重试。';
    throw error;
  }
}
async function openMedicalRecord(id, historyContext=null) {
  try {
    const r=await workflowFetch('/medical-records/'+encodeURIComponent(id));
    const i=medicalRecords.findIndex(item=>String(item.id)===String(id));
    if(i>=0) medicalRecords[i]=r; else medicalRecords.push(r);
    backendRecords=true;
    activeRecordHistory=historyContext;workflowPatientId=r.patient_id;
    if(r.encounter_id) currentEncounterId=r.encounter_id; currentRoundingId=r.source_rounding_session_id||null;
    document.getElementById('panelTitle').textContent=r.record_type+' · '+r.patient;
    document.getElementById('panelSub').textContent=r.record_number+' · '+r.status;
    document.getElementById('panelBody').innerHTML=`<div class="record-paper"><h3>${html(r.record_type)}</h3>
      <div class="record-meta"><div>姓名：${html(r.patient)}</div><div>床号：${html(r.bed)}</div><div>本次住院：${html(r.encounter_number||r.admission_number)}</div><div>记录时间：${html(r.time)}</div></div>
      <div class="record-text" id="recordTextView"></div><textarea id="recordEditor" style="display:none;min-height:230px"></textarea>
      <div class="record-source">${r.source_rounding_session_id?'来源查房 #'+r.source_rounding_session_id:'病历文书'} · 生成内容需医生审核。</div>
      <button class="secondary" onclick="openWorkflowPanel()">查看关联任务</button> <button class="secondary" id="backToRecordHistory">${historyContext?'返回历史病历':'该患者历史病历'}</button> <button class="secondary" onclick="openRecordList()">全部病历列表</button>
      ${r.status==='已签署'?'':'<button class="secondary" id="signRecordButton">审核并模拟签署</button>'}</div>`;
    document.getElementById('recordTextView').textContent=r.content;
    document.getElementById('recordEditor').value=r.content;
    document.getElementById('backToRecordHistory').onclick=()=>openPatientHistory(historyContext?.patientId||r.patient_id,historyContext?.encounterId||null);
    const sign=document.getElementById('signRecordButton'); if(sign)sign.onclick=()=>signMedicalRecord(r.id);
    const action=document.getElementById('panelAction'); action.disabled=false;action.textContent=r.status==='已签署'?'返回历史病历':'编辑病历'; action.onclick=()=>r.status==='已签署'?openPatientHistory(historyContext?.patientId||r.patient_id,historyContext?.encounterId||null):editMedicalRecord(r.id);
    document.getElementById('panelModal').classList.add('show');
  } catch(error) { showToast('病历读取失败：'+error.message); }
}
function workflowPanelHtml() {
  const patient=patients.find(p=>p.encounterId===currentEncounterId)||patients.find(p=>p.backendPatientId===workflowPatientId);
  return `<div class="workflow-toolbar"><div><b>查房 → 小任务 → 任务安排</b><p class="section-note">${html(patient?.name)} · ${html(patient?.bed)} · ${html(patient?.encounterId===currentEncounterId?patient.encounterNumber:('历史住院 #'+currentEncounterId))}</p>
    <select onchange="currentEncounterId=Number(this.value);workflowPatientId=null;currentRoundingId=null;openWorkflowPanel()">${!patients.some(p=>p.encounterId===currentEncounterId)&&patient?`<option value="${currentEncounterId}" selected>${html(patient.name)} · 历史住院</option>`:''}${patients.filter(p=>p.encounterId).map(p=>`<option value="${p.encounterId}" ${p.encounterId===currentEncounterId?'selected':''}>${html(p.name)} · ${html(p.bed)}</option>`).join('')}</select></div>
    <button class="secondary" onclick="openRound()">＋ 新一次查房</button>${patient?`<button class="secondary" onclick="openPatientHistory(${patient.backendPatientId})">历史病历与查房</button>`:''}<button class="secondary" onclick="openTaskSchedule()">任务安排</button></div>
    <div id="workflowStatus" class="section-note">正在读取...</div><div id="roundingLifecycle"></div><div class="workflow-grid"><div><h3>本次查房的小任务（需确认）</h3><div id="workflowIntents" class="detail-list"></div></div>
    <div><h3>已加入任务安排</h3><div id="workflowLoops" class="detail-list"></div></div></div><h3>本次住院时间线</h3><div id="workflowTimeline" class="timeline"></div>`;
}
function openWorkflowPanel() {
  tasksView=false;
  document.getElementById('panelTitle').textContent='临床工作流'; document.getElementById('panelSub').textContent='规则提取 · 医生确认 · 持久化跟进';
  document.getElementById('panelBody').innerHTML=workflowPanelHtml();
  const button=document.getElementById('panelAction'); button.disabled=false;button.textContent='刷新'; button.onclick=refreshWorkflow;
  document.getElementById('panelModal').classList.add('show'); refreshWorkflow();
}
function candidateHtml(item) {
  return `<div class="workflow-item" data-intent-id="${item.id}"><div><b>${html(taskType(item.intent_type))}：${html(item.target)}</b><p>${html(item.raw_text)}</p>
    ${candidateMatchingHtml(item)}<label>计划时间 <input type="datetime-local" id="due-${item.id}" /></label><small> 留空表示待安排</small><p>
    <button class="primary" onclick="confirmWorkflowIntent(${item.id})">确认并加入任务安排</button></p><input id="ignoreReason-${item.id}" placeholder="如需忽略，请填写原因" /> <button class="secondary" onclick="rejectWorkflowIntent(${item.id})">忽略</button><p id="candidateError-${item.id}" role="alert" style="color:#b42318"></p></div></div>`;
}
function taskHtml(item) {
  const overdue=item.due_at && new Date(item.due_at)<new Date() && !['RESOLVED','CANCELLED'].includes(item.status);
  const simple=['ACTION','MONITOR'].includes(item.intent_type);
  const status=item.status==='WAITING_EVENT'&&simple?'<span class="tag orange">待执行</span>':workflowStatusTag(item.status);
  return `<div class="workflow-item" data-task-id="${item.id}"><div><b>${html(item.patient)} · ${html(taskType(item.intent_type))}：${html(item.waiting_for)}</b> ${status}
    <p>计划：${html(localTime(item.due_at))} ${overdue?'<span class="tag orange">已逾期</span>':''}</p><small>任务 #${item.id} · ${html(item.state_reason)}</small>
    ${item.status==='RESOLVED'?`<p>完成时间：${html(localTime(item.completed_at))}</p>`:`<p>下一步：${simple?'填写实际执行情况并完成任务':item.status==='WAITING_EVENT'?'登记实际检验结果':item.status==='RESULT_AVAILABLE'?'确认已查看结果':'填写处理结论并完成'}</p>`}
    <p><button class="primary" onclick="openTaskDetail(${item.id})">查看／处理</button> ${item.record_id?`<button class="secondary" onclick="openMedicalRecord(${item.record_id})">来源病历</button>`:''}</p></div></div>`;
}
async function refreshWorkflow() {
  const encounterId=currentEncounterId;
  try {
    const [intents,tasks,timeline,roundings]=await Promise.all([workflowFetch(`/encounters/${encounterId}/intents`),workflowFetch(`/tasks?doctor_id=1&encounter_id=${encounterId}`),workflowFetch(`/encounters/${encounterId}/timeline`),workflowFetch(`/rounding-sessions?encounter_id=${encounterId}&doctor_id=1`)]);
    if (encounterId!==currentEncounterId) return;
    workflowIntents=intents; workflowTasks=tasks;
    const round=roundings.find(r=>r.id===currentRoundingId)||roundings[0];
    currentRoundingId=round?.id||null;
    const candidates=intents.filter(i=>i.status==='CANDIDATE'&&(!round||round.intent_ids.includes(i.id)));
    const roundTasks=tasks.filter(t=>!round||t.source_rounding_session_id===round.id);
    const lifecycle=document.getElementById('roundingLifecycle');
    if(lifecycle)lifecycle.innerHTML=round?`<div class="draft" style="display:block;margin-bottom:18px"><b>${round.status==='COMPLETED'?'✓ 本次查房已结束':'本次查房待结束'} · #${round.id}</b>
      <p>病历：${html(round.record_status||'未保存')} · 候选待处理 ${round.candidate_count} 个 · 后续待办 ${round.open_task_count} 个 · 已完成 ${round.completed_task_count} 个</p>
      <select aria-label="查房历史" onchange="currentRoundingId=Number(this.value);refreshWorkflow()">${roundings.map(r=>`<option value="${r.id}" ${r.id===round.id?'selected':''}>查房 #${r.id} · ${html(localTime(r.started_at))} · ${r.status==='COMPLETED'?'已结束':'待结束'}</option>`).join('')}</select>
      ${round.record_id?`<button class="secondary" onclick="openMedicalRecord(${round.record_id})">查看本次病历</button>`:''}
      ${round.status==='COMPLETED'?`<p>结束时间：${html(localTime(round.ended_at))}。后续任务继续在任务安排中跟进。</p><button class="primary" onclick="openTaskSchedule()">继续处理后续任务</button>`:`<p>${round.candidate_count?'先逐项确认或忽略候选任务，再结束查房。':'候选任务已处理，可以结束查房；后续随访任务继续保留。'}病历审核签署可单独进行。</p><button class="primary" id="finishRoundingButton" ${round.candidate_count||round.status!=='SUBMITTED'?'disabled':''} onclick="finishRounding(${round.id})">结束本次查房</button>`}
      <p id="roundingError" role="alert" style="color:#b42318"></p></div>`:'<p>尚无保存的查房记录，请先开始查房。</p>';
    const box=document.getElementById('workflowIntents'); if(box)box.innerHTML=candidates.map(candidateHtml).join('')||'<p>没有待确认小任务。已确认的任务在右侧继续跟进。</p>';
    const loops=document.getElementById('workflowLoops'); if(loops) loops.innerHTML=roundTasks.map(taskHtml).join('')||'<p>本次查房暂无已确认的后续任务。</p>';
    const tl=document.getElementById('workflowTimeline'); if(tl)tl.innerHTML=timeline.items.map(t=>`<div class="timeline-item"><div class="dot"></div><div>${html(localTime(t.occurred_at))}<p>${html(t.title)} · ${html(t.status)}</p></div></div>`).join('');
    const recent=document.getElementById('homeRecentTimeline');if(recent)recent.innerHTML=timeline.items.slice(0,4).map(t=>`<div class="timeline-item"><div class="dot"></div><div>${html(localTime(t.occurred_at))}<p>${html(t.title)} · ${html(t.status)}</p></div></div>`).join('')||'<p class="section-note">暂无记录。</p>';
    const status=document.getElementById('workflowStatus'); if(status)status.textContent=`${candidates.length} 个待确认小任务，${tasks.filter(t=>!['RESOLVED','CANCELLED'].includes(t.status)).length} 个未完成任务`;
    document.getElementById('homeWorkflowStatus').textContent='已连接';
    document.getElementById('homeIntentText').textContent=candidates.length+' 个待确认小任务';
    document.getElementById('homeLoopText').textContent=tasks.filter(t=>!['RESOLVED','CANCELLED'].includes(t.status)).length+' 个未完成任务';
    document.getElementById('homeWorkflowHint').textContent='查房保存后核对小任务，确认后进入任务安排';
  } catch(error) {
    const status=document.getElementById('workflowStatus'); if(status)status.textContent='读取失败：'+error.message;
    document.getElementById('homeWorkflowStatus').textContent='连接失败';
  }
}
async function finishRounding(id) {
  const button=document.getElementById('finishRoundingButton');if(button)button.disabled=true;
  try {
    const round=await postWorkflow(`/rounding-sessions/${id}/finish`,{doctor_id:1});
    await loadBackendPatients();await refreshWorkflow();await refreshDashboardTotals();
    showToast('本次查房已结束，后续还有 '+round.open_task_count+' 个待办任务');
  }catch(error){const message=document.getElementById('roundingError');if(message)message.textContent='结束失败：'+error.message;if(button)button.disabled=false;}
}
async function confirmWorkflowIntent(id) {
  try {
    const value=document.getElementById('due-'+id)?.value;
    const specimen=document.getElementById('candidateSpecimen-'+id)?.value.trim();const order=document.getElementById('candidateOrder-'+id)?.value.trim();const intent=workflowIntents.find(i=>i.id===id);
    await postWorkflow('/intents/'+id+'/confirm',{doctor_id:1,due_at:value?new Date(value).toISOString():null,matching:specimen||order?{test_name:intent.target,specimen_id:specimen||undefined,order_id:order||undefined}:undefined});
    showToast('已加入任务安排'); await refreshWorkflow();await refreshDashboardTotals();
  } catch(error) {showToast('确认失败：'+error.message);}
}
async function rejectWorkflowIntent(id) {
  const reason=document.getElementById('ignoreReason-'+id)?.value;
  if(!reason?.trim()){const error=document.getElementById('candidateError-'+id);if(error)error.textContent='请填写忽略原因';return;}
  try {await postWorkflow('/intents/'+id+'/reject',{doctor_id:1,reason}); await refreshWorkflow();} catch(error){showToast(error.message);}
}
async function openTaskSchedule(filter) {
  if(typeof filter==='string')scheduleFilter=filter;
  tasksView=true; document.getElementById('panelTitle').textContent='任务安排'; document.getElementById('panelSub').textContent='全部负责患者 · 按计划时间排列 · 完成任务保留历史';
  document.getElementById('panelBody').innerHTML='<p>正在读取任务...</p>';
  const button=document.getElementById('panelAction'); button.disabled=false;button.textContent='刷新任务'; button.onclick=openTaskSchedule;
  document.getElementById('panelModal').classList.add('show');
  try {
    workflowTasks=await workflowFetch('/tasks?doctor_id=1');
    const done=workflowTasks.filter(t=>t.status==='RESOLVED').length;
    const pending=workflowTasks.filter(t=>!['RESOLVED','CANCELLED','REJECTED'].includes(t.status)).length;
    const shown=workflowTasks.filter(t=>scheduleFilter==='all'||(scheduleFilter==='completed'?t.status==='RESOLVED':scheduleFilter==='cancelled'?t.status==='CANCELLED':!['RESOLVED','CANCELLED','REJECTED'].includes(t.status)));
    document.getElementById('panelBody').innerHTML=`<p>待办 ${pending} 个 · 已完成 ${done} 个</p><label>任务范围 <select id="taskFilter" onchange="openTaskSchedule(this.value)"><option value="pending">待办任务</option><option value="completed">已完成</option><option value="cancelled">已取消</option><option value="all">全部任务</option></select></label> <button class="secondary" onclick="openWorkflowPanel()">返回查房／结束查房</button><div class="detail-list" style="margin-top:16px">${shown.map(taskHtml).join('')||'<p>当前范围没有任务。</p>'}</div>`;
    document.getElementById('taskFilter').value=scheduleFilter;
    const count=document.getElementById('pendingTaskTotal');if(count)count.textContent=pending;
  } catch(error) {document.getElementById('panelBody').textContent='读取失败：'+error.message;}
}
async function openTaskDetail(id) {
  try {
    const data=await workflowFetch(`/loops/${id}/history?doctor_id=1`); const t=data.task;
    currentEncounterId=t.encounter_id; currentRoundingId=t.source_rounding_session_id||null;
    const done=['RESOLVED','CANCELLED'].includes(t.status);
    const simple=['ACTION','MONITOR'].includes(t.intent_type);
    document.getElementById('panelTitle').textContent='任务 #'+id+' · '+t.waiting_for;
    document.getElementById('panelSub').textContent=t.patient+' · '+taskType(t.intent_type);
    document.getElementById('panelModal').classList.add('show');
    document.getElementById('panelBody').innerHTML=`${t.status==='WAITING_EVENT'&&simple?'<span class="tag orange">待执行</span>':workflowStatusTag(t.status)}<p>查房原文：${html(t.raw_text)}</p>
      <p>计划时间：${html(localTime(t.due_at))}</p><p>结果／执行记录：${html(data.result?.note||'尚未登记')}</p>
      ${done?`<div class="draft" style="display:block"><b>${t.status==='RESOLVED'?'✓ 任务已完成':'任务已取消'}</b><p>${html(t.state_reason)}</p>${t.completed_at?`<p>完成时间：${html(localTime(t.completed_at))}</p>`:''}<button class="secondary" onclick="openTaskSchedule('completed')">查看已完成任务</button> <button class="secondary" onclick="currentRoundingId=${t.source_rounding_session_id||'null'};openWorkflowPanel()">返回来源查房</button></div>`:''}
      ${done?'':`<label>调整计划时间 <input type="datetime-local" id="taskDue" /></label> <button class="secondary" onclick="performTask(${id},${t.version},'schedule')">保存时间</button>
      <p><label for="taskNote">${simple?'实际执行情况（必填）':t.status==='ACKNOWLEDGED'?'处理结论（必填）':'检验结果／查看备注'}</label></p><textarea id="taskNote" placeholder="${simple?'例如：已联系会诊，填写联系对象与处理情况':'请填写实际结果或处理结论'}"></textarea>
      <p>${simple?`<button class="primary" onclick="performTask(${id},${t.version},'complete')">记录执行情况并完成任务</button>`:t.status==='WAITING_EVENT'?`<button class="primary" onclick="performTask(${id},${t.version},'record-result')">登记结果／执行记录</button>`:''}
      ${!simple&&t.status==='RESULT_AVAILABLE'?`<button class="primary" onclick="performTask(${id},${t.version},'acknowledge')">确认已查看结果</button>`:''}
      ${!simple&&t.status==='ACKNOWLEDGED'?`<button class="primary" onclick="performTask(${id},${t.version},'resolve')">保存处理结论并完成</button>`:''}
      <button class="secondary" onclick="performTask(${id},${t.version},'cancel')">填写原因并取消</button></p><p class="section-note">${simple?'填写实际执行记录后由医生明确完成任务。':'下一步：'+(t.status==='WAITING_EVENT'?'登记结果 → 确认查看 → 填写结论完成':t.status==='RESULT_AVAILABLE'?'确认查看 → 填写结论完成':'填写处理结论并完成')+'。检验结果暂由医生手工登记。'}</p><p id="taskError" role="alert" style="color:#b42318"></p>`}
      ${reportWorkflowHtml(t,data)}<h3>操作记录</h3>${data.history.map(h=>`<p>${html(localTime(h.at))} · ${html(h.action)} · ${html(h.detail.note||'')}</p>`).join('')}`;
    const date=document.getElementById('taskDue'); if(date && t.due_at){const d=new Date(t.due_at);date.value=new Date(d-d.getTimezoneOffset()*60000).toISOString().slice(0,16);}
    const button=document.getElementById('panelAction');button.disabled=false;button.textContent='返回任务安排';button.onclick=openTaskSchedule;
  }catch(error){showToast(error.message);}
}
async function performTask(id, version, action) {
  if(taskSaving)return;
  const note=document.getElementById('taskNote')?.value||'';
  const message=document.getElementById('taskError');
  if(['complete','resolve','record-result','cancel'].includes(action)&&!note.trim()){if(message)message.textContent=action==='cancel'?'请填写取消原因':'请先填写实际结果、执行情况或处理结论';return;}
  taskSaving=true;document.querySelectorAll('#panelBody button').forEach(b=>b.disabled=true);
  try {
    const due=document.getElementById('taskDue')?.value;
    await postWorkflow(`/loops/${id}/${action}`,{doctor_id:1,expected_version:version,note,evidence_id:Number(document.getElementById('taskEvidenceId')?.dataset.evidenceId)||undefined,due_at:due?new Date(due).toISOString():null});
    showToast(['complete','resolve'].includes(action)?'任务已完成，已移入完成记录':'任务已保存'); await openTaskDetail(id); await refreshWorkflow(); await refreshDashboardTotals();
  }catch(error){if(message)message.textContent='操作失败：'+error.message;else showToast(error.message);document.querySelectorAll('#panelBody button').forEach(b=>b.disabled=false);}
  finally{taskSaving=false;}
}
async function openHandoff() {
  document.getElementById('panelTitle').textContent='交班摘要';document.getElementById('panelSub').textContent='当前住院的未完成任务和待确认计划';
  document.getElementById('panelBody').textContent='正在生成...';
  document.getElementById('panelModal').classList.add('show');
  const button=document.getElementById('panelAction');button.textContent='返回工作流';button.onclick=openWorkflowPanel;
  try {const data=await postWorkflow(`/encounters/${currentEncounterId}/handoffs`,{doctor_id:1});const pre=document.createElement('div');pre.className='record-text';pre.textContent=data.content;document.getElementById('panelBody').replaceChildren(pre);for(const ref of data.references||[]){if(ref.loop_id){const link=document.createElement('button');link.className='secondary';link.textContent='打开待办任务 #'+ref.loop_id+(ref.evidence_id?'（含报告证据）':'');link.onclick=()=>openTaskDetail(ref.loop_id);document.getElementById('panelBody').append(link);}}showToast('交班草稿 #'+data.id+' 已保存');}catch(error){document.getElementById('panelBody').textContent='生成失败：'+error.message;}
}

(async function initializeWorkflow() {
  medicalRecords.splice(0); // Records are always loaded from the API.
  try {await loadBackendPatients(); await refreshWorkflow(); await refreshDashboardTotals();}
  catch(error){document.getElementById('homeWorkflowStatus').textContent='连接失败';showToast('请启动后端后重试');}
})();
