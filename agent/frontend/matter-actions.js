let matterActionBusy=false;
const matterActionRequests=new Map();
const actionLabels={CREATE_FOLLOWUP:'新增关联后续事项',RESCHEDULE:'调整计划时间',REVISE_PLAN:'修改原事项计划',COMPLETE_MATTER:'确认完成原事项',CANCEL_PLAN:'取消原事项'};
function captureMatterInputs(root){return [...root.querySelectorAll('input[id],select[id],textarea[id]')].map(e=>({id:e.id,value:e.value,checked:e.checked}));}
function restoreMatterInputs(root,values,skip=[]){for(const v of values){if(skip.includes(v.id))continue;const e=root.querySelector('[id="'+v.id+'"]');if(e){e.value=v.value;if(e.type==='checkbox')e.checked=v.checked;}}}
function matterActionHtml(a,item){
  const c=a.content,editable=a.status==='PROPOSED';
  const parentId=item.matter_id||item.id,owners=matterView?.data.owners||[];
  const current=matterView?.data.items.find(i=>i.id===parentId);
  const status={PROPOSED:a.stale?'依据已过期，不可批准':'待医生批准',APPLIED:'医生已批准并执行',REJECTED:'医生未采纳',BLOCKED:'建议校验未通过'}[a.status];
  return `<article class="admission-section" data-matter-action="${a.id}"><b>${html(actionLabels[c.action])}</b> <span class="tag ${a.status==='APPLIED'?'green':'orange'}">${html(status)}</span>
    <p>${html(c.title)}</p><p>建议原因：${html(c.rationale)}</p>
    ${a.evidence.map(e=>`<blockquote class="record-text">${html(e.label)} · ${html(e.quote)}</blockquote>`).join('')}
    <p>执行效果：${c.action==='CREATE_FOLLOWUP'?'批准后新增一条正式后续事项，关联原事项；原事项保持跟进中。':c.action==='RESCHEDULE'?'批准后更改原事项的计划时间。':c.action==='REVISE_PLAN'?'批准后更新原事项标题及医生指定的安排。':c.action==='COMPLETE_MATTER'?'批准后将原事项标记为已完成。':'批准后将原事项标记为已取消。'}</p>
    ${c.due_text?`<p>原文时间表达：${html(c.due_text)}，具体时间请医生指定。</p>`:''}
    ${editable?`${['CREATE_FOLLOWUP','REVISE_PLAN'].includes(c.action)?`<label>审定标题 <input id="actionTitle-${a.id}" value="${html(c.title)}" /></label>`:''}
      ${['CREATE_FOLLOWUP','RESCHEDULE','REVISE_PLAN'].includes(c.action)?`<p><label>负责人 <select id="actionOwner-${a.id}">${owners.map(o=>`<option value="${o.id}" ${o.id===(current?.assigned_doctor_id||1)?'selected':''}>${html(o.name)}</option>`).join('')}</select></label>
      <label>计划时间 <input type="datetime-local" id="actionDue-${a.id}" /></label></p>`:''}
      <p><label>审核理由／实际处理结论（必填）<textarea id="actionNote-${a.id}" placeholder="请核对当前进展及原文依据，再记录审核决定。"></textarea></label></p>
      <button class="primary" ${a.stale?'disabled':''} onclick="reviewMatterAction(${parentId},${a.id},'APPROVE')">批准：${html(actionLabels[c.action])}</button>
      <button class="secondary" onclick="reviewMatterAction(${parentId},${a.id},'REJECT')">填写原因并不采纳</button>`:`<p>审核记录：${html(a.note||c.blocked_reason||'')}</p>`}
    ${a.outcome?.child_id?`<button class="secondary" onclick="openPatientMatters(${item.patient_id||matterView?.patientId},${item.encounter_id||matterView?.encounterId})">查看后续事项 M#${a.outcome.child_id}</button>`:''}
    <p id="actionError-${a.id}" role="alert" style="color:#b42318"></p></article>`;
}
async function reviewMatterAction(matterId,actionId,decision){
  if(matterActionBusy)return;
  const view=matterView,feed=matterProgressFeed.items.find(i=>i.matter_id===matterId);
  const source=feed?.progress.actions||view?.data.items.find(i=>i.id===matterId)?.progress?.actions||[];
  const proposal=source.find(a=>a.id===actionId);if(!proposal)return;
  const error=document.getElementById('actionError-'+actionId),note=document.getElementById('actionNote-'+actionId)?.value||'';
  if(!note.trim()){error.textContent='请填写审核理由或实际处理结论';return;}
  const due=document.getElementById('actionDue-'+actionId)?.value;
  if(decision==='APPROVE'&&proposal.content.action==='RESCHEDULE'&&!due){error.textContent='请由医生选择新的计划时间';return;}
  if(!matterActionRequests.has(actionId))matterActionRequests.set(actionId,crypto.randomUUID());
  const values=captureMatterInputs(view.box);
  matterActionBusy=true;
  try{
    await postWorkflow(`/agent/matter-actions/${actionId}/review`,{doctor_id:1,expected_version:proposal.version,
      expected_matter_version:proposal.expected_matter_version,decision,request_key:matterActionRequests.get(actionId),note,
      title:document.getElementById('actionTitle-'+actionId)?.value||null,
      assigned_doctor_id:Number(document.getElementById('actionOwner-'+actionId)?.value)||null,
      due_at:due?new Date(due).toISOString():null});
    if(view?.box===document.getElementById('patientMatterView')){
      await openPatientMatters(view.patientId,view.encounterId,view.filter);
      const skip=decision==='APPROVE'&&['REVISE_PLAN','RESCHEDULE'].includes(proposal.content.action)?['matterTitle-'+matterId,'matterOwner-'+matterId,'matterDue-'+matterId]:[];
      restoreMatterInputs(matterView.box,values,skip);
    }
    await refreshDashboardTotals();showToast(decision==='APPROVE'?'已按医生批准执行，关联和审核记录已保存':'建议未采纳，正式事项未变化');
  }catch(exc){if(error)error.textContent='操作失败：'+exc.message;}
  finally{matterActionBusy=false;await refreshMatterProgress();}
}
