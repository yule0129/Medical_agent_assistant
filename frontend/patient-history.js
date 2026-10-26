// Patient -> admissions -> independent rounding sessions and medical records.
let historyPatientId = null;
let historyEncounterId = null;
let historyRequest = 0;
let activeRecordHistory = null;
let workflowPatientId = null;
let patientDetailRequest = 0;
let patientHistoryView = 'records';

function renderPatients(list=patients) {
  document.getElementById('patientRows').innerHTML=list.map(p=>`<tr onclick="openPatientDetailById(${p.backendPatientId})">
    <td><div class="patient-cell"><div class="patient-avatar">${html(p.name[0])}</div><div><button class="link patient-name" data-patient-open="${p.backendPatientId}" onclick="event.stopPropagation();openPatientDetailById(${p.backendPatientId})">${html(p.name)}</button><div class="patient-id">${html(p.id)}</div></div></div></td>
    <td>${html(p.bed)}</td><td>${html(p.diagnosis)}</td><td><span class="tag ${p.tone}">${html(p.status)}</span></td>
    <td><button class="link" onclick="event.stopPropagation();openPatientHistory(${p.backendPatientId})">历史病历</button></td></tr>`).join('');
}
function openPatientDetail(name) {
  const p=patients.find(p=>p.name===name);if(p)openPatientDetailById(p.backendPatientId);
}
function patientTabs(patientId,active,encounterId=null,admissionEncounterId=encounterId) {
  const basicEncounter=encounterId||'null';
  return `<nav class="patient-tabs" role="tablist" aria-label="患者资料">
    <button class="patient-tab ${active==='basic'?'active':''}" role="tab" aria-selected="${active==='basic'}" id="patientBasicTab" onclick="openPatientDetailById(${patientId},${basicEncounter})">基本信息</button>
    <button class="patient-tab ${active==='admission'?'active':''}" role="tab" aria-selected="${active==='admission'}" id="patientAdmissionTab" onclick="openAdmissionRecord(${patientId},${admissionEncounterId||'null'})">入院记录</button>
    <button class="patient-tab ${active==='records'?'active':''}" role="tab" aria-selected="${active==='records'}" id="patientHistoryButton" onclick="openPatientHistory(${patientId},${basicEncounter},'records')">历史病历</button>
    <button class="patient-tab ${active==='roundings'?'active':''}" role="tab" aria-selected="${active==='roundings'}" id="patientRoundingsTab" onclick="openPatientHistory(${patientId},${basicEncounter},'roundings')">查房记录</button>
  </nav>`;
}
function patientValue(value) {return value===null||value===undefined||String(value).trim()===''?'未记录':String(value);}
async function openPatientDetailById(id,encounterId=null) {
  const p=patients.find(p=>p.backendPatientId===id);if(!p)return;
  const request=++patientDetailRequest;++historyRequest;
  const selectedEncounter=encounterId||p.encounterId||null;
  currentEncounterId=selectedEncounter;currentRoundingId=null;workflowPatientId=id;
  document.getElementById('panelTitle').textContent='患者详情 · '+p.name;
  document.getElementById('panelSub').textContent=p.bed+' · '+p.id;
  document.getElementById('panelBody').innerHTML=patientTabs(id,'basic')+'<div id="patientDetailView" role="tabpanel" aria-labelledby="patientBasicTab">正在读取患者基本信息…</div>';
  const container=document.getElementById('patientDetailView');
  const action=document.getElementById('panelAction');action.textContent='＋ 新一次查房';action.onclick=()=>startNewRounding(id);action.disabled=!p.encounterId;
  document.getElementById('panelModal').classList.add('show');
  try {
    const data=await workflowFetch(`/patients/${id}/profile?doctor_id=1`+(selectedEncounter?`&encounter_id=${selectedEncounter}`:''));
    if(request!==patientDetailRequest||container!==document.getElementById('patientDetailView'))return;
    currentEncounterId=data.encounter_id;
    document.getElementById('panelTitle').textContent='患者详情 · '+data.name;
    document.getElementById('panelSub').textContent=`${patientValue(data.bed)} · ${patientValue(data.admission_number)}`;
    // Basic opens the current admission. History and rounding tabs start with all accessible admissions.
    const nav=container.previousElementSibling;if(nav)nav.outerHTML=patientTabs(id,'basic',null,data.encounter_id);
    const fields=[['姓名',data.name],['性别',data.gender],['年龄',data.age===null||data.age===undefined?null:data.age+' 岁'],
      ['民族',data.ethnicity],['婚姻状况',data.marital_status],['出生地',data.birthplace],['职业',data.occupation],
      ['入院时间',data.admitted_at?localTime(data.admitted_at):null],['记录时间',data.recorded_at?localTime(data.recorded_at):null],['病史陈述者',data.history_reporter]];
    container.innerHTML=`<div class="patient-overview"><div class="patient-avatar">${html(data.name[0]||'患')}</div><div><b>${html(data.name)}</b><p>${html(patientValue(data.diagnosis))}</p></div></div>
      <h3>患者基本信息</h3><dl class="patient-basic-grid" id="patientBasicInfo">${fields.map(([label,value])=>`<div class="patient-basic-field"><dt>${html(label)}</dt><dd>${html(patientValue(value))}</dd></div>`).join('')}</dl>
      <p>住院记录：${html(patientValue(data.encounter_number))}</p>
      ${data.encounters.length>1?`<label>查看住院资料 <select id="profileEncounter" onchange="openPatientDetailById(${id},Number(this.value))">${data.encounters.map(e=>`<option value="${e.id}" ${e.id===data.encounter_id?'selected':''}>${html(e.encounter_number)} · ${html(localTime(e.admitted_at))}</option>`).join('')}</select></label>`:''}
      <p class="section-note" style="margin-top:16px">姓名等资料来自患者档案；入院时间、记录时间和病史陈述者按所选住院展示。缺失项目显示“未记录”。</p>
      ${p.latestRoundingId?`<button class="secondary" onclick="continueRounding(${p.latestRoundingId},${p.encounterId})">查看／继续上次查房</button>`:''}`;
  }catch(error){if(request===patientDetailRequest&&container===document.getElementById('patientDetailView')){container.textContent='基本信息读取失败：'+error.message;const retry=document.createElement('button');retry.className='secondary';retry.textContent='重试';retry.onclick=()=>openPatientDetailById(id,selectedEncounter);container.append(retry);}}
}

