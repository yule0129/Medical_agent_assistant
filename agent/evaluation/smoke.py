"""Opt-in online verification using a disposable synthetic database, never the dev DB.

python -m agent.evaluation.smoke --scenario missing|found|follow|all
"""
import argparse
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from backend.db import Base
from agent.persistence.models import AgentJob, AgentProposal, AgentRun, AgentStep
from backend.models import (ClinicalEvent, ClinicalIntent,
    Doctor, Encounter, LabReport, MedicalRecord, OpenLoop, Patient, ResultAcknowledgement)
from backend.services.report_service import investigate as match_report
from agent.llm.deepseek import configured, PROMPT_VERSION
from agent.runtime.queue import enqueue
from agent.runtime.worker import process_agent_once


def scenario(name):
    with tempfile.TemporaryDirectory(prefix="clinloop-online-") as temporary:
        engine=create_engine("sqlite:///"+str(Path(temporary)/"synthetic.sqlite"))
        Base.metadata.create_all(engine)
        now=datetime.now(timezone.utc)
        with Session(engine,autoflush=False) as db:
            db.add(Doctor(id=1,username="online-synthetic",name="合成医生",department="工程验证"))
            db.add(Patient(id=1,name="完全虚构患者",admission_number="SYNTH-ONLY",bed_number="TEST",diagnosis="合成工程验证",doctor_id=1))
            db.flush()
            db.add(Encounter(id=1,encounter_number="SYNTH-ENC",patient_id=1,attending_doctor_id=1,admitted_at=now-timedelta(days=1)))
            db.flush()
            event=ClinicalEvent(event_key="SYNTH-LAB",event_type="LAB_RESULT_CREATED",patient_id=1,encounter_id=1,
                occurred_at=now-timedelta(minutes=10),received_at=now-timedelta(minutes=10),source="synthetic_online_verification",
                payload={"doctor_id":1,"report_id":"BC-R1","report_version":1,"test_name":"血培养","specimen_id":"BC-S1","order_id":None,"result_text":"完全合成血培养报告：已返回，供工作流工程验证。"})
            db.add(event);db.flush()
            lab=LabReport(event_id=event.id,patient_id=1,encounter_id=1,report_id="BC-R1",report_version=1,test_name="血培养",specimen_id="BC-S1",
                result_text=event.payload["result_text"],reported_at=event.occurred_at)
            intent=ClinicalIntent(intent_key="SYNTH-INTENT",patient_id=1,encounter_id=1,source_event_id=event.id,raw_text="血培养结果出来后通知我",target="血培养",intent_type="FOLLOW_RESULT",status="CONFIRMED",created_at=now-timedelta(hours=1))
            db.add_all([lab,intent]);db.flush()
            loop=OpenLoop(loop_key="SYNTH-LOOP",intent_id=intent.id,patient_id=1,encounter_id=1,waiting_for="血培养",status="WAITING_EVENT",version=1,
                matching_criteria={"test_name":"血培养","specimen_id":"BC-S1"},workflow_memory={},created_at=now-timedelta(hours=1),updated_at=now)
            db.add(loop);db.flush();match_report(db,loop,event)
            if name=="found":
                db.add(ResultAcknowledgement(loop_id=loop.id,evidence_id=loop.current_evidence_id,doctor_id=1,note="医生明确确认已查看血培养报告 BC-R1 v1",created_at=now-timedelta(minutes=2),idempotency_key="SYNTH-ACK"))
                loop.status="ACKNOWLEDGED";loop.version+=1
            if name=="follow":
                db.add(MedicalRecord(record_number="SYNTH-NOTE",patient_id=1,encounter_id=1,doctor_id=1,record_type="日常病程记录",status="草稿",
                    content="已知晓本次血培养报告 BC-R1 v1，标本 BC-S1；继续等待同一标本的药敏结果。",created_at=now-timedelta(minutes=2)))
            job=enqueue(db,loop,event,1,"online-"+name);job_id=job.id;db.commit()
            result=process_agent_once(db,job_id=job_id)
            proposals=db.scalars(select(AgentProposal)).all()
            tools=[step.tool_name for step in db.scalars(select(AgentStep).where(AgentStep.run_id==result["run_id"])) if step.tool_name]
            expected={"missing":"RESPONSE_NOT_FOUND","found":"RESPONSE_FOUND","follow":"FOLLOW_UP"}[name]
            passed=bool(proposals and proposals[-1].content["candidate"]["kind"]==expected)
            report={"scenario":name,"provider":db.get(AgentRun,result["run_id"]).provider,"model":db.get(AgentJob,job_id).result.get("model"),"prompt_version":PROMPT_VERSION,"verified_at":datetime.now(timezone.utc).isoformat(),"synthetic_only":True,"result":result,"tools":tools,"expected_candidate":expected,
                    "trace":[{"step":step.step_number,"action":step.action,"tool_name":step.tool_name,"input":step.input_summary,"result":step.result_summary,"duration_ms":step.duration_ms}
                        for step in db.scalars(select(AgentStep).where(AgentStep.run_id==result["run_id"]).order_by(AgentStep.step_number))],
                    "actual_candidate":proposals[-1].content["candidate"]["kind"] if proposals else None,
                    "diagnostics":[step.result_summary for step in db.scalars(select(AgentStep).where(AgentStep.run_id==result["run_id"],AgentStep.action.in_(["MODEL_ERROR","GUARD_REJECTED","ACT"]))) if step.action != "ACT" or not step.result_summary.get("query_succeeded")],"passed":passed}
        engine.dispose()
        return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario",choices=["missing","found","follow","all"],default="all")
    parser.add_argument("--output",help="Save synthetic traces as JSON, without credentials")
    args=parser.parse_args()
    if not configured():
        print("DeepSeek 未配置：仅在本地 .env 设置 DEEPSEEK_API_KEY；不读取或上传开发库病例。")
        raise SystemExit(2)
    reports=[scenario(name) for name in (["missing","found","follow"] if args.scenario=="all" else [args.scenario])]
    if args.output:
        output=Path(args.output)
        output.parent.mkdir(parents=True,exist_ok=True)
        output.write_text(json.dumps(reports,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps([{k:v for k,v in report.items() if k!="trace"} for report in reports],ensure_ascii=False,indent=2))
    raise SystemExit(0 if all(r["passed"] for r in reports) else 1)


if __name__=="__main__":
    main()
