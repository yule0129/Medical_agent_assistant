// Patient-scoped overview and history. No clinical state is inferred in the browser.
const qualityReasons={WRONG_EXTRACTION:'提取不准确',MISSING_CONTEXT:'缺少上下文',DUPLICATE:'重复事项',WRONG_EVIDENCE:'证据对应错误',PLAN_CHANGED:'实际安排调整',OTHER:'其他'};
function matterWorkSummary(summary){
  if(!summary)return '';
  const labels={needs_confirmation:'待确认',needs_review:'有进展待处理',overdue:'已到计划时间',waiting:'持续跟进'};
  return `<section class="admission-section" id="matterWorkSummary"><h3>本次住院工作摘要</h3><p class="section-note">${html(summary.scope)} · ${html(localTime(summary.generated_at))}</p>${Object.entries(labels).map(([key,label])=>`<details ${key==='needs_review'?'open':''}><summary>${label} · ${summary.groups[key].length} 项</summary>${summary.groups[key].map(i=>`<p><button class="secondary" onclick="focusMatterCard(${i.id})">${html(i.title)}</button> ${html(i.next_step)}<br><small>依据：${html(i.source.label)}${i.evidence.length?'；'+i.evidence.map(e=>html(e.label)).join('、'):''}</small></p>`).join('')||'<p>暂无</p>'}</details>`).join('')}</section>`;
}
async function focusMatterCard(id){
  if(!document.querySelector(`[data-patient-matter="${id}"]`)){
    const v=matterView;if(!v||matterBusy||matterActionBusy)return;
    const inputs=captureMatterInputs(v.box);await openPatientMatters(v.patientId,v.encounterId,'all',v.sourceKey);
    if(matterView?.box===document.getElementById('patientMatterView'))restoreMatterInputs(matterView.box,inputs,inputs.filter(i=>i.id==='matterFilter').map(i=>i.id));
  }
  document.querySelector(`[data-patient-matter="${id}"]`)?.scrollIntoView({block:'center',behavior:'smooth'});
}
function matterWorkflowDetails(i){
  return `<p class="section-note">完成条件：${html(i.completion_contract?.required||'医生记录实际执行结果')}</p>
    <details ontoggle="if(this.open)loadMatterTimeline(${i.id})"><summary>历次查房关联与处理时间线</summary><div id="matterTimeline-${i.id}"></div></details>
    <details><summary>反馈 Agent 判断问题</summary><label>原因 <select id="matterFeedbackReason-${i.id}">${Object.entries(qualityReasons).map(([k,v])=>`<option value="${k}">${v}</option>`).join('')}</select></label><textarea id="matterFeedbackNote-${i.id}" placeholder="说明哪处判断不准确、实际应如何处理。反馈用于质量复盘，不会自动修改安排。"></textarea><button class="secondary" onclick="saveMatterQualityFeedback(${i.id})">保存质量反馈</button><p id="matterFeedbackStatus-${i.id}" role="status"></p></details>`;
}
async function loadMatterTimeline(id){
  const box=document.getElementById('matterTimeline-'+id);if(!box||box.dataset.loading)return;
  box.dataset.loading='1';box.textContent='正在读取…';
  try{const data=await workflowFetch(`/agent/matters/${id}/timeline?doctor_id=1`);
    box.innerHTML=data.items.map(e=>`<section><p><b>${html(e.label)}</b> · ${html(localTime(e.at))}${e.doctor_id?' · 医生 #'+html(e.doctor_id):''}</p>${e.quote?`<blockquote>${html(e.quote)}</blockquote>`:''}<p>${html(e.note||'')}</p>${e.before?.title&&e.after?.title&&e.before.title!==e.after.title?`<p>${html(e.before.title)} → ${html(e.after.title)}</p>`:''}${e.source?`<p class="section-note">${html(e.source.label)} · 版本 ${html(e.source.version)}</p>`:''}${(e.evidence||[]).map(s=>`<details><summary>${html(s.label)}</summary><blockquote>${html(s.quote)}</blockquote></details>`).join('')}</section>`).join('');
  }catch(e){box.textContent='读取失败：'+e.message;}finally{delete box.dataset.loading;}
}
async function saveMatterQualityFeedback(id){
  const item=matterView?.data.items.find(i=>i.id===id),box=document.getElementById('matterFeedbackStatus-'+id),field=document.getElementById('matterFeedbackNote-'+id);
  if(!item||box.dataset.saving)return;
  if(!field.value.trim()){box.textContent='请填写具体问题。';field.focus();return;}
  box.dataset.saving='1';
  try{await postWorkflow(`/agent/matters/${id}/quality-feedback`,{doctor_id:1,expected_version:item.version,reason:document.getElementById('matterFeedbackReason-'+id).value,note:field.value.trim()});box.textContent='反馈已留档，当前事项安排不变。';}
  catch(e){box.textContent='保存失败：'+e.message;}finally{delete box.dataset.saving;}
}
function matterFeedbackSummary(data){
  return `<details><summary>医生质量反馈复盘 · 最近 ${data?.total||0} 条</summary><p class="section-note">操作自动分类仅用于归档，不代表已确认模型错误；反馈不会自动训练模型或改变临床规则。</p>${Object.entries(data?.reasons||{}).map(([k,v])=>`<span class="tag">${html(qualityReasons[k]||k)} ${v}</span>`).join('')}${(data?.items||[]).map(i=>`<p>事项 #${i.matter_id} · ${html(qualityReasons[i.reason]||i.reason)}${i.reason_origin?'（操作默认分类）':''}：${html(i.note||'未填写说明')}</p>`).join('')}</details>`;
}

