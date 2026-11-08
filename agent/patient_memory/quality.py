"""Conservative deterministic gates in addition to a separate semantic review.

These checks cannot prove clinical correctness. Unclear items stay candidates
until a physician supplies an explicit plan; none changes clinical orders.
"""
import re
from agent.admission.contracts import DraftError

POLICY_VERSION='extraction-quality-v1'
VAGUE_TARGETS={'检查','相关检查','炎症指标','情况','治疗','当前方案','抗感染治疗'}
EVIDENCE_LABELS={'LAB_REPORT':'相关检查报告','VERIFICATION_RECORD':'核实结果记录',
    'CONTACT_RECORD':'联系／会诊回应记录','CLINICIAN_NOTE':'实际执行记录','UNSPECIFIED':'待明确'}


def compact(text):
    return re.sub(r'[\W_]+','',text).casefold()


def assess(proposal,review,source,known):
    if proposal.quote not in source['text'] or proposal.due_text and proposal.due_text not in proposal.quote:
        raise DraftError('UNGROUNDED_SOURCE_QUOTE')
    if not set(review.duplicate_ids)<={r['id'] for r in known}:raise DraftError('DUPLICATE_OUTSIDE_SCOPE')
    issues=[];decision=review.decision
    if proposal.classification in {'BACKGROUND','ALREADY_DONE','NEGATED'}:decision='SKIP'
    elif proposal.classification!='ACTIONABLE':
        issues.append(proposal.clarification or '需医生明确这是否为当前待执行计划')
    action,target=proposal.action_text,proposal.target_text
    if not action or not target:issues.append('请明确执行动作及具体对象')
    elif action not in proposal.quote or target not in proposal.quote:
        issues.append('动作或对象不在引用原文中，请核实并补充实际安排')
    if proposal.evidence_kind=='UNSPECIFIED':issues.append('请明确后续可核对的资料类型')
    if compact(target) in VAGUE_TARGETS:
        issues.append('对象过于笼统，请明确具体检查、信息或执行内容')
    if compact(proposal.title) in {'完善检查','继续当前抗感染方案','继续抗感染治疗','住院规范抗感染治疗'}:
        decision='SKIP' if '抗感染' in proposal.title else decision
    if action:
        # Only direct action negation/completion; avoid e.g. “无需停药，复查…”.
        if re.search(r'(?:暂不|不再|无需|取消|停止)\s*'+re.escape(action),proposal.quote):decision='SKIP'
        if re.search(r'(?:已经|已)\s*'+re.escape(action),proposal.quote):decision='SKIP'
    if action and target and (compact(action) not in compact(proposal.title) or compact(target) not in compact(proposal.title)):
        issues.append('标题与原文动作或对象不一致，请医生核对标题')
    position=source['text'].find(proposal.quote)
    action_position=source['text'].find(action,position) if action else position
    speakers=list(re.finditer(r'(医生|医师|患者|病人|家属)\s*[:：]',source['text'][:max(position,action_position)+1]))
    if speakers and speakers[-1].group(1) in {'患者','病人','家属'}:
        issues.append('该安排来自患者或家属表述，需医生明确确认')
    duplicate_ids=set(review.duplicate_ids)
    for row in known:
        previous=row.get('quality') or {}
        same_target=bool(action and target and compact(action)==compact(previous.get('action_text',''))
                         and compact(target)==compact(previous.get('target_text','')))
        if row['kind']==proposal.kind and (compact(row['title'])==compact(proposal.title) or same_target):
            duplicate_ids.add(row['id'])
    if issues and decision!='SKIP':decision='CLARIFY'
    return {'policy_version':POLICY_VERSION,'admission':decision,'issues':list(dict.fromkeys(issues)),
        'action_text':action,'target_text':target,'evidence_kind':proposal.evidence_kind,
        'review_reason':review.reason,'classification':proposal.classification,'duplicate_ids':sorted(duplicate_ids)}


def for_item(row):
    if row.quality:return dict(row.quality)
    if row.status!='CANDIDATE':return {}
    return {'policy_version':'legacy','admission':'CLARIFY','issues':['旧版本候选尚未通过质量核验，请医生明确实际安排'],
        'action_text':'','target_text':'','evidence_kind':'UNSPECIFIED','duplicate_ids':[]}
