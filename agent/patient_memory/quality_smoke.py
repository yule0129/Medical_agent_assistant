"""Fixed synthetic quality benchmark. No development DB reads or clinical writes."""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from backend.config import settings
from .provider import get_provider
from .contracts import Extraction,ExtractionReview
from .quality import assess,POLICY_VERSION

CASES=[
    ('explicit','医生：明天复查血常规。药物过敏史还需向女儿核实。请联系心内科会诊。',[]),
    ('vague','医生：完善检查。另外复查炎症指标。',[]),
    ('not_pending','医生：血常规已经复查，结果已查看。暂不复查胸部CT。',[]),
    ('patient_request','患者：我想明天做胸部CT。医生：先评估，目前尚未安排。',[]),
    ('conditional','医生：如果明天仍发热，再复查C反应蛋白，目前尚未确定是否实施。',[]),
    ('background','医生：继续当前抗感染方案，住院规范抗感染治疗。',[]),
    ('duplicate','医生：明天复查肝功能。',[{'id':7,'title':'复查肝功能','kind':'RECHECK','status':'OPEN',
        'quote':'明天复查肝功能。','quality':{'action_text':'复查','target_text':'肝功能'},'source_at':'2026-11-03T01:00:00+00:00'}]),
]


def evaluate(case):
    name,text,known=case;provider=get_provider()
    source={'kind':'ROUNDING','id':1,'version':1,'label':'完全合成质量案例','text':text,'at':'2026-11-03T02:00:00+00:00'}
    extracted=Extraction.model_validate(provider.extract(source,known))
    review=ExtractionReview.model_validate(provider.review(source,[p.model_dump() for p in extracted.items],known)) if extracted.items else ExtractionReview(assessments=[])
    by_index={r.index:r for r in review.assessments}
    assert len(review.assessments)==len(extracted.items) and set(by_index)==set(range(len(extracted.items)))
    rows=[{'proposal':p.model_dump(),'quality':assess(p,by_index[i],source,known)} for i,p in enumerate(extracted.items)]
    keep=[r for r in rows if r['quality']['admission']=='KEEP']
    clarify=[r for r in rows if r['quality']['admission']=='CLARIFY']
    if name=='explicit':
        passed=all(any(target in r['proposal']['quote'] for r in keep) for target in ['血常规','过敏史','心内科'])
    elif name=='vague':passed=not keep and all(any(target in r['proposal']['quote'] for r in clarify) for target in ['完善检查','炎症指标'])
    elif name in {'not_pending','background'}:passed=not keep and not clarify
    elif name in {'patient_request','conditional'}:passed=not keep  # Omission or clarification is acceptable; never a ready task.
    else:passed=bool(keep) and all(7 in r['quality']['duplicate_ids'] for r in keep)
    return {'case':name,'source':source,'known':known,'rows':rows,'passed':passed,'model':provider.model}


def main():
    with ThreadPoolExecutor(max_workers=3) as pool:results=list(pool.map(evaluate,CASES))
    artifact={'synthetic_only':True,'policy_version':POLICY_VERSION,'cases':results,
        'passed':sum(r['passed'] for r in results),'total':len(results),
        'limits':'Fixed synthetic cases only; not a clinical validation or diagnostic evaluation.'}
    path=Path('docs/verification/extraction-quality-deepseek-2026-11-03.json')
    path.write_text(json.dumps(artifact,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'passed':artifact['passed'],'total':artifact['total'],'cases':[{k:r[k] for k in ['case','passed']} for r in results],'artifact':str(path)}))
    if artifact['passed']!=artifact['total']:raise SystemExit(1)


if __name__=='__main__':main()
