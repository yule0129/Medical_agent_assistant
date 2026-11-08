"""Clinician-facing evidence requirements, longitudinal history and work summary.

Derived from scoped persisted records; never changes a clinical state.
"""
from datetime import datetime, timezone
from collections import Counter
from sqlalchemy import select
from backend.models import AuditLog
from backend.services.report_service import utc
from .models import PatientMatter
from .progress.models import MatterProgress

FEEDBACK_REASONS = {
    'WRONG_EXTRACTION': '提取不准确', 'MISSING_CONTEXT': '缺少上下文',
    'DUPLICATE': '重复事项', 'WRONG_EVIDENCE': '证据对应错误',
    'PLAN_CHANGED': '实际安排调整', 'OTHER': '其他',
}


def revise_plan_metadata(matter, title, note):
    """A revised plan must not inherit the old target or old evidence time window."""
    if title == matter.title:
        return
    from .quality import compact
    quality = dict(matter.quality or {})
    quality.update(plan_revision_at=datetime.now(timezone.utc).isoformat(), plan_revision_note=note)
    if quality.get('target_text') and compact(quality['target_text']) not in compact(title):
        quality.update(prior_plan={k: quality.get(k) for k in ('action_text','target_text','evidence_kind')},
                       action_text='', target_text='')
    matter.quality = quality


def completion_contract(matter):
    quality = matter.quality or {}
    kind = quality.get('evidence_kind') or {
        'RECHECK': 'LAB_REPORT', 'CONTACT': 'CONTACT_RECORD',
        'VERIFY_INFO': 'VERIFICATION_RECORD',
    }.get(matter.kind, 'CLINICIAN_NOTE')
    required = {
        'LAB_REPORT': '对应本次安排的检查报告、医生审阅及处理结论',
        'CONTACT_RECORD': '联系对象、回应结果及必要的后续安排',
        'VERIFICATION_RECORD': '核实对象、信息来源及核实结论',
        'CLINICIAN_NOTE': '实际执行结果及必要的后续安排',
    }.get(kind, '由医生明确实际执行结果')
    return {'evidence_kind': kind, 'target': quality.get('target_text') or matter.title,
            'required': required, 'doctor_confirmation_required': True}


def next_step(item):
    p = item.get('progress') or {}
    if item['status'] != 'OPEN':
        return '核对候选及原文' if item['status'] == 'CANDIDATE' else '查看处理记录'
    if p.get('stale') or p.get('status') != 'SUCCEEDED':
        return '等待当前资料调查；可记录已知的实际进展'
    return {
        'RESULT_AVAILABLE': '填写报告处理结论并确认事项完成' if p.get('acknowledged') else '审阅相关报告并记录处理结论',
        'RESPONSE_RECORDED': '核对执行记录并填写完成结论',
        'PLAN_CHANGED': '核对新查房安排，决定是否变更原事项',
        'CONFLICT': '核对冲突资料，明确当前安排',
        'UNCLEAR': '人工核对证据与事项的对应关系',
        'WAITING': '当前已读取资料中未发现明确进展，继续跟进',
    }.get(p.get('state'), '核对当前资料')


def timeline(db, matter):
    events = [{'at': matter.source.get('at'), 'type': 'ORIGIN',
               'label': '原始安排', 'quote': matter.quote, 'source': matter.source}]
    # Linking preserves each later encounter note and the explicit physician decision.
    for linked in db.scalars(select(PatientMatter).where(
            PatientMatter.encounter_id == matter.encounter_id,
            PatientMatter.patient_id == matter.patient_id)):
        if (linked.quality or {}).get('linked_matter_id') == matter.id:
            events.append({'at': utc(linked.updated_at).isoformat(), 'type': 'LINKED_NOTE',
                           'label': '后续记录已关联', 'quote': linked.quote,
                           'note': linked.resolution, 'source': linked.source})
    for observation in db.scalars(select(MatterProgress).where(
            MatterProgress.matter_id == matter.id).order_by(MatterProgress.id.desc()).limit(50)):
        events.append({'at': utc(observation.created_at).isoformat(), 'type': 'OBSERVATION',
                       'label': '历史进展判断（以当前调查为准）', 'note': observation.summary,
                       'evidence': observation.evidence})
    from .actions.models import MatterAction
    for action in db.scalars(select(MatterAction).where(MatterAction.matter_id == matter.id,
            MatterAction.reviewed_at.is_not(None)).order_by(MatterAction.id.desc()).limit(50)):
        events.append({'at': utc(action.reviewed_at).isoformat(), 'type': 'ACTION_' + action.status,
                       'label': '医生处理进展建议', 'doctor_id': action.reviewed_by,
                       'note': action.note, 'after': action.outcome})
    for audit in db.scalars(select(AuditLog).where(AuditLog.resource_type == 'PatientMatter',
            AuditLog.resource_id == str(matter.id)).order_by(AuditLog.id.desc()).limit(100)):
        if audit.actor_type == 'DOCTOR':
            events.append({'at': utc(audit.created_at).isoformat(), 'type': audit.action,
                           'label': '医生处理', 'doctor_id': audit.actor_id,
                           'before': audit.old_state, 'after': audit.new_state,
                           'note': (audit.new_state or {}).get('note', '')})
    return sorted(events, key=lambda e: datetime.fromisoformat(e['at']).astimezone(timezone.utc)
                  if e.get('at') else datetime.min.replace(tzinfo=timezone.utc), reverse=True)


def work_summary(items):
    now = datetime.now(timezone.utc)
    groups = {'needs_confirmation': [], 'needs_review': [], 'overdue': [], 'waiting': []}
    for item in items:
        if item['status'] not in {'CANDIDATE', 'OPEN'}:
            continue
        p = item.get('progress') or {}
        entry = {'id': item['id'], 'title': item['title'], 'next_step': next_step(item),
                 'source': {k: item['source'].get(k) for k in ('kind', 'id', 'label', 'at')},
                 'evidence': p.get('evidence', []) if not p.get('stale') else []}
        if item['status'] == 'CANDIDATE': key = 'needs_confirmation'
        elif p.get('needs_attention') or (p.get('status')=='SUCCEEDED' and not p.get('stale') and p.get('state') not in {'WAITING',None}): key = 'needs_review'
        else: key = 'waiting'
        groups[key].append(entry)
        if item['status'] == 'OPEN' and item.get('due_at') and datetime.fromisoformat(item['due_at']) < now:
            groups['overdue'].append(entry)
    return {'generated_at': now.isoformat(), 'groups': groups,
            'scope': '本次住院已保存事项与已读取资料；未找到记录不代表未执行'}


def feedback_summary(db, rows):
    ids = [str(row.id) for row in rows]
    if not ids: return {'total': 0, 'reasons': {}, 'items': []}
    audits = db.scalars(select(AuditLog).where(AuditLog.resource_type == 'PatientMatter',
        AuditLog.resource_id.in_(ids), AuditLog.action == 'MATTER_QUALITY_FEEDBACK')
        .order_by(AuditLog.id.desc()).limit(100)).all()
    return {'total': len(audits), 'scope': '本次住院最近100条医生质量反馈',
            'reasons': dict(Counter((a.new_state or {}).get('reason', 'OTHER') for a in audits)),
            'items': [{'matter_id': int(a.resource_id), 'at': utc(a.created_at).isoformat(),
                       **(a.new_state or {})} for a in audits]}
