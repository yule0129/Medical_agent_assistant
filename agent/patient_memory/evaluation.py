"""Versioned synthetic extraction benchmark; no clinical database access.

Replay: python -m agent.patient_memory.evaluation --artifact <model-output.json>
Online: python -m agent.patient_memory.evaluation --online
"""
import argparse
import json
from pathlib import Path
from datetime import datetime, timezone
from .quality_smoke import CASES, evaluate

# Explicit engineering labels, not clinician-adjudicated clinical ground truth.
GOLD_VERSION = 'synthetic-extraction-v1'
GOLD = {
    'explicit': [('核实','药物过敏史','KEEP'), ('复查','血常规','KEEP'), ('联系','心内科','KEEP')],
    'vague': [('完善','检查','CLARIFY'), ('复查','炎症指标','CLARIFY')],
    'not_pending': [], 'background': [], 'patient_request': [], 'conditional': [],
    'duplicate': [('复查','肝功能','KEEP')],
}


def score(cases):
    names=[case['case'] for case in cases]
    if len(names)!=len(set(names)) or set(names)!=set(GOLD):
        raise ValueError('Benchmark requires each fixed case exactly once')
    tp=fp=fn=grounded=emitted=duplicates_found=duplicates_expected=0
    details=[]
    for case in cases:
        expected=list(GOLD[case['case']]); unmatched=list(expected)
        errors=[]
        for row in case['rows']:
            p,q=row['proposal'],row['quality']
            if q['admission']=='SKIP':continue
            # Patient requests and conditional plans may be retained for clarification.
            if case['case'] in {'patient_request','conditional'} and q['admission']=='CLARIFY':continue
            emitted+=1
            valid=p['quote'] in case['source']['text'] and p['action_text'] in p['quote'] and p['target_text'] in p['quote']
            grounded+=int(valid)
            key=(p['action_text'],p['target_text'],q['admission'])
            if valid and key in unmatched:
                tp+=1;unmatched.remove(key)
            else:
                fp+=1;errors.append({'type':'extra_or_wrong','item':key})
            if case['case']=='duplicate':
                duplicates_found+=int(7 in q.get('duplicate_ids',[]))
        fn+=len(unmatched)
        errors.extend({'type':'missed','item':key} for key in unmatched)
        if case['case']=='duplicate':duplicates_expected+=1
        details.append({'case':case['case'],'errors':errors,'passed':not errors})
    return {'gold_version':GOLD_VERSION,'synthetic_only':True,
        'true_positive':tp,'false_positive':fp,'false_negative':fn,
        'precision':tp/(tp+fp) if tp+fp else None,'recall':tp/(tp+fn) if tp+fn else None,
        'evidence_grounding_rate':grounded/emitted if emitted else None,
        'duplicate_detection_rate':min(duplicates_found,duplicates_expected)/duplicates_expected,
        'cases':details,'limits':'Engineering-authored synthetic labels; not clinical accuracy. Exact action/target matching is deliberately strict.'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    source=parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--artifact',type=Path);source.add_argument('--online',action='store_true')
    parser.add_argument('--output',type=Path,default=Path('docs/verification/extraction-benchmark-latest.json'))
    parser.add_argument('--baseline',type=Path)
    args=parser.parse_args()
    if args.online:
        cases=[evaluate(case) for case in CASES]
    else:
        artifact=json.loads(args.artifact.read_text(encoding='utf-8'))
        if artifact.get('synthetic_only') is not True:raise ValueError('Only explicitly synthetic artifacts are accepted')
        cases=artifact['cases']
    report=score(cases);report['generated_at']=datetime.now(timezone.utc).isoformat()
    report['mode']='online' if args.online else 'artifact_replay'
    report['model_outputs']=cases
    if args.baseline:
        previous=json.loads(args.baseline.read_text(encoding='utf-8'))
        if previous['gold_version']!=GOLD_VERSION:raise ValueError('Baseline label version differs')
        report['delta']={key:report[key]-previous[key] for key in ('precision','recall','evidence_grounding_rate','duplicate_detection_rate') if report[key] is not None and previous.get(key) is not None}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k not in {'model_outputs','cases'}},ensure_ascii=True))
    if report['false_positive'] or report['false_negative'] or report['duplicate_detection_rate']!=1:raise SystemExit(1)


if __name__=='__main__':main()