function matterNextStep(p){
  if(p.stale||p.status!=='SUCCEEDED')return '等待当前资料调查；可记录已知的实际进展';
  return ({RESULT_AVAILABLE:p.acknowledged?'填写报告处理结论并确认事项完成':'审阅相关报告并记录处理结论',RESPONSE_RECORDED:'核对执行记录并填写完成结论',PLAN_CHANGED:'核对新安排并决定是否变更原事项',CONFLICT:'核对冲突资料',UNCLEAR:'人工核对证据对应关系',WAITING:'当前已读取资料中未发现明确进展，继续跟进'})[p.state]||'核对当前资料';
}
function refreshMatterWorkSummary(feed){
  const view=matterView,box=document.getElementById('matterWorkSummary');
  if(!view||!box||view.box!==document.getElementById('patientMatterView'))return;
  const live=feed.summaries?.[String(view.encounterId)];if(!live)return;
  const summary=structuredClone(live);
  summary.groups.needs_confirmation=view.data.work_summary?.groups.needs_confirmation||[];
  const opened=[...box.querySelectorAll('details')].map(e=>e.open);
  const holder=document.createElement('div');holder.innerHTML=matterWorkSummary(summary);
  holder.querySelectorAll('details').forEach((e,n)=>e.open=opened[n]);box.replaceWith(holder.firstElementChild);
}

function openMatterEvidence(id,index){
  const item=matterView?.data.items.find(i=>i.id===id),e=item?.progress?.evidence?.[index];
  if(!e)return;
  const parts=e.id.split(':'),kind=parts[0]==='PROGRESS'?'PROGRESS_NOTE':parts[0];
  if(!['ADMISSION','ROUNDING','LAB','PROGRESS_NOTE'].includes(kind)){showToast('暂不支持此证据来源');return;}
  return openMatterSource(id,{source:{kind,id:Number(parts[1]),label:e.label,at:e.at,version:e.version,text:e.text},quote:e.quote});
}

function matterCompletionReviewHtml(i){
  const p=i.progress||{};
  return p.status==='SUCCEEDED'&&p.state==='RESULT_AVAILABLE'&&!p.stale&&!p.acknowledged?`<p><label><input type="checkbox" id="matterReviewedResult-${i.id}" data-result-id="${p.result_id}" /> 我已审阅当前相关报告，并将在下方记录处理结论</label></p>`:'';
}
function syncMatterWorkflow(item){
  const step=document.querySelector(`[data-matter-next-step="${item.matter_id}"]`);if(step)step.textContent='下一步：'+matterNextStep(item.progress);
  const box=document.getElementById('matterCompletionReview-'+item.matter_id);if(!box)return;
  const htmlValue=matterCompletionReviewHtml({...item,id:item.matter_id}),signature=JSON.stringify([item.progress.status,item.progress.state,item.progress.result_id,item.progress.stale,item.progress.acknowledged]);
  const existing=box.querySelector('input');
  if(box.dataset.signature===signature)return;
  if(existing&&Number(existing.dataset.resultId)===item.progress.result_id&&htmlValue){box.dataset.signature=signature;return;}
  const changed=existing&&htmlValue;box.innerHTML=htmlValue;box.dataset.signature=signature;
  if(changed)box.insertAdjacentHTML('afterbegin','<p role="status">报告依据已更新，请重新审阅当前版本。</p>');
}
