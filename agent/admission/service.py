from datetime import datetime, timedelta, timezone
from uuid import uuid4
from fastapi import HTTPException
from sqlalchemy import select
from backend.models import AuditLog, Encounter, MedicalRecord
from backend.services.report_service import utc
from agent.llm.deepseek import configured
from .models import AdmissionWorkspace
from .provider import get_provider
from .contracts import DraftError
from .loop import generate
from .sources import collect_sources, source_digest


def workspace_for(db, encounter_id, create=False):
    row = db.scalar(select(AdmissionWorkspace).where(AdmissionWorkspace.encounter_id == encounter_id))
    if row is None and create:
        row = AdmissionWorkspace(encounter_id=encounter_id, updated_at=datetime.now(timezone.utc))
        db.add(row)
        db.flush()
    return row


def workspace_out(db, encounter, record=None):
    row = workspace_for(db, encounter.id)
    sources = collect_sources(db, encounter, row)
    stale = bool(row and row.draft and row.source_digest != source_digest(sources))
    signed = bool(record and record.status == '已签署' and record.content.strip())
    status = 'SIGNED' if signed else 'STALE' if stale else row.status if row else 'PENDING_REVIEW' if record and record.content.strip() else 'NEEDS_INPUT'
    if status == 'GENERATING' and row.started_at and datetime.now(timezone.utc) > utc(row.started_at) + timedelta(seconds=120):
        status = 'FAILED'
    return {"status": status, "transcript": row.transcript if row else "",
            "examination_text": row.examination_text if row else "", "input_version": row.input_version if row else 0,
            "draft": row.draft if row else {}, "trace": row.trace if row else [],
            "error_code": row.error_code if row else None,
            "sources": sources, "source_digest": source_digest(sources), "stale": stale and not signed,
            "model_configured": configured(), "reviewed_at": utc(row.reviewed_at).isoformat() if row and row.reviewed_at else None}


def assert_reviewable(db, record, doctor_id):
    if not record.content.strip():
        raise HTTPException(409, "入院记录正文为空，不能审查签署")
    row = workspace_for(db, record.encounter_id)
    if row and row.draft:
        encounter = db.get(Encounter, record.encounter_id)
        if row.source_digest != source_digest(collect_sources(db, encounter, row)):
            raise HTTPException(409, "问诊或检查结果已更新，请重新生成并审查当前资料")
        if row.status == 'GENERATING':
            raise HTTPException(409, "Agent正在生成，请完成后审查")
        row.status, row.reviewed_by = 'SIGNED', doctor_id
        row.reviewed_at = datetime.now(timezone.utc)


def generate_admission(db, encounter, doctor_id, expected_revision):
    from backend.services.admission_service import admission_state
    state = admission_state(db, encounter)
    if state['revision'] != expected_revision:
        raise HTTPException(409, "入院记录已更新，请保留内容并刷新核对")
    if state['record'] and state['record']['status'] == '已签署':
        raise HTTPException(409, "已签署入院记录不能重新生成")
    if not configured():
        raise HTTPException(503, "DeepSeek未配置或Agent未启用")
    row = workspace_for(db, encounter.id, True)
    if not row.transcript.strip():
        raise HTTPException(422, "请先保存并核对问诊对话记录")
    now = datetime.now(timezone.utc)
    if row.status == 'GENERATING' and row.started_at and utc(row.started_at) + timedelta(seconds=120) > now:
        raise HTTPException(409, "Agent正在生成，请勿重复提交")
    sources = collect_sources(db, encounter, row)
    digest, token = source_digest(sources), uuid4().hex
    encounter_id, workspace_id = encounter.id, row.id
    previous_draft = row.draft
    row.status, row.started_at, row.generation_token, row.error_code = 'GENERATING', now, token, None
    db.commit()  # Never hold the encounter lock during a model request.
    try:
        draft, trace = generate(get_provider(), sources)
    except DraftError as exc:
        db.expire_all()
        db.scalar(select(Encounter).where(Encounter.id == encounter_id).with_for_update())
        row = db.get(AdmissionWorkspace, workspace_id)
        if row.generation_token == token:
            row.status, row.error_code, row.trace = 'FAILED', str(exc), exc.trace
            row.generation_token = None
            db.commit()
        raise HTTPException(502, "入院Agent生成失败：" + str(exc)) from None
    db.expire_all()
    encounter = db.scalar(select(Encounter).where(Encounter.id == encounter_id).with_for_update())
    row = db.get(AdmissionWorkspace, workspace_id)
    current = admission_state(db, encounter)
    if row.generation_token != token or source_digest(collect_sources(db, encounter, row)) != digest or current['revision'] != expected_revision:
        if row.generation_token == token:
            row.status, row.generation_token = 'STALE', None
            row.error_code, row.trace = 'SOURCES_CHANGED', trace
            db.commit()
        raise HTTPException(409, "生成期间资料或病历发生变化，旧草稿未覆盖，请重新核对生成")
    profile = sources['read_patient_profile'][0]['data']
    header = '\n'.join(f"{key}：{profile.get(field) or '未记录'}" for key, field in (
        ('姓名','name'), ('性别','gender'), ('年龄','age'), ('民族','ethnicity'), ('婚姻状况','marital_status'),
        ('出生地','birthplace'), ('职业','occupation'), ('入院时间','admitted_at'), ('记录时间','recorded_at'), ('病史陈述者','history_reporter')))
    content = header + '\n\n' + '\n\n'.join(s['heading']+'：'+s['text'] for s in draft['sections'])
    if current['record']:
        record = db.get(MedicalRecord, current['record']['id'])
        before_content = record.content
        record.content, record.status = content, '待审核'
    else:
        before_content = None
        record = MedicalRecord(record_number=f"ADM-{encounter.id:06d}", patient_id=encounter.patient_id,
            encounter_id=encounter.id, doctor_id=doctor_id, record_type='入院记录', content=content,
            status='待审核', created_at=datetime.now(timezone.utc))
        db.add(record)
    db.flush()
    draft['source_snapshot'] = sources
    row.draft, row.trace, row.source_digest = draft, trace, digest
    row.status, row.generation_token, row.updated_at = 'PENDING_REVIEW', None, datetime.now(timezone.utc)
    db.add(AuditLog(actor_type='AGENT', actor_id='admission', action='GENERATE_ADMISSION_DRAFT',
        resource_type='MedicalRecord', resource_id=str(record.id),
        old_state={'content': before_content, 'draft': previous_draft},
        new_state={'content': content, 'draft': draft, 'trace': trace, 'doctor_id': doctor_id}, created_at=row.updated_at))
    db.commit()
    return admission_state(db, encounter)