function startNewRounding(patientId) {
  const p=patients.find(p=>p.backendPatientId===patientId);
  if(!p){showToast('当前没有该患者的可用住院记录');return;}
  closePanel();selectPatient(p.name);
}
function continueRounding(roundId, encounterId, patientId=null) {
  currentEncounterId=encounterId;currentRoundingId=roundId;workflowPatientId=patientId||patients.find(p=>p.encounterId===encounterId)?.backendPatientId||historyPatientId;openWorkflowPanel();
}
async function openPatientHistory(patientId, encounterId=null, view='records') {
  patientHistoryView=view==='roundings'?'roundings':'records';
  const selectedView=patientHistoryView;
  historyPatientId=patientId;historyEncounterId=encounterId;
  const request=++historyRequest;
  document.getElementById('panelTitle').textContent=selectedView==='roundings'?'查房记录':'历史病历';
  document.getElementById('panelSub').textContent='正在读取患者历次记录...';
  document.getElementById('panelBody').innerHTML=patientTabs(patientId,selectedView,encounterId)+'<div id="patientHistoryList" role="tabpanel">正在读取...</div>';
  const container=document.getElementById('patientHistoryList');
  const action=document.getElementById('panelAction');action.textContent='＋ 新一次查房';action.disabled=false;action.onclick=()=>startNewRounding(patientId);
  document.getElementById('panelModal').classList.add('show');
  try {
    const data=await workflowFetch(`/patients/${patientId}/history?doctor_id=1`+(encounterId?`&encounter_id=${encounterId}`:''));
    if(request!==historyRequest||container!==document.getElementById('patientHistoryList'))return;
    document.getElementById('panelTitle').textContent=(selectedView==='roundings'?'查房记录':'历史病历')+' · '+data.patient.name;
    document.getElementById('panelSub').textContent=`${data.records.length} 份病历 · ${data.roundings.length} 次查房 · 按时间倒序`;
    const p=patients.find(p=>p.backendPatientId===patientId);
    action.disabled=!p?.encounterId;
    container.previousElementSibling.outerHTML=patientTabs(patientId,selectedView,encounterId);
    container.innerHTML=`<div class="workflow-toolbar"><label>患者 <select id="historyPatient" onchange="openPatientHistory(Number(this.value),null,'${selectedView}')">${patients.map(p=>`<option value="${p.backendPatientId}" ${p.backendPatientId===patientId?'selected':''}>${html(p.name)} · ${html(p.bed)}</option>`).join('')}</select></label>
      <label>住院范围 <select id="historyEncounter" onchange="openPatientHistory(${patientId},this.value?Number(this.value):null,'${selectedView}')"><option value="">全部可访问住院</option>${data.encounters.map(e=>`<option value="${e.id}" ${e.id===encounterId?'selected':''}>${html(e.encounter_number)} · ${html(localTime(e.admitted_at))}</option>`).join('')}</select></label></div>
      <div class="workflow-grid" style="grid-template-columns:1fr"><section id="historyRecordsSection" ${selectedView==='records'?'':'hidden'}><h3>历史病历（${data.records.length}）</h3><div class="detail-list" id="historyRecords">${data.records.map(r=>`<div class="workflow-item" data-history-record="${r.id}"><div><b>${html(r.record_type)}</b> <span class="tag ${r.status==='已签署'?'green':'orange'}">${html(r.status)}</span>
        <p>${html(localTime(r.created_at))} · ${html(r.author)}</p><small>${html(r.encounter_number||'未关联住院')} · ${html(r.record_number)}${r.source_rounding_session_id?' · 查房 #'+r.source_rounding_session_id:''}</small>
        <p>${html(r.preview)}</p><button class="primary" onclick="openMedicalRecord(${r.id},{patientId:${patientId},encounterId:${encounterId||'null'}})">查看病历正文</button></div></div>`).join('')||'<p>该范围内暂无病历。</p>'}</div></section>
      <section id="historyRoundingsSection" ${selectedView==='roundings'?'':'hidden'}><h3>历次查房（${data.roundings.length}）</h3><div class="detail-list" id="historyRoundings">${data.roundings.map(r=>`<div class="workflow-item" data-history-rounding="${r.id}"><div><b>查房 #${r.id} · ${html(localTime(r.started_at))}</b> <span class="tag ${r.status==='COMPLETED'?'green':'orange'}">${r.status==='COMPLETED'?'已结束':r.status==='DRAFT'?'草稿':'待结束'}</span>
        <p>${html(r.encounter_number)}</p><p>候选待处理 ${r.candidate_count} · 后续待办 ${r.open_task_count} · 已完成 ${r.completed_task_count}</p>
        <button class="secondary" onclick="openRoundingHistory(${r.id},${patientId},${encounterId||'null'})">查看查房原文</button> <button class="secondary" onclick="continueRounding(${r.id},${r.encounter_id},${patientId})">查看关联任务／继续处理</button></div></div>`).join('')||'<p>该范围内暂无查房记录。</p>'}</div></section></div>`;
  }catch(error){if(request===historyRequest&&container===document.getElementById('patientHistoryList')){container.textContent='历史读取失败：'+error.message;const retry=document.createElement('button');retry.className='secondary';retry.textContent='重试';retry.onclick=()=>openPatientHistory(patientId,encounterId,selectedView);container.append(retry);}}
}
async function openRoundingHistory(roundId, patientId, encounterId=null) {
  document.getElementById('panelTitle').textContent='历史查房 #'+roundId;
  document.getElementById('panelBody').innerHTML='<div id="historicalRound">正在读取...</div>';
  const container=document.getElementById('historicalRound');
  const action=document.getElementById('panelAction');action.disabled=false;action.textContent='返回查房记录';action.onclick=()=>openPatientHistory(patientId,encounterId,'roundings');
  try {
    const r=await workflowFetch(`/rounding-sessions/${roundId}?doctor_id=1`);
    if(container!==document.getElementById('historicalRound'))return;
    document.getElementById('panelSub').textContent=localTime(r.started_at)+' · '+(r.status==='COMPLETED'?'已结束':'待结束');
    container.innerHTML=`<h3>原始查房记录</h3><div class="record-text" id="historicalTranscript"></div><h3>保存时的病历草稿</h3><div class="record-text" id="historicalDraft"></div><p class="section-note">下方草稿为查房保存时的内容，医生后续编辑及签署后的文书请打开对应病历查看。</p><p>
      ${r.record_id?`<button class="primary" onclick="openMedicalRecord(${r.record_id},{patientId:${patientId},encounterId:${encounterId||'null'}})">查看对应病历</button>`:''} <button class="secondary" onclick="continueRounding(${r.id},${r.encounter_id},${patientId})">查看关联任务／继续处理</button></p>`;
    document.getElementById('historicalTranscript').textContent=r.transcript;
    document.getElementById('historicalDraft').textContent=r.generated_note||'';
  }catch(error){if(container===document.getElementById('historicalRound'))container.textContent='查房读取失败：'+error.message;}
}
