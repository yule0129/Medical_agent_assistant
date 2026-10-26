"""Browser smoke test against an isolated, temporary SQLite API (no development DB writes).

Start the frontend on :5500 and a disposable headless Chromium/Edge with CDP on
:9222, then run `python tests/browser_rounding_smoke.py` from root.
Requires the optional `websockets` package. Microphone hardware/ASR is not tested.
"""
import json
import sys
import threading
import time
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from websockets.sync.client import connect

from backend.db import Base, get_db
from backend.main import app
from backend.models import Doctor, Patient, Encounter
from agent.llm import deepseek as agent_provider
from agent.runtime.worker import process_agent_once
from backend.worker.processor import run_once
from test_agent_runtime import TestModel
from test_admission_agent import AdmissionTestModel
from agent.admission import service as admission_service


def main():
    # Browser requests run concurrently. Use independent connections instead of
    # sharing one SQLite connection among multiple API threads.
    original_configured = agent_provider.configured
    agent_provider.configured = lambda: True  # Injected test model only, never an online claim.
    original_admission_configured,original_admission_provider=admission_service.configured,admission_service.get_provider
    admission_service.configured=lambda:True
    admission_service.get_provider=lambda:AdmissionTestModel()
    temporary = tempfile.TemporaryDirectory(prefix='health-agent-browser-')
    engine = create_engine('sqlite:///' + str(Path(temporary.name) / 'workflow.sqlite'), connect_args={'check_same_thread': False})
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Doctor(id=1, username='browser', name='合成医生', department='测试'))
        db.add(Patient(id=1, name='浏览器合成患者', admission_number='BROWSER-1', bed_number='01床', diagnosis='测试', doctor_id=1, gender='女', age=52, ethnicity='汉族', marital_status='已婚', birthplace='江苏省南京市', occupation='教师'))
        db.flush()
        db.add(Encounter(id=1, encounter_number='BROWSER-ENC', patient_id=1, attending_doctor_id=1, admitted_at=datetime.now(timezone.utc), recorded_at=datetime.now(timezone.utc), history_reporter='患者本人'))
        db.add(Encounter(id=2, encounter_number='BROWSER-OLD-ENC', patient_id=1, attending_doctor_id=1, status='DISCHARGED', admitted_at=datetime(2025,1,1,tzinfo=timezone.utc)))
        db.commit()

    def session():
        with Session(engine, autoflush=False) as db:
            yield db

    app.dependency_overrides[get_db] = session
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=0, log_level='error'))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(.05)

    assert server.started, 'Isolated test API did not start'
    api_port=server.servers[0].sockets[0].getsockname()[1]
    tabs = json.load(urlopen('http://127.0.0.1:9222/json/list'))
    page = next(t for t in tabs if t['type'] == 'page')
    errors = []
    with connect(page['webSocketDebuggerUrl'], max_size=10_000_000) as ws:
        sequence = 0

        def cdp(method, params=None):
            nonlocal sequence
            sequence += 1
            ws.send(json.dumps({'id': sequence, 'method': method, 'params': params or {}}))
            while True:
                message = json.loads(ws.recv(timeout=20))
                if message.get('method') == 'Runtime.exceptionThrown':
                    errors.append(message['params'])
                if message.get('id') == sequence:
                    if 'error' in message:
                        raise AssertionError(message)
                    return message.get('result', {})

        def js(expression):
            result = cdp('Runtime.evaluate', {'expression': expression, 'awaitPromise': True, 'returnByValue': True})
            if result.get('exceptionDetails'):
                raise AssertionError(result['exceptionDetails'])
            return result.get('result', {}).get('value')

        def wait(expression):
            for _ in range(120):
                if js(expression):
                    return
                time.sleep(.05)
            raise AssertionError('Timeout: ' + expression + '\n' + str(js('document.body.innerText')))

        try:
            cdp('Runtime.enable')
            cdp('Page.enable')
            cdp('Page.addScriptToEvaluateOnNewDocument', {'source': f"window.HEALTH_AGENT_API='http://127.0.0.1:{api_port}/api'"})
            errors.clear()  # Runtime.enable can replay exceptions from the previous page.
            cdp('Page.navigate', {'url': 'http://127.0.0.1:5500/index.html'})
            wait("document.getElementById('homeWorkflowStatus')?.textContent==='已连接'")
            js("document.querySelector('[data-patient-open=\"1\"]').click()")
            wait("document.getElementById('patientBasicInfo')!==null")
            assert js("document.getElementById('patientBasicTab').getAttribute('aria-selected')==='true'")
            assert js("JSON.stringify([...document.querySelectorAll('#patientBasicInfo dt')].map(n=>n.textContent))") == json.dumps(['姓名','性别','年龄','民族','婚姻状况','出生地','职业','入院时间','记录时间','病史陈述者'],ensure_ascii=False,separators=(',',':'))
            assert js("document.getElementById('patientBasicInfo').textContent.includes('汉族') && document.getElementById('patientBasicInfo').textContent.includes('患者本人')")
            js("document.getElementById('patientAdmissionTab').click()")
            wait("document.getElementById('admissionTranscript')!==null")
            assert js("document.getElementById('admissionStatus').textContent.includes('问诊')")
            js("document.getElementById('generateAdmissionDraft').click()")
            assert js("document.getElementById('admissionError').textContent.includes('问诊')")
            assert js("!document.getElementById('admissionTranscript').readOnly")
            js("document.getElementById('admissionTranscript').value='医生：哪里不舒服？患者：咳嗽3天，夜间明显。';document.getElementById('admissionTranscript').dispatchEvent(new Event('input'))")
            assert js("document.getElementById('admissionInterviewEditStatus').textContent.includes('尚未保存')")
            js("document.getElementById('saveAdmissionInterview').click()")
            wait("!admissionSaving && document.getElementById('admissionTranscript')?.value.includes('夜间明显')")
            js('openAdmissionRecord(1,1)')
            wait("document.getElementById('admissionTranscript')?.value.includes('夜间明显')")
            js("openTaskSchedule('pending')")
            wait("document.querySelector('[data-admission-task=\"1\"]')!==null")
            js("document.querySelector('[data-admission-task=\"1\"] .primary').click()")
            wait("document.getElementById('admissionTranscript')!==null")
            js("document.getElementById('admissionTranscript').value='医生：哪里不舒服？患者：咳嗽3天。';document.getElementById('admissionExams').value='合成检查结果 <img src=x onerror=window.admissionInjected=true>';document.getElementById('generateAdmissionDraft').click()")
            wait("document.getElementById('admissionStatus')?.textContent.includes('待医生审查') && !admissionSaving")
            assert js("document.getElementById('admissionContent').value.includes('咳嗽3天') && document.getElementById('admissionTrace').textContent.includes('read_interview')")
            assert not js('window.admissionInjected===true')
            js("document.getElementById('panelAction').click()")
            assert js("document.getElementById('admissionError').textContent.includes('确认')")
            js("document.getElementById('admissionContent').value+='医生已核实补充';document.getElementById('saveAdmissionDraft').click()")
            wait("!admissionSaving && document.getElementById('admissionContent')?.value.includes('医生已核实补充')")
            js("openTaskSchedule('pending')")
            wait("document.querySelector('[data-admission-task=\"1\"]')!==null")
            js("document.querySelector('[data-admission-task=\"1\"] .primary').click()")
            wait("document.getElementById('admissionReviewConfirmed')!==null")
            assert js("document.getElementById('admissionTranscript').value.includes('咳嗽3天')")
            js("document.getElementById('admissionReviewConfirmed').checked=true;document.getElementById('panelAction').click()")
            wait("document.getElementById('admissionStatus')?.textContent.includes('已审查并签署') && !admissionSaving")
            js("openTaskSchedule('pending')")
            wait("document.getElementById('taskFilter')?.value==='pending'")
            assert js("document.querySelector('[data-admission-task=\"1\"]')===null")
            js('openPatientDetailById(1)')
            wait("document.getElementById('patientBasicInfo')!==null")
            js("document.getElementById('patientRoundingsTab').click()")
            wait("document.getElementById('historyRoundingsSection')?.hidden===false")
            assert js("document.getElementById('historyRecordsSection').hidden")
            js("document.getElementById('patientHistoryButton').click()")
            wait("document.getElementById('historyRecordsSection')?.hidden===false")
            assert js("document.getElementById('historyRoundingsSection').hidden")
            js("document.getElementById('patientBasicTab').click()")
            wait("document.getElementById('patientBasicInfo')!==null")
            js('closePanel()')
            js('openRound()')
            wait("document.getElementById('roundModal').classList.contains('show')")
            transcript = '明天复查血常规。监测血氧。联系心内科会诊。血培养结果出来后通知我。<img src=x onerror="window.injected=true">'
            js(f"document.getElementById('roundTranscript').value={json.dumps(transcript)}; document.getElementById('generateBtn').click()")
            wait("document.getElementById('generateBtn').textContent.includes('提取小任务')")
            js("document.getElementById('generateBtn').click()")
            wait("document.querySelectorAll('#workflowIntents .workflow-item').length===4")
            assert js("document.getElementById('finishRoundingButton').disabled")
            round_id = js('currentRoundingId')
            js("closePanel();openPatientDetailById(1)")
            wait("document.getElementById('patientBasicInfo')!==null")
            js("document.querySelector('#panelBody button[onclick^=\"continueRounding\"]').click()")
            wait("document.getElementById('finishRoundingButton')!==null && document.getElementById('panelModal').classList.contains('show')")
            assert js(f'currentRoundingId==={round_id}')
            assert js("!document.getElementById('roundModal').classList.contains('show')")
            # Read the just-created record with an empty client-side cache.
            js('medicalRecords.splice(0)')
            js("[...document.querySelectorAll('#panelBody button')].find(b=>b.textContent==='查看本次病历').click()")
            wait("document.getElementById('recordTextView')?.textContent.includes('明天复查血常规')")
            assert js('window.injected===undefined')
            assert js("document.getElementById('recordTextView').querySelector('img')===null")
            record_id = js('medicalRecords[0].id')
            js('openWorkflowPanel()')
            wait("document.querySelectorAll('#workflowIntents .workflow-item').length===4")
            js("{const follow=workflowIntents.find(i=>i.intent_type==='FOLLOW_RESULT'); document.getElementById('due-'+follow.id).value='2026-10-24T09:00';document.querySelector('[data-intent-id=\"'+follow.id+'\"] .primary').click();}")
            wait("document.querySelectorAll('#workflowLoops .workflow-item').length===1")
            for remaining in [2,1,0]:
                js("document.querySelector('#workflowIntents .primary').click()")
                wait(f"document.querySelectorAll('#workflowIntents .workflow-item').length==={remaining}")
            assert js("!document.getElementById('finishRoundingButton').disabled")
            js("document.getElementById('finishRoundingButton').click()")
            wait("document.getElementById('roundingLifecycle')?.textContent.includes('本次查房已结束')")
            assert js("document.getElementById('roundCompleted').textContent==='1'")
            assert js("patients[0].status==='已完成'")
            js('openTaskSchedule()')
            wait("document.getElementById('panelBody').textContent.includes('查看／处理')")
            js("{const task=workflowTasks.find(t=>t.intent_type==='FOLLOW_RESULT'); document.querySelector('[data-task-id=\"'+task.id+'\"] .primary').click();}")
            wait("document.getElementById('taskNote')!==null")
            js("document.getElementById('taskNote').value='合成执行记录'; document.querySelector('#panelBody .primary').click()")
            wait("document.querySelector('#panelBody .primary')?.textContent==='确认已查看结果'")
            wait('!taskSaving')
            js("document.querySelector('#panelBody .primary').click()")
            wait("document.querySelector('#panelBody .primary')?.textContent==='保存处理结论并完成'")
            wait('!taskSaving')
            js("document.querySelector('#panelBody .primary').click()")
            assert js("document.getElementById('taskError').textContent.length>0")
            js("document.getElementById('taskNote').value='测试医生处理完成'; document.querySelector('#panelBody .primary').click()")
            wait("document.getElementById('panelBody').textContent.includes('已完成') && !document.getElementById('taskNote')")
            wait('!taskSaving')
            for kind in ['ACTION','MONITOR','RECHECK']:
                js("openTaskSchedule('pending')")
                wait("document.getElementById('taskFilter')?.value==='pending'")
                js("{const task=workflowTasks.find(t=>t.intent_type==='"+kind+"');document.querySelector('[data-task-id=\"'+task.id+'\"] .primary').click();}")
                wait("document.getElementById('taskNote')!==null")
                if kind != 'RECHECK':
                    assert js("document.querySelector('#panelBody .primary').textContent==='记录执行情况并完成任务'")
                    js("document.querySelector('#panelBody .primary').click()")
                    assert js("document.getElementById('taskError').textContent.length>0")
                js("document.getElementById('taskNote').value='合成执行或检验结果';document.querySelector('#panelBody .primary').click()")
                if kind == 'RECHECK':
                    wait("document.querySelector('#panelBody .primary')?.textContent==='确认已查看结果' && !taskSaving")
                    js("document.querySelector('#panelBody .primary').click()")
                    wait("document.querySelector('#panelBody .primary')?.textContent==='保存处理结论并完成' && !taskSaving")
                    js("document.getElementById('taskNote').value='医生处理结论';document.querySelector('#panelBody .primary').click()")
                wait("document.getElementById('panelBody').textContent.includes('任务已完成') && !document.getElementById('taskNote') && !taskSaving")
            assert js("document.getElementById('pendingTaskTotal').textContent==='0'")
            cdp('Page.reload')
            wait("document.getElementById('homeWorkflowStatus')?.textContent==='已连接'")
            js(f'openMedicalRecord({record_id})')
            wait("document.getElementById('recordTextView')?.textContent.includes('明天复查血常规')")
            js('openTaskSchedule()')
            wait("document.getElementById('panelBody').textContent.includes('待办 0 个 · 已完成 5 个')")
            assert js("document.querySelectorAll('[data-task-id]').length===0")
            js("document.getElementById('taskFilter').value='completed';document.getElementById('taskFilter').dispatchEvent(new Event('change'))")
            wait("document.querySelectorAll('[data-task-id]').length===4")
            js('openWorkflowPanel()')
            wait("document.getElementById('roundingLifecycle')?.textContent.includes('本次查房已结束')")
            assert js("document.getElementById('roundingLifecycle').textContent.includes('后续待办 0 个 · 已完成 4 个')")
            assert js("document.getElementById('roundCompleted').textContent==='1'")
            assert js(f'currentRoundingId==={round_id}')
            js('openHandoff()')
            wait("document.getElementById('panelBody').textContent.includes('当前没有未完成任务或待确认计划')")
            # Start another visit for the same patient and inspect both notes.
            js('openPatientDetailById(1)')
            wait("document.getElementById('patientBasicInfo')!==null")
            js("document.getElementById('patientHistoryButton').click()")
            wait("document.querySelectorAll('[data-history-record]').length===2")
            assert js("document.querySelectorAll('#historyEncounter option').length===3")
            js("document.getElementById('panelAction').click()")
            wait("document.getElementById('roundModal').classList.contains('show')")
            assert js("document.getElementById('roundTranscript').value===''")
            js("document.getElementById('roundTranscript').value='午后再次查房，患者自诉好转。';document.getElementById('generateBtn').click();document.getElementById('generateBtn').click()")
            wait(f"currentRoundingId!=={round_id} && document.getElementById('roundingLifecycle')!==null")
            second_round_id=js('currentRoundingId')
            js('openPatientHistory(1)')
            wait("document.querySelectorAll('[data-history-record]').length===3 && document.querySelectorAll('[data-history-rounding]').length===2")
            second_record_id=js("Number(document.querySelector('[data-history-record]').dataset.historyRecord)")
            assert second_record_id!=record_id
            js("document.querySelector('[data-history-record] .primary').click()")
            wait("document.getElementById('recordTextView')?.textContent.includes('午后再次查房')")
            js("document.getElementById('backToRecordHistory').click()")
            wait("document.querySelectorAll('[data-history-record]').length===3")
            js(f"document.querySelector('[data-history-record=\"{record_id}\"] .primary').click()")
            wait("document.getElementById('recordTextView')?.textContent.includes('明天复查血常规')")
            assert js("!document.getElementById('recordTextView').textContent.includes('午后再次查房')")
            js("document.getElementById('backToRecordHistory').click()")
            wait("document.getElementById('historyEncounter')!==null")
            js("document.getElementById('historyEncounter').value='2';document.getElementById('historyEncounter').dispatchEvent(new Event('change'))")
            wait("document.getElementById('historyEncounter')?.value==='2' && document.querySelectorAll('[data-history-record]').length===0")
            js("document.getElementById('historyEncounter').value='1';document.getElementById('historyEncounter').dispatchEvent(new Event('change'))")
            wait("document.querySelectorAll('[data-history-record]').length===3")
            js("document.getElementById('patientRoundingsTab').click()")
            wait("document.getElementById('historyRoundingsSection')?.hidden===false")
            js(f"document.querySelector('[data-history-rounding=\"{round_id}\"] .secondary').click()")
            wait("document.getElementById('historicalTranscript')?.textContent.includes('明天复查血常规')")
            assert js('window.injected===undefined')
            cdp('Page.reload')
            wait("document.getElementById('homeWorkflowStatus')?.textContent==='已连接'")
            js('openPatientHistory(1)')
            wait("document.querySelectorAll('[data-history-record]').length===3")
            assert js(f"document.querySelector('[data-history-rounding=\"{round_id}\"]').textContent.includes('已结束')")
            assert js(f"document.querySelector('[data-history-rounding=\"{second_round_id}\"]').textContent.includes('待结束')")
            # Automatic event recovery, independent review and a dependent loop.
            js('startNewRounding(1)')
            wait("document.getElementById('roundModal').classList.contains('show')")
            js("document.getElementById('roundTranscript').value='血培养结果出来后通知我';document.getElementById('generateBtn').click();document.getElementById('generateBtn').click()")
            wait("document.querySelectorAll('#workflowIntents [data-intent-id]').length===1")
            js("document.querySelector('#workflowIntents details').open=true;document.querySelector('[id^=candidateSpecimen]').value='AUTO-S1';document.querySelector('#workflowIntents .primary').click()")
            wait("document.querySelectorAll('#workflowIntents [data-intent-id]').length===0 && workflowTasks.some(t=>t.matching_criteria.specimen_id==='AUTO-S1')")
            auto_loop=js("workflowTasks.find(t=>t.matching_criteria.specimen_id==='AUTO-S1').id")
            js(f'openTaskDetail({auto_loop})')
            wait("document.getElementById('labImportForm')!==null")
            js("document.getElementById('labImportForm').open=true;document.getElementById('labReportId').value='AUTO-R1';document.getElementById('labResult').value='合成报告 <img src=x onerror=\"window.reportInjected=true\">';document.getElementById('importLabButton').click()")
            wait("document.getElementById('taskEvidenceId')?.dataset.evidenceId && !reportBusy")
            assert js("document.getElementById('panelBody').textContent.includes('AUTO-R1')")
            assert js('window.reportInjected===undefined')
            js("document.querySelector('[data-finding-id] button').click()")
            wait("document.querySelector('[data-finding-id]')?.textContent.includes('ACCEPTED') && !reportBusy")
            assert js("document.querySelector('#panelBody .primary').textContent==='确认已查看结果'")
            js("document.querySelector('#panelBody .primary').click()")
            wait("document.querySelector('#panelBody .primary')?.textContent==='保存处理结论并完成' && !taskSaving")
            js("document.getElementById('taskNote').value='已核对培养，继续等待药敏';document.querySelector('#panelBody .primary').click()")
            wait("document.getElementById('followUpForm')!==null && !document.getElementById('taskNote') && !taskSaving")
            js("document.getElementById('followUpForm').open=true;document.getElementById('followTest').value='药敏';document.getElementById('followNote').value='继续等待同一标本药敏';document.querySelector('#followUpForm button').click()")
            wait("viewedReportTask.id!=="+str(auto_loop)+" && document.getElementById('labImportForm')!==null && !reportBusy")
            child_loop=js('viewedReportTask.id')
            js("document.getElementById('labImportForm').open=true;document.getElementById('labReportId').value='AUTO-AST-R1';document.getElementById('labResult').value='合成药敏报告';document.getElementById('importLabButton').click()")
            wait("document.getElementById('panelBody').textContent.includes('AUTO-AST-R1') && document.getElementById('taskEvidenceId')?.dataset.evidenceId && !reportBusy")
            trace=js(f"workflowFetch('/loops/{child_loop}/runs')")
            investigations=[r for r in trace['runs'] if any(s['tool_name']=='get_lab_reports' for s in r['steps'])]
            assert len(investigations)==2
            assert investigations[1]['prior_run_id']==investigations[0]['id']
            assert investigations[1]['plan_version']>investigations[0]['plan_version']
            cdp('Page.reload')
            wait("document.getElementById('homeWorkflowStatus')?.textContent==='已连接'")
            js(f'openTaskDetail({child_loop})')
            wait("document.getElementById('panelBody').textContent.includes('AUTO-AST-R1')")
            # Model panel: actual DB tools and runtime, explicitly injected test model.
            wait("document.getElementById('agentStartButton')!==null")
            assert js("document.getElementById('agentWorkflowPanel').textContent.includes('医疗 Agent 调查')")
            js("document.getElementById('agentStartButton').click()")
            wait("!agentPanelBusy && document.getElementById('agentPanelContent').textContent.includes('PENDING')")
            with Session(engine,autoflush=False) as db:
                result=process_agent_once(db,TestModel('ambiguous'))
                assert result['stop_reason']=='NEEDS_REVIEW'
            wait("document.getElementById('agentPanelContent').textContent.includes('test-model')")
            assert js("document.querySelectorAll('[data-agent-run]').length===1")
            js("document.getElementById('agentProgressText').value='已知晓本次药敏报告，核对后继续处理。';document.querySelector('#agentPanelContent button[onclick^=saveAgentProgress]').click()")
            wait('!agentPanelBusy')
            js("document.getElementById('agentProgressText').value='未提交的后续病程草稿';document.getElementById('taskNote').value='未提交的医生处理结论'")
            with Session(engine,autoflush=False) as db:
                run_once(db)
                result=process_agent_once(db,TestModel('note_found'))
                assert result['stop_reason']=='CANDIDATE_READY',result
            wait("document.querySelector('[data-agent-proposal]')!==null")
            assert js("document.getElementById('agentProgressText').value==='未提交的后续病程草稿'")
            assert js("document.getElementById('taskNote').value==='未提交的医生处理结论'")
            wait("document.querySelector('#agentFeedbackHome [data-agent-feedback]')?.textContent.includes('发现医生响应')")
            js('openAgentInbox()')
            wait("document.querySelector('#agentInboxList [data-agent-feedback]')!==null")
            js("document.querySelector('#agentInboxList [data-agent-feedback] button').click()")
            wait("document.querySelector('[data-agent-proposal]')!==null")
            js("[...document.querySelectorAll('[data-agent-proposal] button')].find(b=>b.textContent==='核对本次报告并确认已查看').click()")
            wait("!agentPanelBusy && document.querySelector('#panelBody .primary')?.textContent==='保存处理结论并完成'")
            wait("document.getElementById('agentFeedbackCount').textContent==='0 项待处理'")
            js('openTaskSchedule()')
            wait("document.querySelector('[data-agent-task-summary]')?.textContent.includes('Agent')")
            js('openHandoff()')
            wait("document.querySelector('[id^=handoffContent-]')!==null")
            js("[...document.querySelectorAll('#panelBody button')].find(b=>b.textContent==='确认此交接快照').click()")
            wait("document.getElementById('panelBody').textContent.includes('CONFIRMED')")
            cdp('Page.reload')
            wait("document.getElementById('homeWorkflowStatus')?.textContent==='已连接'")
            js(f'openTaskDetail({child_loop})')
            wait("document.getElementById('agentPanelContent')?.textContent.includes('ACCEPTED')")
            assert js('window.reportInjected===undefined')
            assert not errors, errors
            print('PASS: patient-name opens persisted basic information first; independent history/rounding tabs and admission selection; doctor feedback inbox, automatic result updates, unsaved input preservation, review count clearing; injected test-model Agent panel, real read tools, evidence candidate review, persisted traces, handoff confirmation and reload. No online model verification.')
            print('PASS: prior rounding/history regression; report event auto recovery; independent finding review; version-specific acknowledgement; physician resolution; dependent sensitivity task and second event recovery; persisted trace and reload; incoming report text never executes.')
        finally:
            agent_provider.configured = original_configured
            admission_service.configured,admission_service.get_provider=original_admission_configured,original_admission_provider
            server.should_exit = True
            thread.join(timeout=5)
            app.dependency_overrides.clear()
            engine.dispose()
            temporary.cleanup()


if __name__ == '__main__':
    main()
