// Bedside transcripts are clinician records; this flow never calls a model.
let bedsideView=null,bedsideSaving=false,bedsideSpeech=null,bedsideRecording=false;
function stopBedsideSpeech(){
  bedsideRecording=false;
  if(bedsideSpeech){bedsideSpeech.onresult=null;bedsideSpeech.onend=null;try{bedsideSpeech.stop();}catch(_){}bedsideSpeech=null;}
  const b=document.getElementById('bedsideSpeechButton');if(b)b.textContent='● 开始查房转写';
}
function toggleBedsideSpeech(){
  if(bedsideRecording){stopBedsideSpeech();return;}
  const Recognition=window.SpeechRecognition||window.webkitSpeechRecognition;
  const editor=document.getElementById('bedsideTranscript'),status=document.getElementById('bedsideSpeechStatus');
  if(!Recognition){status.textContent='当前浏览器不支持语音转写，可直接输入或粘贴查房问诊。';return;}
  const speech=new Recognition();bedsideSpeech=speech;speech.lang='zh-CN';speech.continuous=true;speech.interimResults=true;
  speech.onresult=event=>{
    if(editor!==document.getElementById('bedsideTranscript')){stopBedsideSpeech();return;}
    let interim='';
    for(let i=event.resultIndex;i<event.results.length;i++){
      if(event.results[i].isFinal)editor.value+=(editor.value?'\n':'')+event.results[i][0].transcript;
      else interim+=event.results[i][0].transcript;
    }
    if(bedsideView)bedsideView.source='browser_speech';
    status.textContent=interim?'正在转写：'+interim:'文字已转写，请核对、修改后保存。';
  };
  speech.onerror=()=>{status.textContent='转写中断，请检查麦克风权限或直接编辑文字。';stopBedsideSpeech();};
  speech.onend=stopBedsideSpeech;
  try{speech.start();bedsideRecording=true;document.getElementById('bedsideSpeechButton').textContent='■ 停止转写';status.textContent='正在转写，结束后请核对文字和说话人。';}
  catch(_){status.textContent='无法开始语音转写，请直接输入文字。';stopBedsideSpeech();}
}
async function openBedsideRounding(patientId=null,encounterId=null,roundingId=null,fresh=false){
  stopBedsideSpeech();bedsideView=null;
  const request=++patientDetailRequest;++historyRequest;
  const p=patients.find(p=>p.backendPatientId===patientId)||patients.find(p=>p.backendPatientId===workflowPatientId)||patients[0];
  if(!p){showToast('暂无可用患者');return;}
  patientId=p.backendPatientId;
  const body=document.getElementById('panelBody'),button=document.getElementById('panelAction');
  document.getElementById('panelTitle').textContent='查房问诊记录';document.getElementById('panelSub').textContent='正在读取…';
  body.innerHTML='<div id="bedsideView">正在读取查房资料…</div>';const container=document.getElementById('bedsideView');
  button.disabled=true;button.textContent='保存并完成本次查房';document.getElementById('panelModal').classList.add('show');
  try{
    const profile=await workflowFetch(`/patients/${patientId}/profile?doctor_id=1`+(encounterId?`&encounter_id=${encounterId}`:''));
    if(!profile.encounter_id)throw new Error('该患者没有可用住院记录');
    encounterId=profile.encounter_id;
    const rounds=await workflowFetch(`/rounding-sessions?encounter_id=${encounterId}&doctor_id=1`);
    const selected=roundingId?rounds.find(r=>r.id===roundingId):fresh?null:rounds.find(r=>r.record_mode==='TRANSCRIPT'&&r.status==='DRAFT');
    if(roundingId&&!selected)throw new Error('该次查房不属于所选住院');
    if(selected&&selected.record_mode!=='TRANSCRIPT'){if(request===patientDetailRequest)await openRoundingHistory(selected.id,patientId,encounterId);return;}
    const data=selected?await workflowFetch(`/rounding-sessions/${selected.id}?doctor_id=1`):null;
    if(request!==patientDetailRequest||container!==document.getElementById('bedsideView'))return;
    currentEncounterId=encounterId;currentRoundingId=data?.id||null;workflowPatientId=patientId;
    bedsideView={patientId,encounterId,container,row:data,key:data?.request_key||crypto.randomUUID(),source:data?.transcript_source||'manual'};
    const done=data?.status==='COMPLETED',encounter=profile.encounters.find(e=>e.id===encounterId),closed=encounter.status!=='INPATIENT';
    document.getElementById('panelTitle').textContent='查房问诊 · '+profile.name;
    document.getElementById('panelSub').textContent=profile.bed+' · '+profile.encounter_number;
    container.insertAdjacentHTML('beforebegin',patientTabs(patientId,'roundings',encounterId));
    container.innerHTML=`<div class="workflow-toolbar"><label>患者 <select id="bedsidePatient" onchange="openBedsideRounding(Number(this.value))">${patients.map(p=>`<option value="${p.backendPatientId}" ${p.backendPatientId===patientId?'selected':''}>${html(p.name)} · ${html(p.bed)}</option>`).join('')}</select></label>
      <label>住院 <select id="bedsideEncounter" onchange="openBedsideRounding(${patientId},Number(this.value))">${profile.encounters.map(e=>`<option value="${e.id}" ${e.id===encounterId?'selected':''}>${html(e.encounter_number)}</option>`).join('')}</select></label></div>
      <p>入院：${html(localTime(profile.admitted_at))}</p><p>首次查房截止：${html(localTime(new Date(Date.parse(profile.admitted_at)+48*3600000).toISOString()))}（入院后48小时）</p>
      <p id="bedsideStatus" role="status">${done?'✓ 本次查房已完成':data?'问诊文字已暂存，尚未完成本次查房':'记录本次查房问诊，核对后确认完成'}</p>
      <label for="bedsideTime">实际查房时间</label> <input type="datetime-local" id="bedsideTime" step="1" ${(done||closed)?'disabled':''} />
      <section class="admission-section"><h3>查房问诊文字</h3><button class="secondary bedside-control" id="bedsideSpeechButton" ${(done||closed)?'disabled':''} onclick="toggleBedsideSpeech()">● 开始查房转写</button>
      <p class="section-note" id="bedsideSpeechStatus">支持语音转录、粘贴和文字修改，请核对识别内容并按需要标注说话人。</p>
      <textarea id="bedsideTranscript" ${(done||closed)?'readonly':''} oninput="if(bedsideRecording)stopBedsideSpeech()" placeholder="医生：今天感觉怎么样？&#10;患者：……&#10;直接记录实际查房问诊及所见。"></textarea></section>
      <p>核对原文后直接完成查房。本页不生成Agent总结，也不进入入院记录审查流程。</p>
      <p><button class="secondary bedside-control" id="saveBedsideDraft" ${(done||closed)?'disabled':''} onclick="saveBedsideRounding(false)">暂存问诊文字</button>
      <button class="secondary" id="newBedsideRounding" ${closed?'disabled':''} onclick="openBedsideRounding(${patientId},${encounterId},null,true)">＋ 新一次查房</button>
      <button class="secondary" onclick="openPatientHistory(${patientId},${encounterId},'roundings')">查看历次查房</button></p>
      ${data?`<button class="secondary" id="bedsideMattersButton" onclick="openSavedSourceMatters(${patientId},${encounterId},'ROUNDING',${data.id})">识别已保存问诊中的跟进事项</button>`:''}
      <p id="bedsideError" role="alert" style="color:#b42318"></p>`;
    document.getElementById('bedsideTranscript').value=data?.transcript||'';
    const originalStart=data?.started_at||new Date().toISOString();const time=new Date(originalStart);document.getElementById('bedsideTime').value=new Date(time-time.getTimezoneOffset()*60000).toISOString().slice(0,19);bedsideView.originalStart=originalStart;bedsideView.displayedTime=document.getElementById('bedsideTime').value;
    button.disabled=done||closed;button.textContent=done?'本次查房已完成':'保存并完成本次查房';button.onclick=()=>saveBedsideRounding(true);
  }catch(error){if(request===patientDetailRequest&&container===document.getElementById('bedsideView'))container.textContent='查房资料读取失败：'+error.message;}
}
async function saveBedsideRounding(complete){
  if(bedsideSaving||!bedsideView)return;
  stopBedsideSpeech();const view=bedsideView,error=document.getElementById('bedsideError');
  const text=document.getElementById('bedsideTranscript').value.trim(),time=document.getElementById('bedsideTime').value;
  if(!text){error.textContent='请先记录并核对查房问诊文字';return;}
  if(!time){error.textContent='请填写实际查房时间';return;}
  bedsideSaving=true;error.textContent='';document.querySelectorAll('.bedside-control,#panelAction').forEach(b=>b.disabled=true);
  try{
    const payload={doctor_id:1,patient_id:view.patientId,encounter_id:view.encounterId,request_key:view.key,
      started_at:time===view.displayedTime?view.originalStart:new Date(time).toISOString(),transcript:text,transcript_source:view.source,expected_version:view.row?.version||null};
    let row=await workflowFetch('/bedside-roundings'+(view.row?'/'+view.row.id:''),{method:view.row?'PUT':'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    view.row=row;
    if(complete)row=await postWorkflow(`/bedside-roundings/${row.id}/complete`,{doctor_id:1,expected_version:row.version});
    showToast(complete?'本次查房已完成，首次查房待办已更新':'问诊文字已暂存，查房待办仍保留');
    if(view===bedsideView)await openBedsideRounding(view.patientId,view.encounterId,row.id);
    await loadBackendPatients();await refreshDashboardTotals();
  }catch(exc){if(view===bedsideView){error.textContent='保存失败：'+exc.message+'。文字已保留。';document.querySelectorAll('.bedside-control,#panelAction').forEach(b=>b.disabled=false);}else showToast(exc.message);}
  finally{bedsideSaving=false;}
}
new MutationObserver(()=>{if(bedsideRecording&&(!document.getElementById('bedsideTranscript')||!document.getElementById('panelModal').classList.contains('show')))stopBedsideSpeech();}).observe(document.getElementById('panelModal'),{childList:true,subtree:true,attributes:true,attributeFilter:['class']});
