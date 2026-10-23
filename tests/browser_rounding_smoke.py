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


def main():
    # Browser requests run concurrently. Use independent connections instead of
    # sharing one SQLite connection among multiple API threads.
    temporary = tempfile.TemporaryDirectory(prefix='health-agent-browser-')
    engine = create_engine('sqlite:///' + str(Path(temporary.name) / 'workflow.sqlite'), connect_args={'check_same_thread': False})
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Doctor(id=1, username='browser', name='合成医生', department='测试'))
        db.add(Patient(id=1, name='浏览器合成患者', admission_number='BROWSER-1', bed_number='01床', diagnosis='测试', doctor_id=1))
        db.flush()
        db.add(Encounter(id=1, encounter_number='BROWSER-ENC', patient_id=1, attending_doctor_id=1, admitted_at=datetime.now(timezone.utc)))
        db.add(Encounter(id=2, encounter_number='BROWSER-OLD-ENC', patient_id=1, attending_doctor_id=1, status='DISCHARGED', admitted_at=datetime(2025,1,1,tzinfo=timezone.utc)))
        db.commit()

    def session():
        with Session(engine, autoflush=False) as db:
            yield db

    app.dependency_overrides[get_db] = session
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=8001, log_level='error'))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(.05)

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
            cdp('Page.addScriptToEvaluateOnNewDocument', {'source': "window.HEALTH_AGENT_API='http://127.0.0.1:8001/api'"})
            errors.clear()  # Runtime.enable can replay exceptions from the previous page.
            cdp('Page.navigate', {'url': 'http://127.0.0.1:5500/index.html'})
            wait("document.getElementById('homeWorkflowStatus')?.textContent==='已连接'")
            js('openRound()')
            wait("document.getElementById('roundModal').classList.contains('show')")
            transcript = '明天复查血常规。监测血氧。联系心内科会诊。血培养结果出来后通知我。<img src=x onerror="window.injected=true">'
            js(f"document.getElementById('roundTranscript').value={json.dumps(transcript)}; document.getElementById('generateBtn').click()")
            wait("document.getElementById('generateBtn').textContent.includes('提取小任务')")
            js("document.getElementById('generateBtn').click()")
            wait("document.querySelectorAll('#workflowIntents .workflow-item').length===4")
            assert js("document.getElementById('finishRoundingButton').disabled")
            round_id = js('currentRoundingId')
            js("closePanel();openPatientDetailById(1);document.querySelector('#panelBody button[onclick^=\"continueRounding\"]').click()")
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
            wait("document.getElementById('panelBody').textContent.includes('待办 0 个 · 已完成 4 个')")
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
            js("openPatientDetailById(1);document.getElementById('patientHistoryButton').click()")
            wait("document.querySelectorAll('[data-history-record]').length===1")
            assert js("document.querySelectorAll('#historyEncounter option').length===3")
            js("document.getElementById('panelAction').click()")
            wait("document.getElementById('roundModal').classList.contains('show')")
            assert js("document.getElementById('roundTranscript').value===''")
            js("document.getElementById('roundTranscript').value='午后再次查房，患者自诉好转。';document.getElementById('generateBtn').click();document.getElementById('generateBtn').click()")
            wait(f"currentRoundingId!=={round_id} && document.getElementById('roundingLifecycle')!==null")
            second_round_id=js('currentRoundingId')
            js('openPatientHistory(1)')
            wait("document.querySelectorAll('[data-history-record]').length===2 && document.querySelectorAll('[data-history-rounding]').length===2")
            second_record_id=js("Number(document.querySelector('[data-history-record]').dataset.historyRecord)")
            assert second_record_id!=record_id
            js("document.querySelector('[data-history-record] .primary').click()")
            wait("document.getElementById('recordTextView')?.textContent.includes('午后再次查房')")
            js("document.getElementById('backToRecordHistory').click()")
            wait("document.querySelectorAll('[data-history-record]').length===2")
            js(f"document.querySelector('[data-history-record=\"{record_id}\"] .primary').click()")
            wait("document.getElementById('recordTextView')?.textContent.includes('明天复查血常规')")
            assert js("!document.getElementById('recordTextView').textContent.includes('午后再次查房')")
            js("document.getElementById('backToRecordHistory').click()")
            wait("document.getElementById('historyEncounter')!==null")
            js("document.getElementById('historyEncounter').value='2';document.getElementById('historyEncounter').dispatchEvent(new Event('change'))")
            wait("document.getElementById('historyEncounter')?.value==='2' && document.querySelectorAll('[data-history-record]').length===0")
            js("document.getElementById('historyEncounter').value='1';document.getElementById('historyEncounter').dispatchEvent(new Event('change'))")
            wait("document.querySelectorAll('[data-history-record]').length===2")
            js(f"document.querySelector('[data-history-rounding=\"{round_id}\"] .secondary').click()")
            wait("document.getElementById('historicalTranscript')?.textContent.includes('明天复查血常规')")
            assert js('window.injected===undefined')
            cdp('Page.reload')
            wait("document.getElementById('homeWorkflowStatus')?.textContent==='已连接'")
            js('openPatientHistory(1)')
            wait("document.querySelectorAll('[data-history-record]').length===2")
            assert js(f"document.querySelector('[data-history-rounding=\"{round_id}\"]').textContent.includes('已结束')")
            assert js(f"document.querySelector('[data-history-rounding=\"{second_round_id}\"]').textContent.includes('待结束')")
            assert not errors, errors
            print('PASS: rounding/task closure; same patient second visit; independent historical notes and original transcripts; admission filter; reload preserves both sessions and completion states; no JavaScript exceptions or HTML execution.')
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            app.dependency_overrides.clear()
            engine.dispose()
            temporary.cleanup()


if __name__ == '__main__':
    main()
