"""Online layer-two check with fixed synthetic sources only, no development DB access."""
import json
from pathlib import Path
from backend.config import settings
from .provider import get_provider
from .runtime import validate


def main():
    provider=get_provider();results=[]
    cases=[
        ('response','VERIFY_INFO','向女儿核实药物过敏史','药物过敏史还需向女儿核实。',
         [{'id':'note-new','kind':'NOTE','label':'合成查房2','text':'医生：已向女儿核实，患者既往服用阿莫西林后出现皮疹，已补充过敏史。','is_origin':False}], 'RESPONSE_RECORDED'),
        ('result','RECHECK','复查血常规','明天复查血常规。',
         [{'id':'lab-v2','kind':'LAB','label':'血常规','text':'【合成检查】白细胞计数12.0×10^9/L。','version':2,'is_origin':False}], 'RESULT_AVAILABLE'),
        ('changed','CONTACT','联系心内科会诊','请今天联系心内科会诊。',
         [{'id':'note-change','kind':'NOTE','label':'合成查房2','text':'医生：原计划改为明天下午联系心内科会诊，今天暂不联系。','is_origin':False}], 'PLAN_CHANGED'),
        ('waiting','RECHECK','复查血常规','明天复查血常规。',[], 'WAITING'),
    ]
    for name,kind,title,quote,sources,expected in cases:
        snapshot={'matter':{'id':1,'kind':kind,'title':title,'quote':quote,'due_at':None,'original_source':{'at':'2026-10-31T01:00:00+00:00'}},
            'past_due':False,'coverage':{'complete':True,'scope':'仅合成资料'},
            'sources':[{'id':'origin','kind':'NOTE','label':'合成原计划','text':quote,'is_origin':True}]+sources}
        for source in snapshot['sources']:source.setdefault('at','2026-10-31T02:00:00+00:00')
        observation=provider.observe(snapshot);validate(snapshot,observation)
        assert observation.state==expected,(name,observation.state)
        results.append({'scenario':name,'snapshot':snapshot,'observation':observation.model_dump()})
        print('PASS:',name,observation.state,flush=True)
    path=Path('docs/verification/matter-progress-deepseek-2026-10-31.json')
    path.write_text(json.dumps({'synthetic_only':True,'model':provider.model,'cases':results},ensure_ascii=False,indent=2),encoding='utf-8')
    print('Synthetic-only artifact:',path)


if __name__=='__main__':main()
