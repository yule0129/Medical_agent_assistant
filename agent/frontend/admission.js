// Interview -> scoped Agent sources and draft -> clinician review/signature.
let admissionSaving=false,admissionSpeech=null,admissionRecording=false,admissionCurrent=null;
function stopAdmissionRecording(){
  admissionRecording=false;
  if(admissionSpeech){try{admissionSpeech.stop();}catch(_){}admissionSpeech=null;}
  const b=document.getElementById('admissionRecordButton');if(b)b.textContent='● 开始问诊转写';
}
function markAdmissionInterviewEdited(){
  if(admissionRecording)stopAdmissionRecording();
  const note=document.getElementById('admissionInterviewEditStatus');
  const changed=document.getElementById('admissionTranscript').value.trim()!==admissionCurrent?.data.agent.transcript;
  if(note)note.textContent=changed?'问诊文字已修改，尚未保存。保存后，Agent将使用修改后的问诊记录。':'问诊文字与已保存记录一致。';
}
function toggleAdmissionRecording(){
  if(admissionRecording){stopAdmissionRecording();return;}
  const Speech=window.SpeechRecognition||window.webkitSpeechRecognition;
  const status=document.getElementById('admissionSpeechStatus'),editor=document.getElementById('admissionTranscript');
  if(!Speech){status.textContent='当前浏览器不支持语音转写，请粘贴已有转录或输入医生患者对话。';return;}
  const speech=new Speech();admissionSpeech=speech;speech.lang='zh-CN';speech.continuous=true;speech.interimResults=true;
  speech.onresult=event=>{
    if(editor!==document.getElementById('admissionTranscript')){stopAdmissionRecording();return;}
    let interim='';
    for(let i=event.resultIndex;i<event.results.length;i++){
      if(event.results[i].isFinal)editor.value+=(editor.value?'\n':'')+event.results[i][0].transcript;
      else interim+=event.results[i][0].transcript;
    }
    status.textContent=interim?'正在转写：'+interim:'对话已转写，请核对说话人、症状和数值后生成。';
  };
  speech.onerror=()=>{status.textContent='语音转写中断，请检查麦克风权限或直接输入对话。';stopAdmissionRecording();};
  speech.onend=()=>{if(admissionRecording)stopAdmissionRecording();};
  try{speech.start();admissionRecording=true;document.getElementById('admissionRecordButton').textContent='■ 停止转写';status.textContent='正在转写问诊对话，结束后请核对文本；说话人需人工标注。';}
  catch(_){status.textContent='无法启动语音转写，请直接输入或粘贴转录。';stopAdmissionRecording();}
}
function admissionStatusText(data){
  if(data.reviewed)return '医生已审查并签署 · 待办已完成';
  const prefix=data.overdue?'⚠ 已超过入院24小时 · ':'';
  return prefix+({GENERATING:'Agent正在读取资料并生成草稿',STALE:'问诊或检查已更新，请重新生成并审查',FAILED:'Agent生成失败，可保留资料并重试'}[data.agent.status]||(data.written?'草稿待医生审查签署':'待采集问诊资料，由Agent生成入院记录草稿'));
}
async function openAdmissionRecord(patientId,encounterId=null){
  stopAdmissionRecording();admissionCurrent=null;
  const request=++patientDetailRequest;++historyRequest;
  const body=document.getElementById('panelBody'),action=document.getElementById('panelAction');
  document.getElementById('panelTitle').textContent='入院记录';document.getElementById('panelSub').textContent='正在读取本次住院资料…';
  body.innerHTML='<div id="admissionView">正在读取入院记录…</div>';const container=document.getElementById('admissionView');
  action.disabled=true;action.textContent='审查并签署';document.getElementById('panelModal').classList.add('show');
  try{
    const profile=await workflowFetch(`/patients/${patientId}/profile?doctor_id=1`+(encounterId?`&encounter_id=${encounterId}`:''));
    if(request!==patientDetailRequest||container!==document.getElementById('admissionView'))return;
    if(!profile.encounter_id)throw new Error('该患者暂无可用的住院记录');
    const data=await workflowFetch(`/patients/${patientId}/admission-record?encounter_id=${profile.encounter_id}&doctor_id=1`);
    if(request!==patientDetailRequest||container!==document.getElementById('admissionView'))return;
    admissionCurrent={patientId,encounterId:profile.encounter_id,data,container};
    document.getElementById('panelTitle').textContent='入院记录 · '+profile.name;
    document.getElementById('panelSub').textContent=profile.bed+' · '+profile.encounter_number;
    container.insertAdjacentHTML('beforebegin',patientTabs(patientId,'admission',profile.encounter_id));
    const signed=data.reviewed,locked=signed?'disabled':'',readonly=signed?'readonly':'';
    const reports=data.agent.sources.read_examinations.filter(s=>s.kind==='已接入检查报告');
    container.innerHTML=`<label>住院记录 <select id="admissionEncounter" onchange="openAdmissionRecord(${patientId},Number(this.value))">${profile.encounters.map(e=>`<option value="${e.id}" ${e.id===profile.encounter_id?'selected':''}>${html(e.encounter_number)} · ${html(localTime(e.admitted_at))}</option>`).join('')}</select></label>
      <p>入院：${html(localTime(data.admitted_at))} · 截止：${html(localTime(data.due_at))}</p>
      <p id="admissionStatus" role="status">${html(admissionStatusText(data))}</p>
      <section class="admission-section"><h3>1 · 医生患者问诊记录</h3>
      <button class="secondary admission-control" id="admissionRecordButton" ${locked} onclick="toggleAdmissionRecording()">● 开始问诊转写</button>
      <p class="section-note" id="admissionSpeechStatus">支持语音实时转写，也可粘贴已有转录；请核对识别结果并标注医生、患者说话人。</p>
      <label for="admissionTranscript">问诊文字（可直接修改、补充或粘贴）</label>
      <textarea id="admissionTranscript" ${readonly} oninput="markAdmissionInterviewEdited()" placeholder="医生：这次哪里不舒服，多久了？&#10;患者：……&#10;请记录实际对话；未询问的信息不要补成否认或正常。"></textarea>
      <p id="admissionInterviewEditStatus" class="section-note" role="status">${signed?'已签署，问诊记录只读':'可直接点击上方文本框修改语音转写结果，修改后请保存。'}</p>
      <button class="secondary admission-control" id="saveAdmissionInterview" ${locked} onclick="admissionAction('inputs')">保存问诊文字修改</button></section>
      <section class="admission-section"><h3>2 · 本次住院检查结果</h3>
      <div id="admissionReports">${reports.map(s=>`<article class="draft" style="display:block"><b>${html(s.test_name)} · 版本 ${s.version}</b><p>${html(localTime(s.reported_at))} · 来源 ${html(s.id)}</p><div class="record-text">${html(s.text)}</div></article>`).join('')||'<p>本次住院尚无已接入的结构化检查报告。可在下方补充已核对的检查结果。</p>'}</div>
      <label for="admissionExams">医生补充的检查／影像结果（注明项目、日期及原始结果）</label>
      <textarea id="admissionExams" ${readonly} placeholder="仅填写实际已有的检查结果；留空表示未提供。"></textarea></section>
      <p><button class="secondary admission-control" id="saveAdmissionInputs" ${locked} onclick="admissionAction('inputs')">保存问诊与检查资料</button>
      <button class="primary admission-control" id="generateAdmissionDraft" ${locked} onclick="admissionAction('generate')">保存资料并由Agent生成草稿</button></p>
      <p class="section-note">${data.agent.model_configured?'Agent读取本次住院的患者资料、问诊对话和检查结果，再起草各章节并标注来源与缺项。':'当前模型尚未配置，资料可以先保存；配置DeepSeek后可生成草稿。'}</p>
      <section class="admission-section"><h3>3 · 医生审查入院记录</h3>
      ${data.agent.draft.missing_sections?.length?`<p class="tag orange" id="admissionMissing">需核实／补充：${html(data.agent.draft.missing_sections.join('、'))}</p>`:''}
      <textarea id="admissionContent" ${readonly} placeholder="Agent生成草稿后在此显示；请结合原始问诊和检查资料核对、补充及修改。"></textarea>
      <p><button class="secondary admission-control" id="saveAdmissionDraft" ${locked} onclick="admissionAction('draft')">保存审查修改</button></p>
      ${!signed?'<label><input type="checkbox" id="admissionReviewConfirmed" /> 我已核对问诊、检查结果及入院记录内容，并完成必要补充，同意签署。</label>':''}
      <p class="section-note">生成草稿和保存修改均不会完成审查待办，医生审查并签署后才完成。未提供的资料会标注待补充，不自动补成正常或否认。</p></section>
      <details id="admissionTrace"><summary>Agent工作过程与草稿来源</summary>${data.agent.trace.map(t=>`<p>${t.step} · ${html(t.action)} ${html(t.tool_name||'')} ${html((t.source_ids||[]).join('、'))}</p>`).join('')||'<p>尚无Agent生成记录。</p>'}
      ${(data.agent.draft.sections||[]).map(s=>`<p><b>${html(s.heading)}</b> · 来源：${html(s.source_ids.join('、')||'未提供')} ${s.missing?'· 待补充':''}</p>`).join('')}
      ${data.agent.error_code?`<p>生成状态：${html(data.agent.error_code)}</p>`:''}</details>
      <p id="admissionError" role="alert" style="color:#b42318"></p><button class="secondary" onclick="openTaskSchedule()">返回任务安排</button>`;
    document.getElementById('admissionTranscript').value=data.agent.transcript;
    document.getElementById('admissionExams').value=data.agent.examination_text;
    document.getElementById('admissionContent').value=data.record?.content||'';
    action.disabled=signed||!data.written;action.textContent=signed?'已审查并签署':'审查并签署';action.onclick=()=>admissionAction('review');
  }catch(error){if(request===patientDetailRequest&&container===document.getElementById('admissionView'))container.textContent='入院记录读取失败：'+error.message;}
}
async function admissionAction(kind){
  if(admissionSaving||!admissionCurrent)return;
  stopAdmissionRecording();const view=admissionCurrent,{patientId,encounterId}=view;
  const errorBox=document.getElementById('admissionError'),content=document.getElementById('admissionContent').value;
  const transcript=document.getElementById('admissionTranscript').value,examination_text=document.getElementById('admissionExams').value;
  if(['draft','review'].includes(kind)&&!content.trim()){errorBox.textContent='草稿为空，请先生成或补充正文';return;}
  if(kind==='generate'&&content!==(view.data.record?.content||'')){errorBox.textContent='正文有未保存的审查修改，请先保存修改再重新生成';return;}
  if(kind==='generate'&&!transcript.trim()){errorBox.textContent='请先填写并核对医生患者问诊记录';return;}
  if(['draft','review'].includes(kind)&&(transcript.trim()!==view.data.agent.transcript||examination_text.trim()!==view.data.agent.examination_text)){
    errorBox.textContent='问诊或检查资料尚未保存，请先保存资料并重新生成后审查';return;
  }
  if(kind==='review'&&!document.getElementById('admissionReviewConfirmed')?.checked){errorBox.textContent='请先确认已核对问诊、检查结果及入院记录内容';return;}
  admissionSaving=true;errorBox.textContent='';document.querySelectorAll('.admission-control,#panelAction').forEach(b=>b.disabled=true);
  try{
    let data=view.data;
    if(['inputs','generate'].includes(kind)){
      data=await workflowFetch(`/patients/${patientId}/admission-inputs`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({doctor_id:1,encounter_id:encounterId,transcript,examination_text,expected_input_version:data.agent.input_version})});
      view.data=data;
    }
    if(kind==='generate'){
      if(view===admissionCurrent)document.getElementById('admissionStatus').textContent='Agent正在读取问诊和检查资料并生成草稿，请稍候…';
      data=await postWorkflow(`/patients/${patientId}/admission-generate`,{doctor_id:1,encounter_id:encounterId,expected_revision:data.revision,expected_source_digest:data.agent.source_digest});
    }
    if(['draft','review'].includes(kind)){
      const payload={doctor_id:1,encounter_id:encounterId,content,expected_revision:data.revision};
      if(kind==='review'){payload.confirmed=true;payload.expected_source_digest=data.agent.source_digest;}
      data=await workflowFetch(`/patients/${patientId}/admission-${kind==='review'?'review':'record'}`,{method:kind==='review'?'POST':'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    }
    showToast({inputs:'问诊与检查资料已保存',generate:'Agent草稿已生成，请审查来源、缺项及正文',draft:'修改已保存，审查待办仍保留',review:'已审查并签署，入院记录待办已完成'}[kind]);
    if(view===admissionCurrent)await openAdmissionRecord(patientId,encounterId);
    await refreshDashboardTotals();await loadBackendRecords();
  }catch(error){
    if(view===admissionCurrent){errorBox.textContent='操作失败：'+error.message+'。当前输入已保留。';document.querySelectorAll('.admission-control,#panelAction').forEach(b=>b.disabled=false);}
    else showToast(error.message);
  }finally{admissionSaving=false;}
}
new MutationObserver(()=>{if(admissionRecording&&(!document.getElementById('admissionTranscript')||!document.getElementById('panelModal').classList.contains('show')))stopAdmissionRecording();}).observe(document.getElementById('panelModal'),{childList:true,subtree:true,attributes:true,attributeFilter:['class']});
