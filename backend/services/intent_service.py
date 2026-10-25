from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ClinicalIntent, ClinicalEvent, OpenLoop, LoopStatus, Encounter, Patient, AuditLog
from ..providers.rules import extract_intent


def _key(event: ClinicalEvent, fingerprint: str) -> str:
    digest = hashlib.sha256(f"{event.event_key}|{fingerprint}".encode()).hexdigest()[:24]
    return f"INT-{digest}"


def create_candidates(db: Session, event: ClinicalEvent) -> list[ClinicalIntent]:
    payload_text = str(event.payload.get("text", ""))
    candidates = []
    for candidate in extract_intent(payload_text):
        intent_key = _key(event, candidate.fingerprint)
        intent = db.scalar(select(ClinicalIntent).where(ClinicalIntent.intent_key == intent_key))
        if intent is None:
            intent = ClinicalIntent(intent_key=intent_key, patient_id=event.patient_id, encounter_id=event.encounter_id, source_event_id=event.id, raw_text=candidate.raw_text, target=candidate.target[:120], intent_type=candidate.intent_type, status="CANDIDATE", created_at=event.occurred_at)
            db.add(intent)
            db.flush()
        candidates.append(intent)
    return candidates


def candidate_target(intent: ClinicalIntent) -> str:
    if intent.target:
        return intent.target
    candidates = extract_intent(intent.raw_text)
    return next((c.target for c in candidates if c.intent_type == intent.intent_type), intent.raw_text)[:120]


def require_scope(db: Session, encounter_id: int, doctor_id: int):
    encounter = db.get(Encounter, encounter_id)
    patient = db.get(Patient, encounter.patient_id) if encounter else None
    if not encounter or not patient or (patient.doctor_id != doctor_id and encounter.attending_doctor_id != doctor_id):
        raise ValueError("doctor is outside encounter scope")


def confirm_intent(db: Session, intent_id: int, waiting_for: str | None, doctor_id: int, due_at: datetime | None = None) -> OpenLoop:
    intent = db.scalar(select(ClinicalIntent).where(ClinicalIntent.id == intent_id).with_for_update())
    if intent is None:
        raise ValueError("intent not found")
    require_scope(db, intent.encounter_id, doctor_id)
    if intent.status == "CONFIRMED":
        existing = db.scalar(select(OpenLoop).where(OpenLoop.intent_id == intent.id, OpenLoop.parent_loop_id.is_(None)))
        if existing:
            return existing
    if intent.status != "CANDIDATE":
        raise ValueError("only candidate intent can be confirmed")
    intent.status = "CONFIRMED"
    now = datetime.now(timezone.utc)
    loop_key = f"LOOP-{intent.intent_key}"
    loop = db.scalar(select(OpenLoop).where(OpenLoop.loop_key == loop_key))
    if loop is None:
        loop = OpenLoop(loop_key=loop_key, intent_id=intent.id, patient_id=intent.patient_id, encounter_id=intent.encounter_id, waiting_for=waiting_for or candidate_target(intent), due_at=due_at, status=LoopStatus.WAITING_EVENT, version=1, created_at=now, updated_at=now, state_reason=f"doctor:{doctor_id} confirmed")
        db.add(loop)
        db.flush()
        db.add(AuditLog(actor_type="DOCTOR", actor_id=str(doctor_id), action="CONFIRM_INTENT", resource_type="OpenLoop", resource_id=str(loop.id), new_state={"status": "WAITING_EVENT"}, created_at=now))
    return loop
