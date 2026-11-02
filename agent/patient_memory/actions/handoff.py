from sqlalchemy import select
from backend.models import Encounter
from backend.services.report_service import utc
from agent.patient_memory.models import PatientMatter
from agent.patient_memory.service import item_out,current_source
from agent.patient_memory.progress.sources import build_snapshot


def memory_handoff(db,encounter_id):
    encounter=db.get(Encounter,encounter_id)
    lines=[];references=[]
    for row in db.scalars(select(PatientMatter).where(PatientMatter.encounter_id==encounter_id,
        PatientMatter.status.in_(['CANDIDATE','OPEN'])).order_by(PatientMatter.id)):
        item=item_out(db,row,encounter);p=item['progress']
        try:source_digest=current_source(db,encounter,row.source['kind'],row.source['id'])['digest']
        except Exception:source_digest='MISSING'
        pending=[{'id':a['id'],'version':a['version'],'stale':a['stale']} for a in p.get('actions',[]) if a['status']=='PROPOSED']
        reference={'matter_id':row.id,'patient_id':row.patient_id,'version':row.version,'status':row.status,
            'source_digest':source_digest,'context_digest':build_snapshot(db,row)[1] if row.status=='OPEN' else None,
            'progress_job_id':p.get('job_id'),'progress_status':p['status'],'progress_result_id':p.get('result_id'),
            'progress_acknowledged':p.get('acknowledged',False),'pending_actions':pending}
        references.append(reference)
        lines.append(f"事项 M#{row.id}：{row.title} · {'待医生确认' if row.status=='CANDIDATE' else '跟进中'} · 负责人 {item['assigned_doctor_name'] or '待指派'} · 计划时间 {item['due_at'] or '待安排'}")
        lines.append(f"  来源：{row.source['label']} · 版本 {row.source['version']}；原文：{row.quote[:240]}")
        if item['parent_id']:lines.append(f"  前序事项 M#{item['parent_id']}；本项为医生批准的后续事项。")
        if item['children']:lines.append('  后续事项：'+'；'.join(f"M#{c['id']} {c['status']} {c['title']}" for c in item['children']))
        if p.get('summary'):lines.append(f"  最新进展：{p['summary']} · {'旧反馈，待重新核对' if p.get('stale') else '医生已阅' if p.get('acknowledged') else '待医生核对'}")
        for evidence in p.get('evidence',[]):lines.append(f"  进展依据：{evidence['label']} 版本{evidence['version']} · {evidence['quote'][:240]}")
        for a in p.get('actions',[]):
            if a['status']=='PROPOSED':lines.append(f"  行动建议 A#{a['id']}：{a['content']['title']} · {'已过期，不可执行' if a['stale'] else '待医生批准，尚未执行'}")
    return lines,references


def validate_memory_handoff(db,report):
    from fastapi import HTTPException
    _,current=memory_handoff(db,report.encounter_id)
    saved=[r for r in report.references if 'matter_id' in r]
    if saved!=current:raise HTTPException(409,'患者事项、进展依据或待批准行动已变化，请重新生成交接')
    if any(f"M#{r['matter_id']}" not in report.content for r in current):
        raise HTTPException(409,'交接内容缺少患者事项标识，请保留全部未完成事项')
